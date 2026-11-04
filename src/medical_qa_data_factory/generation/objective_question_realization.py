"""Deterministic realization gate for a frozen objective question."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import Field, model_validator

from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from .objective_question_contracts import (
    AnswerCardinality,
    DomainOutcome,
    ExecutionStatus,
    FrozenQuestionContract,
    SubagentResult,
    SubagentTask,
    VerificationVerdict,
)
from .subagent_contracts import (
    RoleCapability,
    RoleContractViolation,
    SubagentRole,
    default_objective_role_registry,
    validate_objective_subagent_task,
)


def objective_question_realization_capability() -> RoleCapability:
    return default_objective_role_registry()[
        SubagentRole.QUESTION_REALIZATION
    ]


class QuestionSemanticReadback(ContractModel):
    question_intent: str = Field(min_length=1)
    public_scope: str = Field(min_length=1)
    answer_type: str = Field(min_length=1)
    answer_values: list[str] = Field(min_length=1)
    answer_cardinality: AnswerCardinality
    claim_strength: str = Field(min_length=1)
    required_public_conditions: list[str] = Field(default_factory=list)


class ObjectiveQuestionDraft(ContractModel):
    draft_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    question_text: str = Field(min_length=1)
    answer_values: list[str] = Field(min_length=1)
    concise_explanation: str = Field(min_length=1)
    semantic_readback: QuestionSemanticReadback

    @model_validator(mode="after")
    def cardinality_matches_answers(self) -> "ObjectiveQuestionDraft":
        if (
            self.semantic_readback.answer_cardinality
            is AnswerCardinality.EXACTLY_ONE
            and len(self.answer_values) != 1
        ):
            raise ValueError("EXACTLY_ONE drafts require exactly one answer")
        return self


@dataclass(frozen=True)
class QuestionRealizationOutcome:
    result: SubagentResult
    draft: ObjectiveQuestionDraft


class ObjectiveQuestionRealizationSubagent:
    role = SubagentRole.QUESTION_REALIZATION

    def __init__(self, model: Any) -> None:
        self.model = model
        self.capability = objective_question_realization_capability()

    def run(
        self,
        task: SubagentTask,
        *,
        contract: FrozenQuestionContract,
    ) -> QuestionRealizationOutcome:
        validate_objective_subagent_task(task, self.capability)
        task_contract = FrozenQuestionContract.model_validate(
            task.goal["frozen_question_contract"]
        )
        if task_contract != contract:
            raise RoleContractViolation(
                "task contract does not match the frozen input contract"
            )
        total_usage: dict[str, Any] = {}
        previous_invalid_draft: dict[str, Any] | None = None
        validation_error: str | None = None
        draft: ObjectiveQuestionDraft | None = None
        for attempt in range(3):
            payload = {
                "frozen_question_contract": contract.model_dump(mode="json"),
                "design_requirements": dict(
                    task.goal.get("design_requirements", {})
                ),
                "instruction": (
                    "Write the final question and concise answer explanation. "
                    "Use natural standalone question wording, not research-task "
                    "commands such as 查明/确定/查询/阐明. Keep only the information "
                    "need fixed in the contract; do not append name meanings, "
                    "family explanations or mechanisms for apparent complexity. "
                    "The public question must not contain any answer value, "
                    "must not name a data source, and must not restate a "
                    "classification label that is the answer. "
                    "Return a semantic readback that preserves every frozen "
                    "field exactly."
                ),
            }
            if previous_invalid_draft is not None:
                payload["previous_invalid_draft"] = previous_invalid_draft
                payload["validation_error"] = validation_error
                payload["repair_instruction"] = (
                    "Rewrite only the wording needed to fix the reported error. "
                    "Do not alter any frozen semantic field or answer value."
                )
            response = self.model.generate_structured(
                StructuredGenerationRequest(
                    invocation_id=(
                        task.initial_attempt_id
                        + (f".repair-{attempt}" if attempt else "")
                    ),
                    role="objective_question_realization",
                    system_prompt_version=self.capability.prompt_version,
                    payload=payload,
                    idempotency_key=(
                        task.operation_key
                        + (f"|repair-{attempt}" if attempt else "")
                    ),
                ),
                ObjectiveQuestionDraft,
            )
            for key, value in response.usage.items():
                if isinstance(value, (int, float)) and not isinstance(
                    value, bool
                ):
                    total_usage[key] = total_usage.get(key, 0) + value
                else:
                    total_usage[key] = value
            candidate = ObjectiveQuestionDraft.model_validate(response.parsed)
            try:
                if candidate.contract_id != contract.contract_id:
                    raise RoleContractViolation(
                        "draft references another contract"
                    )
                self._validate_semantics(candidate, contract)
            except RoleContractViolation as exc:
                previous_invalid_draft = candidate.model_dump(mode="json")
                validation_error = str(exc)
                if attempt == 2:
                    raise
                continue
            draft = candidate
            break
        if draft is None:  # pragma: no cover - loop either returns or raises
            raise AssertionError("realization repair loop produced no draft")
        draft = draft.model_copy(
            update={"draft_id": f"draft.{contract.contract_id}"}
        )
        result = SubagentResult(
            subtask_id=task.subtask_id,
            execution_status=ExecutionStatus.SUCCEEDED,
            domain_outcome=DomainOutcome.NOT_APPLICABLE,
            verdict=VerificationVerdict.NOT_EMITTED,
            output_artifact_ids=[draft.draft_id],
            evidence_need_ids=[],
            usage=total_usage,
            error_category=None,
            stop_reason="question_realized",
        )
        return QuestionRealizationOutcome(result=result, draft=draft)

    @staticmethod
    def _validate_semantics(
        draft: ObjectiveQuestionDraft,
        contract: FrozenQuestionContract,
    ) -> None:
        lowered = draft.question_text.lower()
        source_terms = (
            "primekg",
            "ctd",
            "rag",
            "database",
            "knowledge graph",
            "知识图谱",
            "数据库",
        )
        if any(term in lowered for term in source_terms):
            raise RoleContractViolation("SOURCE_RELATIVE_WORDING")
        meta_terms = (
            "current research",
            "research support",
            "literature mention",
            "existing evidence",
            "现有研究",
            "文献是否",
            "根据当前证据",
        )
        if any(term in lowered for term in meta_terms):
            raise RoleContractViolation("META_EVIDENCE_WORDING")
        readback = draft.semantic_readback
        expected = {
            "question_intent": contract.question_intent,
            "public_scope": contract.public_scope,
            "answer_type": contract.answer_type,
            "claim_strength": contract.claim_strength,
            "answer_cardinality": contract.answer_cardinality,
            "required_public_conditions": sorted(
                contract.required_public_conditions
            ),
        }
        actual = {
            "question_intent": readback.question_intent,
            "public_scope": readback.public_scope,
            "answer_type": readback.answer_type,
            "claim_strength": readback.claim_strength,
            "answer_cardinality": readback.answer_cardinality,
            "required_public_conditions": sorted(
                readback.required_public_conditions
            ),
        }
        for field_name in (
            "question_intent",
            "public_scope",
            "answer_type",
            "claim_strength",
            "answer_cardinality",
            "required_public_conditions",
        ):
            if actual[field_name] != expected[field_name]:
                raise RoleContractViolation(
                    f"writer changed frozen {field_name}"
                )
        if sorted(draft.answer_values) != sorted(contract.answer_values):
            raise RoleContractViolation("writer changed frozen answer_values")
        if sorted(readback.answer_values) != sorted(contract.answer_values):
            raise RoleContractViolation(
                "semantic readback changed answer_values"
            )
