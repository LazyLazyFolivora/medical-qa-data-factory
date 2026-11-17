"""Independent difficulty grading after answer verification."""
from typing import Literal
from pydantic import Field, ValidationError
from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from ..llm.errors import ModelError, ModelErrorCode
from .verification_evidence_view import generate_with_evidence


class DifficultyAssessment(ContractModel):
    assessed_difficulty: Literal["easy", "medium", "hard"]
    shortest_solution_steps: list[str] = Field(min_length=1)
    necessary_integrations: list[str] = Field(default_factory=list)
    shortcuts: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)


class DifficultyAssessmentError(ModelError):
    """A grading failure must not consume the candidate's rejection budget."""

    def __init__(self, cause):
        super().__init__(
            cause.code if isinstance(cause, ModelError) else ModelErrorCode.SCHEMA_ERROR,
            f"独立难度评估未完成，题目等待恢复。{cause}",
            retryable=getattr(cause, "retryable", False),
            status_code=getattr(cause, "status_code", None),
        )


def assess_difficulty(model, *, question, answer, anchors, planner_environment, operation_key):
    # Deliberately omit target difficulty, generating strategy and writer grade.
    request = StructuredGenerationRequest(
        invocation_id=f"{operation_key}.difficulty",
        idempotency_key=f"{operation_key}|difficulty-v1",
        role="objective_difficulty_assessment",
        system_prompt_version="difficulty-v1",
        payload={
            "question": question,
            "verified_answer": list(answer),
            "planner_environment": planner_environment,
            "evidence_anchors": [anchor.model_dump(mode="json", include={
                "anchor_id", "document_id", "quoted_text", "section_or_field"
            }) for anchor in anchors],
        },
    )
    try:
        response = generate_with_evidence(model, request, DifficultyAssessment, "evidence_anchors")
        assessment = DifficultyAssessment.model_validate(response.parsed)
    except (ModelError, ValidationError) as exc:
        raise DifficultyAssessmentError(exc) from exc
    usage = {**response.usage, "model_calls": response.usage.get("model_calls", 1)}
    return assessment, usage
