"""Candidate question design from already-persisted candidate claims."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from pydantic import Field

from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from .objective_question_contracts import (
    CandidateClaim,
    DomainOutcome,
    EvidenceAnchor,
    ExecutionStatus,
    QuestionHypothesis,
    SubagentResult,
    SubagentTask,
    VerificationVerdict,
)
from .subagent_contracts import (
    RoleContractViolation,
    SubagentRole,
    default_objective_role_registry,
    validate_objective_subagent_task,
)


class QuestionHypothesisBatch(ContractModel):
    hypotheses: list[QuestionHypothesis] = Field(min_length=1)


@dataclass(frozen=True)
class QuestionDesignOutcome:
    result: SubagentResult
    hypotheses: list[QuestionHypothesis]


class ObjectiveQuestionDesignSubagent:
    role = SubagentRole.QUESTION_DESIGN

    def __init__(self, model: Any) -> None:
        self.model = model
        self.capability = default_objective_role_registry()[self.role]

    def run(
        self,
        task: SubagentTask,
        *,
        claims: Sequence[CandidateClaim],
        anchors: Sequence[EvidenceAnchor],
    ) -> QuestionDesignOutcome:
        validate_objective_subagent_task(task, self.capability)
        claim_ids = [claim.claim_id for claim in claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise RoleContractViolation("candidate claim IDs must be unique")
        anchor_ids = {anchor.anchor_id for anchor in anchors}
        if any(
            anchor_id not in anchor_ids
            for claim in claims
            for anchor_id in claim.supporting_anchor_ids
        ):
            raise RoleContractViolation(
                "candidate claim references an anchor hidden from question design"
            )
        payload = {
            "task": {
                "target_entity": task.goal["target_entity"],
                "allowed_fact_types": list(
                    task.goal.get("allowed_fact_types", [])
                ),
                "question_count_upper_bound": int(
                    task.goal.get("question_count_upper_bound", 1)
                ),
                "difficulty_targets": list(
                    task.goal.get("difficulty_targets", [])
                ),
                "style_targets": list(task.goal.get("style_targets", [])),
            },
            "candidate_claims": [
                claim.model_dump(mode="json") for claim in claims
            ],
            "available_anchor_locations": [
                {
                    "anchor_id": anchor.anchor_id,
                    "material_id": anchor.material_id,
                    "document_id": anchor.document_id,
                    "section_or_field": anchor.section_or_field,
                }
                for anchor in anchors
            ],
            "instruction": (
                "Treat proposed answers as candidates only. Do not change any "
                "candidate claim or claim that an answer is verified."
            ),
        }
        response = self.model.generate_structured(
            StructuredGenerationRequest(
                invocation_id=task.initial_attempt_id,
                role="objective_question_design",
                system_prompt_version=self.capability.prompt_version,
                payload=payload,
                idempotency_key=task.operation_key,
            ),
            QuestionHypothesisBatch,
        )
        batch = QuestionHypothesisBatch.model_validate(response.parsed)
        visible_claim_ids = set(claim_ids)
        claim_by_id = {claim.claim_id: claim for claim in claims}
        for hypothesis in batch.hypotheses:
            if unknown_claims := sorted(
                set(hypothesis.candidate_claim_ids) - visible_claim_ids
            ):
                raise RoleContractViolation(
                    f"hypothesis references unknown claims: {unknown_claims}"
                )
            supporting_anchor_ids = {
                anchor_id
                for claim_id in hypothesis.candidate_claim_ids
                for anchor_id in claim_by_id[claim_id].supporting_anchor_ids
            }
            if invalid_anchors := sorted(
                set(hypothesis.discovery_anchor_ids)
                - supporting_anchor_ids
            ):
                raise RoleContractViolation(
                    "hypothesis discovery_anchor_ids are not supporting anchors: "
                    f"{invalid_anchors}"
                )
        result = SubagentResult(
            subtask_id=task.subtask_id,
            execution_status=ExecutionStatus.SUCCEEDED,
            domain_outcome=DomainOutcome.NOT_APPLICABLE,
            verdict=VerificationVerdict.NOT_EMITTED,
            output_artifact_ids=[
                hypothesis.hypothesis_id for hypothesis in batch.hypotheses
            ],
            evidence_need_ids=[],
            usage=dict(response.usage),
            error_category=None,
            stop_reason="hypotheses_proposed",
        )
        return QuestionDesignOutcome(
            result=result,
            hypotheses=list(batch.hypotheses),
        )
