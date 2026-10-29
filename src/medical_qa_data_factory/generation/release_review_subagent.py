"""Two-stage release review and deterministic objective report merger."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from pydantic import Field, model_validator

from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from .objective_question_contracts import (
    AnswerEvidenceAuditResult,
    AnswerResolution,
    AnswerStatus,
    FrozenQuestionContract,
    ObjectiveVerdict,
    ObjectiveValidationReport,
    PublicQuestionAuditResult,
    canonical_digest,
    text_digest,
)
from .subagent_contracts import (
    RoleCapability,
    RoleContractViolation,
    SubagentRole,
    build_objective_subagent_task,
)


DEFAULT_VALIDATION_SKILL = (
    Path(__file__).resolve().parents[3]
    / "skills"
    / "objective-question-validator"
    / "SKILL.md"
)


@dataclass(frozen=True)
class ValidationSkill:
    skill_id: str
    content: str
    digest: str
    version: str


def load_validation_skill(
    path: str | Path = DEFAULT_VALIDATION_SKILL,
) -> ValidationSkill:
    skill_path = Path(path)
    content = skill_path.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError("objective question validation skill is empty")
    digest = text_digest(content)
    return ValidationSkill(
        skill_id="objective-question-validator",
        content=content,
        digest=digest,
        version=f"sha256:{digest}",
    )


class PublicQuestionPackage(ContractModel):
    question_id: str = Field(min_length=1)
    question_text: str = Field(min_length=1)

    def digest(self) -> str:
        return text_digest(self.question_text)


class IndependentEvidenceAuditPackage(ContractModel):
    evidence_used: list[str] = Field(min_length=1)
    anchors: list[dict[str, Any]] = Field(min_length=1)

    def digest(self) -> str:
        return release_evidence_digest(self.model_dump(mode="json"))


def release_evidence_digest(payload: Any) -> str:
    return canonical_digest(payload)


class AnswerEvidenceAuditPackage(ContractModel):
    question: PublicQuestionPackage
    frozen_question_contract: FrozenQuestionContract
    answer_resolution: AnswerResolution
    independent_evidence: IndependentEvidenceAuditPackage

    @model_validator(mode="after")
    def answer_and_contract_are_the_same_verified_fact(self) -> "AnswerEvidenceAuditPackage":
        if self.answer_resolution.status is not AnswerStatus.VERIFIED:
            raise ValueError(
                "answer evidence audit requires a VERIFIED resolution"
            )
        if (
            self.frozen_question_contract.resolution_id
            != self.answer_resolution.resolution_id
        ):
            raise ValueError("frozen contract references another resolution")
        if sorted(self.frozen_question_contract.answer_values) != sorted(
            self.answer_resolution.verified_answer_values
        ):
            raise ValueError("contract and resolution answers differ")
        return self


def _release_capability(
    *,
    output_schema: str,
    allowed_fields: set[str],
    stage_version: str,
) -> RoleCapability:
    return RoleCapability(
        role=SubagentRole.RELEASE_REVIEW,
        role_version=stage_version,
        model_role="objective_release_review",
        prompt_version="objective-release-review-v1",
        output_schema=output_schema,
        allowed_input_fields=frozenset(allowed_fields),
        forbidden_input_fields=frozenset(
            {
                "proposed_answer_values",
                "discovery_anchor_ids",
                "fact_explanation",
                "gold_answer",
                "release_allowed",
            }
        ),
        allowed_tools=frozenset(),
    )


@dataclass(frozen=True)
class ReleaseReviewOutcome:
    report: ObjectiveValidationReport
    public_audit: PublicQuestionAuditResult | None
    answer_audit: AnswerEvidenceAuditResult | None


class ReleaseReviewSubagent:
    role = SubagentRole.RELEASE_REVIEW

    def __init__(self, model: Any) -> None:
        self.model = model

    def run(
        self,
        *,
        public_question: PublicQuestionPackage,
        answer_evidence: AnswerEvidenceAuditPackage,
        skill_path: str | Path = DEFAULT_VALIDATION_SKILL,
    ) -> ObjectiveValidationReport:
        outcome = self.run_with_stages(
            public_question=public_question,
            answer_evidence=answer_evidence,
            skill_path=skill_path,
        )
        return outcome.report

    def run_with_stages(
        self,
        *,
        public_question: PublicQuestionPackage,
        answer_evidence: AnswerEvidenceAuditPackage,
        skill_path: str | Path = DEFAULT_VALIDATION_SKILL,
    ) -> ReleaseReviewOutcome:
        skill = load_validation_skill(skill_path)
        if answer_evidence.question != public_question:
            raise RoleContractViolation(
                "answer evidence package references another public question"
            )
        public_capability = _release_capability(
            output_schema="PublicQuestionAuditResult",
            allowed_fields={"public_question"},
            stage_version="objective-release-public-v1",
        )
        public_task = build_objective_subagent_task(
            task_id="subtask-public",
            role=SubagentRole.RELEASE_REVIEW,
            capability=public_capability,
            goal={"public_question": public_question.model_dump(mode="json")},
            input_artifact_ids=[public_question.question_id],
        )
        try:
            public_response = self._generate(
                stage="public_question_audit",
                task_id=public_task.subtask_id,
                operation_key=public_task.operation_key,
                invocation_id=f"{public_task.initial_attempt_id}.public",
                skill=skill,
                payload={
                    "stage": "public_question_audit",
                    "public_question": public_question.model_dump(mode="json"),
                    "public_question_digest": public_question.digest(),
                    "instruction": (
                        "Audit only the exact public question. You do not have "
                        "the answer, contract, evidence or internal metadata."
                    ),
                },
                response_model=PublicQuestionAuditResult,
            )
        except Exception as exc:
            return ReleaseReviewOutcome(
                report=self._failure_report(
                    public_question,
                    answer_evidence,
                    skill,
                    reason="MODEL_OUTPUT_INVALID",
                    detail=str(exc),
                ),
                public_audit=None,
                answer_audit=None,
            )
        public_audit = PublicQuestionAuditResult.model_validate(
            public_response.parsed
        ).model_copy(
            update={"task_id": public_task.subtask_id}
        )
        if public_audit.public_question_digest != public_question.digest():
            return ReleaseReviewOutcome(
                report=self._failure_report(
                    public_question,
                    answer_evidence,
                    skill,
                    reason="STALE_INPUT",
                    detail="public audit digest does not match the question",
                ),
                public_audit=public_audit,
                answer_audit=None,
            )
        if public_audit.verdict is not ObjectiveVerdict.PASS:
            report = merge_objective_audits(
                question_id=public_question.question_id,
                public_audit=public_audit,
                answer_audit=None,
                current_public_question_digest=public_question.digest(),
                current_contract_digest=(
                    answer_evidence.frozen_question_contract.digest()
                ),
                current_independent_evidence_digest=(
                    answer_evidence.independent_evidence.digest()
                ),
                audited_skill_digest=skill.digest,
                current_skill_digest=skill.digest,
                evidence_used=answer_evidence.independent_evidence.evidence_used,
            )
            return ReleaseReviewOutcome(
                report=report,
                public_audit=public_audit,
                answer_audit=None,
            )

        answer_capability = _release_capability(
            output_schema="AnswerEvidenceAuditResult",
            allowed_fields={
                "public_question",
                "public_audit_result",
                "frozen_question_contract",
                "answer_resolution",
                "independent_evidence",
            },
            stage_version="objective-release-answer-v1",
        )
        answer_task = build_objective_subagent_task(
            task_id="subtask-answer",
            role=SubagentRole.RELEASE_REVIEW,
            capability=answer_capability,
            goal={
                "public_question": public_question.model_dump(mode="json"),
                "public_audit_result": public_audit.model_dump(mode="json"),
                "frozen_question_contract": (
                    answer_evidence.frozen_question_contract.model_dump(
                        mode="json"
                    )
                ),
                "answer_resolution": (
                    answer_evidence.answer_resolution.model_dump(mode="json")
                ),
                "independent_evidence": (
                    answer_evidence.independent_evidence.model_dump(mode="json")
                ),
            },
            input_artifact_ids=[
                public_audit.task_id,
                answer_evidence.frozen_question_contract.contract_id,
                answer_evidence.answer_resolution.resolution_id,
            ],
            budget={"max_model_calls": 1, "max_provider_calls": 0},
        ).model_copy(
            update={
                "parent_decision_id": f"public-audit.{public_task.subtask_id}",
                "expected_state": "PUBLIC_QUESTION_AUDIT_FROZEN",
            }
        )
        try:
            answer_response = self._generate(
                stage="answer_evidence_audit",
                task_id=answer_task.subtask_id,
                operation_key=answer_task.operation_key,
                invocation_id=f"{answer_task.initial_attempt_id}.answer",
                skill=skill,
                payload={
                    "stage": "answer_evidence_audit",
                    "public_question": public_question.model_dump(mode="json"),
                    "public_question_audit": public_audit.model_dump(mode="json"),
                    "frozen_question_contract": (
                        answer_evidence.frozen_question_contract.model_dump(
                            mode="json"
                        )
                    ),
                    "answer_resolution": (
                        answer_evidence.answer_resolution.model_dump(mode="json")
                    ),
                    "independent_evidence": (
                        answer_evidence.independent_evidence.model_dump(
                            mode="json"
                        )
                    ),
                    "instruction": (
                        "Audit the answer and evidence only after the frozen "
                        "public result. Do not rewrite or improve the question."
                    ),
                },
                response_model=AnswerEvidenceAuditResult,
            )
        except Exception as exc:
            return ReleaseReviewOutcome(
                report=self._failure_report(
                    public_question,
                    answer_evidence,
                    skill,
                    reason="MODEL_OUTPUT_INVALID",
                    detail=str(exc),
                ),
                public_audit=public_audit,
                answer_audit=None,
            )
        answer_audit = AnswerEvidenceAuditResult.model_validate(
            answer_response.parsed
        ).model_copy(
            update={"task_id": answer_task.subtask_id}
        )
        report = merge_objective_audits(
            question_id=public_question.question_id,
            public_audit=public_audit,
            answer_audit=answer_audit,
            current_public_question_digest=public_question.digest(),
            current_contract_digest=(
                answer_evidence.frozen_question_contract.digest()
            ),
            current_independent_evidence_digest=(
                answer_evidence.independent_evidence.digest()
            ),
            audited_skill_digest=skill.digest,
            current_skill_digest=skill.digest,
            evidence_used=answer_evidence.independent_evidence.evidence_used,
        )
        return ReleaseReviewOutcome(
            report=report,
            public_audit=public_audit,
            answer_audit=answer_audit,
        )

    def _generate(
        self,
        *,
        stage: str,
        task_id: str,
        operation_key: str,
        invocation_id: str,
        skill: ValidationSkill,
        payload: dict[str, Any],
        response_model: type[ContractModel],
    ):
        return self.model.generate_structured(
            StructuredGenerationRequest(
                invocation_id=invocation_id,
                role="objective_release_review",
                system_prompt_version="objective-release-review-v1",
                skill_id=skill.skill_id,
                skill_version=skill.version,
                skill_instruction=skill.content,
                payload=payload,
                idempotency_key=f"{operation_key}|{stage}",
            ),
            response_model,
        )

    @staticmethod
    def _failure_report(
        public_question: PublicQuestionPackage,
        answer_evidence: AnswerEvidenceAuditPackage,
        skill: ValidationSkill,
        *,
        reason: str,
        detail: str,
    ) -> ObjectiveValidationReport:
        return ObjectiveValidationReport(
            report_id=f"validation.{public_question.question_id}",
            question_id=public_question.question_id,
            verdict=ObjectiveVerdict.INDETERMINATE,
            release_allowed=False,
            primary_reason=reason,
            secondary_reasons=[],
            public_question_audit_id="not_created",
            answer_audit_id="not_created",
            blocking_findings=[detail],
            evidence_used=answer_evidence.independent_evidence.evidence_used,
            validator_version=(
                f"objective-question-validator@sha256:{skill.digest}"
            ),
            input_digest=canonical_digest(
                {
                    "public_question": public_question.digest(),
                    "contract": answer_evidence.frozen_question_contract.digest(),
                    "evidence": (
                        answer_evidence.independent_evidence.digest()
                    ),
                    "skill": skill.digest,
                }
            ),
        )


def merge_objective_audits(
    *,
    question_id: str,
    public_audit: PublicQuestionAuditResult,
    answer_audit: AnswerEvidenceAuditResult | None,
    current_public_question_digest: str,
    current_contract_digest: str,
    current_independent_evidence_digest: str,
    audited_skill_digest: str,
    current_skill_digest: str,
    evidence_used: Sequence[str],
) -> ObjectiveValidationReport:
    if audited_skill_digest != current_skill_digest:
        verdict = ObjectiveVerdict.INDETERMINATE
        reason = "STALE_SKILL"
        blocking = ["validation skill changed after the audit"]
    elif public_audit.public_question_digest != current_public_question_digest:
        verdict = ObjectiveVerdict.INDETERMINATE
        reason = "STALE_INPUT"
        blocking = ["public question changed after the audit"]
    elif public_audit.verdict is not ObjectiveVerdict.PASS:
        verdict = public_audit.verdict
        reason = public_audit.primary_reason
        blocking = [
            "public question audit did not PASS",
            *public_audit.secondary_reasons,
        ]
    elif answer_audit is None:
        verdict = ObjectiveVerdict.INDETERMINATE
        reason = "ANSWER_AUDIT_MISSING"
        blocking = ["answer evidence audit was not completed"]
    elif (
        answer_audit.public_question_digest
        != current_public_question_digest
    ):
        verdict = ObjectiveVerdict.INDETERMINATE
        reason = "STALE_INPUT"
        blocking = ["answer audit belongs to another public question"]
    elif (
        answer_audit.question_answer_contract_digest
        != current_contract_digest
    ):
        verdict = ObjectiveVerdict.INDETERMINATE
        reason = "STALE_INPUT"
        blocking = ["frozen question contract changed after the audit"]
    elif (
        answer_audit.independent_evidence_digest
        != current_independent_evidence_digest
    ):
        verdict = ObjectiveVerdict.INDETERMINATE
        reason = "STALE_INPUT"
        blocking = ["independent evidence package changed after the audit"]
    elif answer_audit.verdict is not ObjectiveVerdict.PASS:
        verdict = answer_audit.verdict
        reason = answer_audit.primary_reason
        blocking = [
            "answer evidence audit did not PASS",
            *answer_audit.secondary_reasons,
        ]
    else:
        public_gates = {
            "self_contained": public_audit.self_contained,
            "source_independent": public_audit.source_independent,
            "asks_underlying_fact": public_audit.asks_underlying_fact,
            "entity_unambiguous": public_audit.entity_unambiguous,
            "cardinality_clear": public_audit.cardinality_clear,
        }
        answer_gates = {
            "independently_supported": answer_audit.independently_supported,
            "unique_or_closed": answer_audit.unique_or_closed,
            "contradiction_checked": answer_audit.contradiction_checked,
            "relation_strength_preserved": (
                answer_audit.relation_strength_preserved
            ),
        }
        blocking = [
            name
            for name, passed in {**public_gates, **answer_gates}.items()
            if not passed
        ]
        if blocking:
            verdict = ObjectiveVerdict.REJECT
            reason = "HARD_GATE_FAILED"
        else:
            verdict = ObjectiveVerdict.PASS
            reason = "PASS"
    combined_digest = canonical_digest(
        {
            "public_question": current_public_question_digest,
            "contract": current_contract_digest,
            "evidence": current_independent_evidence_digest,
            "skill": current_skill_digest,
        }
    )
    return ObjectiveValidationReport(
        report_id=f"validation.{question_id}",
        question_id=question_id,
        verdict=verdict,
        release_allowed=verdict is ObjectiveVerdict.PASS,
        primary_reason=reason,
        secondary_reasons=(
            []
            if verdict is ObjectiveVerdict.PASS
            else [
                *public_audit.secondary_reasons,
                *(
                    answer_audit.secondary_reasons
                    if answer_audit is not None
                    else []
                ),
            ]
        ),
        public_question_audit_id=f"public-audit.{public_audit.task_id}",
        answer_audit_id=(
            f"answer-audit.{answer_audit.task_id}"
            if answer_audit is not None
            else "not_created"
        ),
        blocking_findings=blocking,
        evidence_used=sorted(set(evidence_used)),
        validator_version=(
            f"objective-question-validator@sha256:{current_skill_digest}"
        ),
        input_digest=combined_digest,
    )
