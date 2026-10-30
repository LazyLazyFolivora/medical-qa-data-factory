"""The exploration loop: propose, gate, execute, observe.

Step 5 and Step 6 of the real-generation-agent plan. This module wires the
three pieces together into a turn:

    planner.propose -> policy gate -> executor.execute -> observation

The loop stops when the model asks to finish, when the policy refuses every
proposal, when two consecutive turns produce no new evidence, or when the
budget runs out. A refusal never ends the run on its own: the model gets the
refusal reason and may try something else.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from pydantic import Field, model_validator

from ..domain.common import ContractModel, UtcDateTime, utc_now
from ..domain.evidence import EvidenceGap
from .actions import (
    ModelActionProposal,
    PolicyGate,
    PolicyRejection,
    PolicyRejectionCode,
    RunPolicy,
    SealedAction,
)
from .contracts import ExplorationActionType, StopReason, ToolObservation
from .executor import ExecutionOutcome, Executor
from .planner import PlannerContext, PlannerPort, build_planner_context
from .task_contracts import TaskCandidate


CandidateStatus = Literal[
    "proposed", "needs_evidence", "validated", "rejected"
]
ExplorationStopReason = Literal[
    "question_targets_met",
    "all_candidates_terminal",
    "search_saturated",
    "budget_exhausted",
    "provider_unavailable",
]


class ExplorationState(ContractModel):
    """Authoritative, serialisable observation for one entity-first run.

    The model may inspect this state and propose work, but it cannot mutate
    candidate validation or publication state.  Those transitions are made by
    :func:`apply_validation_feedback` after deterministic validation.
    """

    schema_version: str = "entity-exploration-state-v1"
    episode_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    seed_entity_id: str = Field(min_length=1)
    seed_entity_name: str = Field(min_length=1)
    max_questions: int = Field(default=1, ge=1)
    candidates: list[TaskCandidate] = Field(default_factory=list)
    candidate_statuses: dict[str, CandidateStatus] = Field(default_factory=dict)
    open_evidence_gaps: list[EvidenceGap] = Field(default_factory=list)
    gap_candidate_ids: dict[str, str] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    attempted_action_hashes: list[str] = Field(default_factory=list)
    provider_status: dict[str, str] = Field(default_factory=dict)
    budget_state: dict[str, Any] = Field(default_factory=dict)
    published_task_ids: list[str] = Field(default_factory=list)
    stop_reason: ExplorationStopReason | None = None

    @model_validator(mode="after")
    def references_known_candidates(self) -> "ExplorationState":
        candidate_ids = [item.candidate_id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate IDs must be unique")
        unknown = set(self.candidate_statuses) - set(candidate_ids)
        if unknown:
            raise ValueError(
                f"candidate statuses reference unknown candidates: {sorted(unknown)}"
            )
        unknown_gap_candidates = set(self.gap_candidate_ids.values()) - set(
            candidate_ids
        )
        if unknown_gap_candidates:
            raise ValueError(
                "evidence gaps reference unknown candidates: "
                f"{sorted(unknown_gap_candidates)}"
            )
        if len(self.published_task_ids) != len(set(self.published_task_ids)):
            raise ValueError("published task IDs must be unique")
        return self

    def state_digest(self) -> str:
        """Stable identity used to reject decisions made from stale state."""

        payload = self.model_dump(mode="json", exclude_none=True)
        encoded = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class AgentDecision(ContractModel):
    """One bounded model decision over an :class:`ExplorationState`.

    Deliberately absent are fields that could claim a validated or published
    result.  The model can propose candidates; only server validation can
    promote them.
    """

    schema_version: str = "entity-agent-decision-v1"
    decision: Literal[
        "continue_search", "propose_candidate", "abandon_direction", "stop"
    ]
    #: Bound by the server to the exact state snapshot used for this call.
    #: A language model cannot reliably compute the SHA-256 state digest.
    state_digest: str | None = None
    next_action: dict[str, Any] | None = None
    selected_skill_id: str | None = None
    skill_arguments: dict[str, Any] = Field(default_factory=dict)
    expected_information: list[str] = Field(default_factory=list)
    candidates: list[TaskCandidate] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    affected_gap_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    unresolved_gap_ids: list[str] = Field(default_factory=list)
    stop_reason: ExplorationStopReason | None = None

    @model_validator(mode="after")
    def decision_has_required_payload(self) -> "AgentDecision":
        if self.decision == "propose_candidate" and not self.candidates:
            raise ValueError("propose_candidate requires at least one candidate")
        if self.decision != "propose_candidate" and self.candidates:
            raise ValueError(
                "candidates are only valid for propose_candidate"
            )
        if self.decision in {"continue_search", "abandon_direction"}:
            selection_count = int(self.next_action is not None) + int(
                self.selected_skill_id is not None
            )
            if selection_count != 1:
                raise ValueError(
                    f"{self.decision} requires exactly one of next_action "
                    "or selected_skill_id"
                )
        elif self.next_action is not None or self.selected_skill_id is not None:
            raise ValueError(
                "actions and skills are only valid for a search or "
                "redirected search"
            )
        if self.selected_skill_id is None and self.skill_arguments:
            raise ValueError("skill_arguments require selected_skill_id")
        if self.decision == "stop" and self.stop_reason is None:
            raise ValueError("stop requires a structured stop_reason")
        if self.decision != "stop" and self.stop_reason is not None:
            raise ValueError("stop_reason is only valid for a stop decision")
        return self


class ValidationFeedback(ContractModel):
    """Server-owned result of validating one proposed task candidate."""

    schema_version: str = "candidate-validation-feedback-v1"
    candidate_id: str = Field(min_length=1)
    status: Literal["validated", "recoverable_gap", "terminal_rejection"]
    evidence_gaps: list[EvidenceGap] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    message: str | None = None
    validated_task_id: str | None = None
    dependency_digest: str = Field(min_length=1)

    @model_validator(mode="after")
    def status_has_required_payload(self) -> "ValidationFeedback":
        if self.status == "validated" and not self.validated_task_id:
            raise ValueError("validated feedback requires validated_task_id")
        if self.status == "recoverable_gap" and not self.evidence_gaps:
            raise ValueError("recoverable_gap requires at least one evidence gap")
        if self.status == "terminal_rejection" and not self.reason_codes:
            raise ValueError("terminal_rejection requires a stable reason code")
        if self.status != "validated" and self.validated_task_id is not None:
            raise ValueError(
                "validated_task_id is only valid for validated feedback"
            )
        return self


class AgentDecisionRecord(ContractModel):
    """Append-only audit record for one v2 model decision."""

    record_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    turn: int = Field(ge=1)
    decision: AgentDecision
    created_at: UtcDateTime = Field(default_factory=utc_now)


class ValidationFeedbackRecord(ContractModel):
    """Append-only audit record for one candidate validation transition."""

    record_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    turn: int = Field(ge=1)
    feedback: ValidationFeedback
    created_at: UtcDateTime = Field(default_factory=utc_now)


def apply_validation_feedback(
    state: ExplorationState, feedback: ValidationFeedback
) -> ExplorationState:
    """Apply a validator-owned transition without ending unrelated search."""

    known_candidate_ids = {item.candidate_id for item in state.candidates}
    if feedback.candidate_id not in known_candidate_ids:
        raise ValueError(
            f"validation feedback references unknown candidate {feedback.candidate_id!r}"
        )

    statuses = dict(state.candidate_statuses)
    gaps_by_id = {item.gap_id: item for item in state.open_evidence_gaps}
    gap_candidate_ids = dict(state.gap_candidate_ids)
    published_task_ids = list(state.published_task_ids)

    if feedback.status == "recoverable_gap":
        statuses[feedback.candidate_id] = "needs_evidence"
        for gap in feedback.evidence_gaps:
            gaps_by_id[gap.gap_id] = gap
            gap_candidate_ids[gap.gap_id] = feedback.candidate_id
    elif feedback.status == "terminal_rejection":
        statuses[feedback.candidate_id] = "rejected"
        for gap_id, candidate_id in list(gap_candidate_ids.items()):
            if candidate_id == feedback.candidate_id:
                gap_candidate_ids.pop(gap_id, None)
                gaps_by_id.pop(gap_id, None)
    else:
        statuses[feedback.candidate_id] = "validated"
        for gap_id, candidate_id in list(gap_candidate_ids.items()):
            if candidate_id == feedback.candidate_id:
                gap_candidate_ids.pop(gap_id, None)
                gaps_by_id.pop(gap_id, None)
        assert feedback.validated_task_id is not None
        if feedback.validated_task_id not in published_task_ids:
            published_task_ids.append(feedback.validated_task_id)

    payload = state.model_dump(mode="python")
    payload.update(
        {
            "candidate_statuses": statuses,
            "open_evidence_gaps": list(gaps_by_id.values()),
            "gap_candidate_ids": gap_candidate_ids,
            "published_task_ids": published_task_ids,
        }
    )
    return ExplorationState.model_validate(payload)


def build_exploration_planner_context(
    state: ExplorationState, *, turn: int, policy: RunPolicy
) -> PlannerContext:
    """Project the persisted exploration state into the bounded model view."""

    return build_planner_context(
        run_id=state.task_id,
        episode_id=state.episode_id,
        turn=turn,
        seed_entity_id=state.seed_entity_id,
        seed_entity_name=state.seed_entity_name,
        known_entities=[
            {
                "canonical_id": state.seed_entity_id,
                "canonical_name": state.seed_entity_name,
            }
        ],
        observations=[],
        open_needs=[],
        evidence_gaps=[item.description for item in state.open_evidence_gaps],
        executed_action_hashes=state.attempted_action_hashes,
        policy=policy,
        available_tools=policy.available_tool_names,
        available_actions=policy.allowed_action_types,
        evidence_summary={
            "evidence_ids": list(state.evidence_ids),
            "claim_ids": list(state.claim_ids),
            "candidate_statuses": dict(state.candidate_statuses),
            "published_task_count": len(state.published_task_ids),
            "max_questions": state.max_questions,
        },
    )


def should_stop_exploration(state: ExplorationState) -> bool:
    """Return whether an explicit, auditable terminal condition was reached."""

    if state.stop_reason is not None:
        return True
    if len(state.published_task_ids) >= state.max_questions:
        return True
    return False


class ExplorationStop(str):
    """Why the exploration loop ended."""


@dataclass
class ExplorationTurn:
    """One turn of the loop, with everything that happened in it."""

    turn: int
    proposal: ModelActionProposal | None = None
    sealed: SealedAction | None = None
    rejection: PolicyRejection | None = None
    outcome: ExecutionOutcome | None = None
    observation: ToolObservation | None = None

    @property
    def was_refused(self) -> bool:
        return self.sealed is None

    @property
    def produced_new_evidence(self) -> bool:
        if self.outcome is None:
            return False
        return bool(self.outcome.new_evidence_ids or self.outcome.new_path_ids)


@dataclass
class ExplorationResult:
    """The whole run of the exploration loop."""

    turns: list[ExplorationTurn] = field(default_factory=list)
    stop_reason: StopReason | None = None
    stop_message: str = ""
    known_entity_ids: set[str] = field(default_factory=set)
    executed_action_hashes: list[str] = field(default_factory=list)

    @property
    def observations(self) -> list[ToolObservation]:
        return [item.observation for item in self.turns if item.observation]

    @property
    def refusals(self) -> list[PolicyRejection]:
        return [item.rejection for item in self.turns if item.rejection]

    @property
    def tool_call_count(self) -> int:
        return sum(1 for item in self.turns if item.sealed is not None)

    @property
    def reached_evidence_sufficiency(self) -> bool:
        return self.stop_reason is StopReason.EVIDENCE_SUFFICIENT


class ExplorationLoop:
    """Runs bounded turns of propose, gate, execute and observe."""

    def __init__(
        self,
        *,
        planner: PlannerPort,
        executor: Executor,
        policy: RunPolicy,
        run_id: str,
        episode_id: str,
        seed_entity_id: str,
        seed_entity_name: str,
        available_tools: Sequence[str] = (),
        open_needs: Sequence[dict[str, Any]] = (),
        #: The hook that decides whether the collected evidence is enough.
        sufficiency: Any = None,
    ) -> None:
        self.planner = planner
        self.executor = executor
        self.policy = policy
        self.run_id = run_id
        self.episode_id = episode_id
        self.seed_entity_id = seed_entity_id
        self.seed_entity_name = seed_entity_name
        self.available_tools = list(available_tools)
        self.open_needs = [dict(item) for item in open_needs]
        self.sufficiency = sufficiency
        self.known_entity_ids: set[str] = {seed_entity_id}
        self.observations: list[ToolObservation] = []
        self.executed_action_hashes: list[str] = []
        self._last_refusal: str = ""

    def run(self, max_turns: int | None = None) -> ExplorationResult:
        """Run the loop until it stops."""
        result = ExplorationResult()
        limit = max_turns if max_turns is not None else self.policy.max_turns
        turns_without_evidence = 0
        turn = 0
        while turn < limit:
            turn += 1
            context = self._context(turn)
            proposal = self._propose(context)
            if proposal is None:
                result.stop_reason = StopReason.MODEL_ERROR
                result.stop_message = (
                    "the model did not return a usable proposal"
                )
                break
            gate = PolicyGate(self.policy)
            sealed, rejection = gate.evaluate(
                proposal,
                run_id=self.run_id,
                episode_id=self.episode_id,
                turn=turn,
                known_entity_ids=self.known_entity_ids,
                known_observation_ids={
                    item.observation_id for item in self.observations
                },
            )
            if sealed is None:
                assert rejection is not None
                self._last_refusal = rejection.message
                result.turns.append(
                    ExplorationTurn(
                        turn=turn, proposal=proposal, rejection=rejection
                    )
                )
                # A refusal does not end the run. The model is told why and
                # may try something else, until the turn budget runs out.
                turns_without_evidence += 1
                if turns_without_evidence >= 2:
                    result.stop_reason = StopReason.NO_VALID_ACTION
                    result.stop_message = (
                        "two consecutive proposals were refused: "
                        f"{self._last_refusal}"
                    )
                    break
                continue
            self._last_refusal = ""
            if sealed.action_type is ExplorationActionType.FINISH:
                result.turns.append(
                    ExplorationTurn(turn=turn, proposal=proposal, sealed=sealed)
                )
                result.stop_reason = StopReason.EVIDENCE_SUFFICIENT
                result.stop_message = "the model asked to finish"
                break
            outcome = self.executor.execute(
                sealed, episode_id=self.episode_id, run_id=self.run_id
            )
            self.observations.append(outcome.observation)
            self.known_entity_ids.update(outcome.discovered_entity_ids)
            self.executed_action_hashes.append(sealed.canonical_action_hash)
            self.policy = self.policy.with_execution(
                sealed.canonical_action_hash
            ).with_evidence()
            if outcome.new_evidence_ids or outcome.new_path_ids:
                turns_without_evidence = 0
            else:
                turns_without_evidence += 1
            result.turns.append(
                ExplorationTurn(
                    turn=turn,
                    proposal=proposal,
                    sealed=sealed,
                    outcome=outcome,
                    observation=outcome.observation,
                )
            )
            if turns_without_evidence >= 2:
                result.stop_reason = StopReason.NO_NEW_EVIDENCE
                result.stop_message = (
                    "two consecutive actions produced no new evidence"
                )
                break
            if self.sufficiency is not None and self._is_sufficient():
                result.stop_reason = StopReason.EVIDENCE_SUFFICIENT
                result.stop_message = "the evidence is sufficient"
                break
        else:
            result.stop_reason = StopReason.BUDGET_EXHAUSTED
            result.stop_message = f"the turn budget of {limit} was exhausted"
        result.known_entity_ids = set(self.known_entity_ids)
        result.executed_action_hashes = list(self.executed_action_hashes)
        return result

    def _propose(self, context: PlannerContext) -> ModelActionProposal | None:
        try:
            return self.planner.propose(context, ModelActionProposal)
        except Exception:
            return None

    def _is_sufficient(self) -> bool:
        if self.sufficiency is None:
            return False
        try:
            return bool(self.sufficiency(self.observations))
        except Exception:
            return False

    def _context(self, turn: int) -> PlannerContext:
        return build_planner_context(
            run_id=self.run_id,
            episode_id=self.episode_id,
            turn=turn,
            seed_entity_id=self.seed_entity_id,
            seed_entity_name=self.seed_entity_name,
            known_entities=[
                {"canonical_id": item} for item in sorted(self.known_entity_ids)
            ],
            observations=self.observations,
            open_needs=self.open_needs,
            evidence_gaps=[self._last_refusal] if self._last_refusal else [],
            executed_action_hashes=self.executed_action_hashes,
            policy=self.policy,
            available_tools=self.available_tools,
        )
