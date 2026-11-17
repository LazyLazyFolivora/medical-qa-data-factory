"""Structured model invocation contracts.

Writer and verifier share one model configuration but must use distinct roles
and distinct contexts, which is why ``role`` is part of every request.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ..domain.common import ContractModel


ModelRole = Literal[
    "objective_claim_entailment",
    "objective_research_agent",
    "objective_reading_summary",
    "objective_question_discovery",
    "objective_question_opportunity",
    "objective_question_reader",
    "objective_question_form_reader",
    "objective_training_demonstrator",
    "objective_training_trace_review",
    "objective_grounded_answer",
    "objective_answer_source_reading",
    "objective_react_agent",
    "objective_difficulty_assessment",
    "question_method_pilot",
    "exploration_planner",
    "entity_question_exploration_planner",
    "evidence_curator",
    "sufficiency_judge",
    "claim_builder",
    "question_planner",
    "question_writer",
    "style_judge",
    "answer_verifier",
    "material_retrieval",
    "fact_understanding",
    "objective_question_design",
    "objective_candidate_review",
    "objective_question_realization",
    "objective_answer_verification",
    "objective_release_review",
    "objective_main_agent",
    "objective_workflow_planner",
    "objective_history_summary",
    "objective_evidence_synthesis",
]


#: Roles that must never share a prompt version or context with each other.
#: The writer must not see the verifier's reasoning, and the verifier must
#: never receive the writer's free-text rationale as a source of fact.
SEPARATED_ROLE_PAIRS: tuple[tuple[str, str], ...] = (
    ("question_writer", "answer_verifier"),
    ("question_writer", "style_judge"),
    ("fact_understanding", "objective_question_design"),
    ("objective_question_design", "objective_question_realization"),
    ("objective_question_design", "objective_answer_verification"),
    ("objective_answer_verification", "objective_release_review"),
)


class StructuredGenerationRequest(ContractModel):
    invocation_id: str = Field(min_length=1)
    role: ModelRole
    system_prompt_version: str = Field(min_length=1)
    skill_id: str | None = None
    skill_version: str | None = None
    skill_instruction: str | None = None
    payload: dict
    idempotency_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def skill_fields_are_coherent(self) -> "StructuredGenerationRequest":
        if any((self.skill_id, self.skill_version, self.skill_instruction)):
            if not all((self.skill_id, self.skill_version, self.skill_instruction)):
                raise ValueError(
                    "skill_id, skill_version and skill_instruction must appear together"
                )
        else:
            from .objective_skills import load_objective_skill

            bundle = load_objective_skill(self.role, self.payload)
            if bundle is not None:
                self.skill_id, self.skill_version, self.skill_instruction = bundle
                self.idempotency_key += f"|skill:{self.skill_version}"
        return self


class StructuredGenerationResult(ContractModel):
    invocation_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    parsed: dict = Field(default_factory=dict)
    usage: dict = Field(default_factory=dict)
    model_config_id: str = Field(min_length=1)
