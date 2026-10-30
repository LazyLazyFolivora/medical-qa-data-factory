"""Independent validation adapter for model-proposed task candidates."""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from typing import Any

from ..domain import EvidenceGap, SuggestedSource
from .exploration import ValidationFeedback
from .task_compiler import TaskCompilationError, TaskCompiler
from .task_contracts import (
    ProviderSnapshotRef,
    TaskCandidate,
    ValidatedTask,
    ValidationEnvironment,
    ValidationReport,
    promote_validated_task,
)
from .task_validation import TaskValidator
from .world_evaluation import PrimeKGWorldEvaluator


_RECOVERABLE_REASONS = frozenset(
    {
        "answer_set_truncated",
        "incomplete_world_query",
        "incomplete_search",
        "evaluator_query_mismatch",
        "relaxed_evaluator_query_mismatch",
    }
)


class GraphTaskCandidateAssessor:
    """Compile and validate candidates against a complete pinned KG world.

    Candidate discovery and validation deliberately use different authority:
    the model proposes a task from bounded observations, while this adapter
    recomputes the answer set through the provider-backed world evaluator.
    """

    def __init__(
        self,
        provider: Any,
        *,
        environment: ValidationEnvironment | None = None,
        compiler: TaskCompiler | None = None,
        validator: TaskValidator | None = None,
        on_validated: Callable[[ValidatedTask], None] | None = None,
        on_validation_report: Callable[[ValidationReport], None] | None = None,
        page_size: int = 500,
    ) -> None:
        self.provider = provider
        self.environment = environment or self._environment(provider)
        self.compiler = compiler or TaskCompiler()
        self.validator = validator or TaskValidator(
            PrimeKGWorldEvaluator(provider, page_size=page_size)
        )
        self.on_validated = on_validated
        self.on_validation_report = on_validation_report

    def __call__(
        self,
        observations: Sequence[Any],
        state: dict[str, Any],
    ) -> tuple[list[TaskCandidate], list[ValidationFeedback]]:
        del observations  # validation reads the frozen world, not summaries
        candidates = [
            TaskCandidate.model_validate(item)
            for item in state.get("react_candidates", [])
        ]
        statuses = dict(state.get("react_candidate_statuses", {}))
        feedback: list[ValidationFeedback] = []
        for candidate in candidates:
            if statuses.get(candidate.candidate_id) in {"validated", "rejected"}:
                continue
            feedback.append(self._assess(candidate))
        return candidates, feedback

    def _assess(self, candidate: TaskCandidate) -> ValidationFeedback:
        try:
            task = self.compiler.compile(candidate, self.environment)
        except (TaskCompilationError, ValueError) as exc:
            return ValidationFeedback(
                candidate_id=candidate.candidate_id,
                status="terminal_rejection",
                reason_codes=["candidate_not_compilable"],
                message=f"{type(exc).__name__}: {exc}",
                dependency_digest=self._digest(
                    candidate.semantic_digest(), type(exc).__name__, str(exc)
                ),
            )

        report = self.validator.validate(
            task,
            slice_answer_ids=candidate.proposed_answer_ids,
        )
        if self.on_validation_report is not None:
            self.on_validation_report(report)
        dependency_digest = self._digest(
            candidate.semantic_digest(), report.report_id
        )
        if report.passed:
            validated = promote_validated_task(task, report)
            if self.on_validated is not None:
                self.on_validated(validated)
            return ValidationFeedback(
                candidate_id=candidate.candidate_id,
                status="validated",
                validated_task_id=validated.task.task_id,
                dependency_digest=dependency_digest,
            )

        reasons = list(report.reason_codes)
        recoverable = bool(reasons) and set(reasons).issubset(
            _RECOVERABLE_REASONS
        )
        if recoverable:
            gaps = [
                EvidenceGap(
                    gap_id=(
                        "gap-"
                        + self._digest(candidate.candidate_id, reason)[:24]
                    ),
                    gap_type=reason,
                    description=self._gap_description(reason),
                    suggested_source=SuggestedSource.EITHER,
                    blocking=True,
                )
                for reason in reasons
            ]
            return ValidationFeedback(
                candidate_id=candidate.candidate_id,
                status="recoverable_gap",
                evidence_gaps=gaps,
                reason_codes=reasons,
                dependency_digest=dependency_digest,
            )
        return ValidationFeedback(
            candidate_id=candidate.candidate_id,
            status="terminal_rejection",
            reason_codes=reasons or ["candidate_validation_failed"],
            dependency_digest=dependency_digest,
        )

    @staticmethod
    def _environment(provider: Any) -> ValidationEnvironment:
        snapshot = provider.snapshot()
        return ValidationEnvironment(
            environment_id=(
                f"world-{snapshot.provider_id}-{snapshot.data_snapshot_id}"
            ),
            providers=[
                ProviderSnapshotRef(
                    provider_id=snapshot.provider_id,
                    snapshot_id=snapshot.data_snapshot_id,
                    schema_version=getattr(snapshot, "adapter_version", None),
                    manifest_hash=getattr(snapshot, "sha256", None),
                    completeness="complete_paginated_snapshot_query",
                    detail={
                        key: value
                        for key, value in {
                            "config_hash": getattr(snapshot, "config_hash", None),
                            "size_bytes": getattr(snapshot, "size_bytes", None),
                        }.items()
                        if value is not None
                    },
                )
            ],
            completeness_scope=(
                "all matching edges returned by complete paginated queries "
                "against the frozen graph snapshot"
            ),
        )

    @staticmethod
    def _digest(*parts: str) -> str:
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    @staticmethod
    def _gap_description(reason: str) -> str:
        descriptions = {
            "answer_set_truncated": (
                "the complete-world answer set was truncated"
            ),
            "incomplete_world_query": (
                "the complete-world query did not finish"
            ),
            "incomplete_search": (
                "one or more competitor searches were incomplete"
            ),
            "evaluator_query_mismatch": (
                "the validator response did not match the submitted query"
            ),
            "relaxed_evaluator_query_mismatch": (
                "a qualifier counterfactual query could not be verified"
            ),
        }
        return descriptions.get(reason, reason.replace("_", " "))
