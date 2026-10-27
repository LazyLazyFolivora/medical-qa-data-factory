"""Authoritative task validation against a complete validation world."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from .ctd_world_evaluation import (
    KNOWN_OUTCOME_STATUSES,
    CtdWorldEvaluator,
)
from .logical_query import QueryEvaluator
from .task_contracts import (
    AnswerCandidateOutcome,
    StudyContext,
    TaskSpec,
    ValidationReport,
)


TASK_VALIDATOR_VERSION = "task-validator-v2"


def validation_report_id_for(
    task: TaskSpec,
    *,
    validator_version: str,
    world_answer_ids: list[str],
    result_complete: bool,
    reason_codes: list[str],
    rows_examined: int,
    matched_claim_ids: Sequence[str] = (),
    supporting_evidence_ids: Sequence[str] = (),
    competitor_outcomes: Sequence[AnswerCandidateOutcome] = (),
    evidence_dependency_digest: str | None = None,
) -> str:
    """Return the content identity of a validation result."""

    payload = {
        "task_id": task.task_id,
        "query_digest": task.query.dependency_digest,
        "environment_id": task.validation_environment.environment_id,
        "provider_snapshots": [
            item.model_dump(mode="json")
            for item in task.validation_environment.providers
        ],
        "validator_version": validator_version,
        "world_answer_ids": world_answer_ids,
        "result_complete": result_complete,
        "reason_codes": reason_codes,
        "rows_examined": rows_examined,
        "matched_claim_ids": sorted(set(matched_claim_ids)),
        "supporting_evidence_ids": sorted(set(supporting_evidence_ids)),
        "competitor_outcomes": [
            item.model_dump(mode="json")
            for item in sorted(
                competitor_outcomes,
                key=lambda outcome: (outcome.answer_id, outcome.status),
            )
        ],
        "evidence_dependency_digest": evidence_dependency_digest,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    return f"validation-report-{digest}"


class TaskValidator:
    """Validate a compiled task without trusting its candidate answer."""

    def __init__(
        self,
        evaluator: QueryEvaluator,
        *,
        version: str = TASK_VALIDATOR_VERSION,
    ) -> None:
        self.evaluator = evaluator
        self.version = version

    def validate(
        self,
        task: TaskSpec,
        *,
        slice_answer_ids: Sequence[str] = (),
    ) -> ValidationReport:
        reasons: set[str] = set()
        if task.query.dependency_digest != task.query.digest():
            reasons.add("query_digest_mismatch")
        answer = self.evaluator.answer(task.query)
        rows_examined = answer.rows_examined
        known_statuses = {"ok", "ambiguous", "no_answer", "incomplete"}
        if answer.status not in known_statuses:
            reasons.add("invalid_evaluator_status")
        if answer.query_id != task.query.query_id:
            reasons.add("evaluator_query_mismatch")
        world_answers = sorted(set(answer.answer_ids))
        slice_answers = sorted(set(slice_answer_ids))

        result_complete = (
            answer.status in {"ok", "ambiguous", "no_answer"}
            and not answer.truncated
            and "query_digest_mismatch" not in reasons
            and "evaluator_query_mismatch" not in reasons
        )
        if answer.truncated:
            reasons.add("answer_set_truncated")
        if answer.status == "incomplete":
            reasons.add("incomplete_world_query")
        elif answer.status == "no_answer":
            reasons.add("world_answer_missing")
        elif answer.status == "ambiguous":
            reasons.add("world_cardinality_mismatch")

        cardinality = task.proposed_answer_contract.cardinality
        if cardinality == "exactly_one" and len(world_answers) != 1:
            reasons.add("world_cardinality_mismatch")
        elif cardinality == "one_or_more" and not world_answers:
            reasons.add("world_cardinality_mismatch")

        proposed = sorted(
            set(task.proposed_answer_contract.canonical_answer_ids)
        )
        if result_complete and proposed != world_answers:
            reasons.add("candidate_answer_mismatch")

        # A constrained question has training value only when every stated
        # constraint changes the complete answer set.  Otherwise the answer
        # can be obtained through a cheaper shortcut and the extra condition
        # is decorative rather than an information requirement.
        if result_complete and task.query.constraints:
            for index in range(len(task.query.constraints)):
                relaxed_constraints = [
                    constraint
                    for position, constraint in enumerate(
                        task.query.constraints
                    )
                    if position != index
                ]
                relaxed_query = task.query.model_copy(
                    update={
                        "constraints": relaxed_constraints,
                        "dependency_digest": "pending",
                    }
                )
                relaxed_digest = relaxed_query.digest()
                relaxed_query = relaxed_query.model_copy(
                    update={
                        "query_id": f"query-relaxed-{relaxed_digest[:24]}",
                        "dependency_digest": relaxed_digest,
                    }
                )
                relaxed = self.evaluator.answer(relaxed_query)
                rows_examined += relaxed.rows_examined
                if relaxed.query_id != relaxed_query.query_id:
                    reasons.add("relaxed_evaluator_query_mismatch")
                    result_complete = False
                    continue
                if (
                    relaxed.truncated
                    or relaxed.status not in {"ok", "ambiguous"}
                ):
                    reasons.add("incomplete_search")
                    result_complete = False
                    continue
                relaxed_answers = set(relaxed.answer_ids)
                if not set(world_answers).issubset(relaxed_answers):
                    reasons.add("constraint_evaluation_mismatch")
                elif len(relaxed_answers) <= len(world_answers):
                    reasons.add("shortcut_detected")

        reason_codes = sorted(reasons)
        report_id = validation_report_id_for(
            task,
            validator_version=self.version,
            world_answer_ids=world_answers,
            result_complete=result_complete,
            reason_codes=reason_codes,
            rows_examined=rows_examined,
        )
        return ValidationReport(
            report_id=report_id,
            task_id=task.task_id,
            query_id=task.query.query_id,
            query_digest=task.query.dependency_digest,
            environment_id=task.validation_environment.environment_id,
            passed=not reason_codes,
            reason_codes=reason_codes,
            slice_answer_ids=slice_answers,
            world_answer_ids=world_answers,
            result_complete=result_complete,
            rows_examined=rows_examined,
            validator_version=self.version,
            validated_at=answer.evaluated_at,
        )


class CtdTaskValidator:
    """Validate a CTD task against every disease competitor in its snapshot."""

    version = "ctd-task-validator-v1"
    _RELAXABLE_FIELDS = (
        "population",
        "exposure_context",
        "dose_or_regimen",
        "exposure_duration",
        "study_type",
        "sample_type",
        "temporal_scope",
        "disease_category",
    )

    def __init__(self, evaluator: CtdWorldEvaluator) -> None:
        self.evaluator = evaluator

    def validate(
        self,
        task: TaskSpec,
        *,
        slice_answer_ids: Sequence[str] = (),
    ) -> ValidationReport:
        if task.question_type != "study_context_qualified_disease":
            raise ValueError("CTD validator only accepts study-context tasks")
        context = task.public_study_context
        if context is None:
            raise ValueError("CTD task is missing its public study context")
        chemical_id = task.seed_entity_id.rsplit(":", 1)[-1]
        evaluation = self.evaluator.evaluate(
            chemical_id=chemical_id,
            snapshot_id=task.query.snapshot_id,
            context=context,
        )
        reasons = set(evaluation.reason_codes)
        result_complete = evaluation.result_complete
        ctd_snapshots = {
            item.snapshot_id
            for item in task.validation_environment.providers
            if item.provider_id == "ctd"
        }
        if task.query.snapshot_id not in ctd_snapshots:
            reasons.add("ctd_snapshot_mismatch")
            result_complete = False
        if not any(
            item.provider_id == "pubmed"
            for item in task.validation_environment.providers
        ):
            reasons.add("scope_not_closed")
            result_complete = False
        if any(
            item.status not in KNOWN_OUTCOME_STATUSES
            for item in evaluation.competitor_outcomes
        ):
            reasons.add("unknown_competitor")
            result_complete = False

        world_answers = sorted(set(evaluation.world_answer_ids))
        proposed = sorted(
            set(task.proposed_answer_contract.canonical_answer_ids)
        )
        if len(world_answers) != 1:
            reasons.add(
                "world_answer_missing"
                if not world_answers
                else "world_cardinality_mismatch"
            )
        if result_complete and proposed != world_answers:
            reasons.add("candidate_answer_mismatch")

        matched = [
            item
            for item in evaluation.competitor_outcomes
            if item.status == "matched"
        ]
        matched_claim_ids = sorted(
            {claim_id for item in matched for claim_id in item.claim_ids}
        )
        supporting_evidence_ids = sorted(
            {
                evidence_id
                for item in matched
                for evidence_id in item.evidence_ids
            }
        )
        if world_answers and (
            not matched_claim_ids or not supporting_evidence_ids
        ):
            reasons.add("claim_not_supported")
            result_complete = False
        if not set(matched_claim_ids).issubset(
            task.candidate_claim_ids
        ) or not set(supporting_evidence_ids).issubset(
            task.candidate_evidence_ids
        ):
            reasons.add("evidence_dependency_mismatch")

        relaxed_rows = 0
        any_discriminating_qualifier = False
        baseline_answers = set(world_answers)
        for qualifier, relaxed in self._relaxed_contexts(context):
            relaxed_evaluation = self.evaluator.evaluate(
                chemical_id=chemical_id,
                snapshot_id=task.query.snapshot_id,
                context=relaxed,
            )
            relaxed_rows += relaxed_evaluation.rows_examined
            if not relaxed_evaluation.result_complete:
                reasons.add("incomplete_search")
                result_complete = False
                continue
            if (
                set(relaxed_evaluation.world_answer_ids)
                != baseline_answers
                and qualifier != "disease_category"
            ):
                any_discriminating_qualifier = True
        if not any_discriminating_qualifier:
            reasons.add("shortcut_detected")

        reason_codes = sorted(reasons)
        report_id = validation_report_id_for(
            task,
            validator_version=self.version,
            world_answer_ids=world_answers,
            result_complete=result_complete,
            reason_codes=reason_codes,
            rows_examined=evaluation.rows_examined + relaxed_rows,
            matched_claim_ids=matched_claim_ids,
            supporting_evidence_ids=supporting_evidence_ids,
            competitor_outcomes=evaluation.competitor_outcomes,
            evidence_dependency_digest=task.evidence_dependency_digest(),
        )
        return ValidationReport(
            report_id=report_id,
            task_id=task.task_id,
            query_id=task.query.query_id,
            query_digest=task.query.dependency_digest,
            environment_id=task.validation_environment.environment_id,
            passed=not reason_codes,
            reason_codes=reason_codes,
            slice_answer_ids=sorted(set(slice_answer_ids)),
            world_answer_ids=world_answers,
            result_complete=result_complete,
            rows_examined=evaluation.rows_examined + relaxed_rows,
            matched_claim_ids=matched_claim_ids,
            supporting_evidence_ids=supporting_evidence_ids,
            competitor_outcomes=list(evaluation.competitor_outcomes),
            evidence_dependency_digest=task.evidence_dependency_digest(),
            validator_version=self.version,
            validated_at=evaluation.evaluated_at,
        )

    def _relaxed_contexts(
        self,
        context: StudyContext,
    ):
        for field in self._RELAXABLE_FIELDS:
            value = getattr(context, field)
            if value in {None, ""}:
                continue
            yield (
                field,
                context.model_copy(
                    update={
                        field: "" if field == "disease_category" else None
                    }
                ),
            )
        for key, value in sorted(context.additional_qualifiers.items()):
            if not value:
                continue
            remaining = dict(context.additional_qualifiers)
            remaining.pop(key, None)
            yield (
                f"additional_qualifiers.{key}",
                context.model_copy(
                    update={"additional_qualifiers": remaining}
                ),
            )

