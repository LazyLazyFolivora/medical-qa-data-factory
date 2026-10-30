"""Append-only business audit store for generation episodes.

LangGraph checkpoints allow replay; they are not an audit trail. Every state
change writes a new immutable version instead of overwriting in place.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..repository import (
    LocalArtifactRepository,
    RecordAlreadyExistsError,
    RecordNotFoundError,
)
from .contracts import (
    AgentStep,
    GenerationEpisode,
    RejectionRecord,
    ToolObservation,
    VerificationResult,
)
from .exploration import AgentDecisionRecord, ValidationFeedbackRecord
from .skills import SkillInvocation, SkillOutcome

EPISODES = "episodes"
STEPS = "generation_steps"
OBSERVATIONS = "tool_observations"
VERIFICATIONS = "verification_results"
REJECTIONS = "rejection_records"
REVIEW_OPERATIONS = "review_operations"
AGENT_DECISIONS = "entity_agent_decisions"
VALIDATION_FEEDBACK = "candidate_validation_feedback"
SKILL_INVOCATIONS = "skill_invocations"
SKILL_OUTCOMES = "skill_outcomes"


class IdempotencyConflictError(RuntimeError):
    """Same identity, different content: the run must stop."""

    code = "idempotency_conflict"


def record_digest(record: Any) -> str:
    return hashlib.sha256(record.model_dump_json().encode("utf-8")).hexdigest()


#: Wall-clock and per-call identity fields legitimately differ between a run
#: and its replay, so they are excluded from the idempotency comparison.
VOLATILE_FIELDS = frozenset(
    {
        "created_at",
        "retrieved_at",
        "retrieval_request_id",
        "started_at",
        "completed_at",
        "updated_at",
        "recorded_at",
        "invocation_id",
        "usage_id",
        "change_id",
        "elapsed_ms",
        "changes",
        "check_id",
        # A validation result records when it ran. That timestamp legitimately
        # differs between a run and its replay, so it must not take part in
        # the idempotency comparison. The result itself still does.
        "evaluated_at",
        # Query traces are operation-level observations. A replay performs a
        # new physical read and may receive a new trace id, but that does not
        # change the frozen evidence semantics or derive a new artifact.
        "query_trace_id",
        "records_returned",
    }
)


def content_digest(record: Any) -> str:
    def _strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: _strip(item)
                for key, item in value.items()
                if key not in VOLATILE_FIELDS
            }
        if isinstance(value, list):
            return [_strip(item) for item in value]
        return value

    payload = json.dumps(_strip(record.model_dump(mode="json")), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class GenerationRepository:
    """Versioned storage for episodes and their append-only collections."""

    def __init__(self, root: str | Path) -> None:
        self.artifacts = LocalArtifactRepository(root)

    # ---- generic idempotent writes -------------------------------------

    def save_idempotent(self, collection: str, record_id: str, version: str, record: Any) -> str:
        """Write a version, treating an identical rewrite as success.

        A different payload under the same identity raises
        ``idempotency_conflict``: silently accepting it would corrupt the
        audit trail.
        """
        digest = content_digest(record)
        try:
            self.artifacts.save(collection, record_id, version, record)
            return "written"
        except RecordAlreadyExistsError:
            existing = self.artifacts.load(collection, record_id, version, type(record))
            if content_digest(existing) != digest:
                raise IdempotencyConflictError(
                    f"{collection}/{record_id}/{version} already exists with different content"
                )
            return "existing"

    # ---- episodes ------------------------------------------------------

    def save_episode(self, episode: GenerationEpisode) -> str:
        return self.save_idempotent(
            EPISODES, episode.episode_id, episode.episode_version, episode
        )

    def load_episode(self, episode_id: str, version: str) -> GenerationEpisode:
        return self.artifacts.load(EPISODES, episode_id, version, GenerationEpisode)

    def load_latest_episode(self, episode_id: str) -> GenerationEpisode:
        return self.artifacts.load_latest(EPISODES, episode_id, GenerationEpisode)

    def list_episode_ids(self) -> list[str]:
        return self.artifacts.list_record_ids(EPISODES)

    def list_episodes(self) -> list[GenerationEpisode]:
        return [
            self.artifacts.load_latest(EPISODES, item, GenerationEpisode)
            for item in self.list_episode_ids()
        ]

    def graph_thread_id(self, episode_id: str) -> str:
        """One episode maps to exactly one LangGraph thread."""
        return episode_id

    # ---- steps ---------------------------------------------------------

    def save_step(self, step: AgentStep) -> str:
        return self.save_idempotent(STEPS, step.step_id, step.step_version, step)

    def load_step(self, step_id: str, version: str) -> AgentStep:
        return self.artifacts.load(STEPS, step_id, version, AgentStep)

    def list_step_versions(self, step_id: str) -> list[str]:
        return self.artifacts.list_versions(STEPS, step_id)

    def list_steps_by_episode(self, episode_id: str) -> list[AgentStep]:
        steps: list[AgentStep] = []
        for step_id in self.artifacts.list_record_ids(STEPS):
            try:
                step = self.artifacts.load_latest(STEPS, step_id, AgentStep)
            except RecordNotFoundError:  # pragma: no cover - defensive
                continue
            if step.episode_id == episode_id:
                steps.append(step)
        return sorted(steps, key=lambda item: (item.sequence, item.node))

    # ---- append-only collections --------------------------------------

    def save_observation(self, observation: ToolObservation) -> str:
        return self.save_idempotent(
            OBSERVATIONS, observation.observation_id, observation.observation_id, observation
        )

    def list_observations_by_episode(self, episode_id: str) -> list[ToolObservation]:
        return self._list_by_episode(OBSERVATIONS, episode_id, ToolObservation)

    def save_verification(self, result: VerificationResult) -> str:
        record_id = f"{result.draft_id}.{len(result.reason_codes)}"
        return self.save_idempotent(VERIFICATIONS, record_id, record_id, result)

    def list_verifications_by_episode(self, episode_id: str) -> list[VerificationResult]:
        return self._list_by_episode(VERIFICATIONS, episode_id, VerificationResult)

    # ---- review operations ---------------------------------------------

    def save_review_operation(self, operation: Any) -> str:
        """Journal a review operation.

        The journal is append-only: a composite operation records each step
        as it completes, so a run that died halfway can be reconciled.
        """
        return self.save_idempotent(
            REVIEW_OPERATIONS,
            operation.operation_id,
            operation.operation_version,
            operation,
        )

    def list_review_operations(self) -> list[Any]:
        """Every journalled review operation, newest last."""
        from .review_operations import ReviewOperation

        items: list[Any] = []
        for record_id in self.artifacts.list_record_ids(REVIEW_OPERATIONS):
            try:
                items.append(
                    self.artifacts.load_latest(
                        REVIEW_OPERATIONS, record_id, ReviewOperation
                    )
                )
            except RecordNotFoundError:  # pragma: no cover - defensive
                continue
        return sorted(items, key=lambda item: item.created_at)

    def list_unfinished_review_operations(self) -> list[Any]:
        """The operations that did not reach a terminal state."""
        from .review_operations import ReviewOperationStatus

        return [
            item
            for item in self.list_review_operations()
            if item.status is ReviewOperationStatus.PENDING
        ]

    def save_rejection(self, record: RejectionRecord) -> str:
        return self.save_idempotent(
            REJECTIONS, record.rejection_id, record.rejection_id, record
        )

    def list_rejections_by_episode(self, episode_id: str) -> list[RejectionRecord]:
        return self._list_by_episode(REJECTIONS, episode_id, RejectionRecord)

    def save_agent_decision(self, record: AgentDecisionRecord) -> str:
        return self.save_idempotent(
            AGENT_DECISIONS, record.record_id, record.record_id, record
        )

    def list_agent_decisions_by_episode(
        self, episode_id: str
    ) -> list[AgentDecisionRecord]:
        return sorted(
            self._list_by_episode(
                AGENT_DECISIONS, episode_id, AgentDecisionRecord
            ),
            key=lambda item: item.turn,
        )

    def save_validation_feedback(
        self, record: ValidationFeedbackRecord
    ) -> str:
        return self.save_idempotent(
            VALIDATION_FEEDBACK, record.record_id, record.record_id, record
        )

    def list_validation_feedback_by_episode(
        self, episode_id: str
    ) -> list[ValidationFeedbackRecord]:
        return sorted(
            self._list_by_episode(
                VALIDATION_FEEDBACK, episode_id, ValidationFeedbackRecord
            ),
            key=lambda item: (item.turn, item.feedback.candidate_id),
        )

    def save_skill_invocation(self, invocation: SkillInvocation) -> str:
        return self.save_idempotent(
            SKILL_INVOCATIONS,
            invocation.invocation_id,
            invocation.invocation_id,
            invocation,
        )

    def list_skill_invocations_by_episode(
        self, episode_id: str
    ) -> list[SkillInvocation]:
        return sorted(
            self._list_by_episode(
                SKILL_INVOCATIONS, episode_id, SkillInvocation
            ),
            key=lambda item: (item.turn, item.invocation_id),
        )

    def save_skill_outcome(self, outcome: SkillOutcome) -> str:
        return self.save_idempotent(
            SKILL_OUTCOMES,
            outcome.invocation_id,
            outcome.invocation_id,
            outcome,
        )

    def list_skill_outcomes_by_episode(
        self, episode_id: str
    ) -> list[SkillOutcome]:
        return sorted(
            self._list_by_episode(SKILL_OUTCOMES, episode_id, SkillOutcome),
            key=lambda item: (item.created_at, item.invocation_id),
        )

    def _list_by_episode(self, collection: str, episode_id: str, model: Any) -> list[Any]:
        items: list[Any] = []
        for record_id in self.artifacts.list_record_ids(collection):
            try:
                record = self.artifacts.load_latest(collection, record_id, model)
            except RecordNotFoundError:  # pragma: no cover - defensive
                continue
            if getattr(record, "episode_id", None) == episode_id:
                items.append(record)
        return items
