"""Role capabilities and deterministic construction of subagent tasks."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .objective_question_contracts import (
    SubagentTask,
    canonical_digest,
)


class SubagentRole(str, Enum):
    MATERIAL_RETRIEVAL = "material_retrieval"
    FACT_UNDERSTANDING = "fact_understanding"
    QUESTION_DESIGN = "objective_question_design"
    QUESTION_REALIZATION = "objective_question_realization"
    ANSWER_VERIFICATION = "objective_answer_verification"
    RELEASE_REVIEW = "objective_release_review"


class RoleContractViolation(ValueError):
    code = "role_contract_violation"


@dataclass(frozen=True)
class RoleCapability:
    role: SubagentRole
    role_version: str
    model_role: str
    prompt_version: str
    output_schema: str
    allowed_input_fields: frozenset[str]
    forbidden_input_fields: frozenset[str]
    allowed_tools: frozenset[str]


_COMMON_FORBIDDEN = frozenset(
    {
        "gold_answer",
        "verified_answer_values",
        "objective_validation_report",
        "release_allowed",
    }
)


def default_objective_role_registry() -> dict[
    SubagentRole, RoleCapability
]:
    return {
        SubagentRole.MATERIAL_RETRIEVAL: RoleCapability(
            role=SubagentRole.MATERIAL_RETRIEVAL,
            role_version="material-retrieval-v1",
            model_role="material_retrieval",
            prompt_version="material-retrieval-v1",
            output_schema="EvidenceProviderResult",
            allowed_input_fields=frozenset(
                {
                    "target_entity",
                    "evidence_need",
                    "allowed_providers",
                    "material_limit",
                    "snapshot_policy",
                }
            ),
            forbidden_input_fields=_COMMON_FORBIDDEN
            | frozenset(
                {
                    "proposed_answer_values",
                    "question_intent",
                    "candidate_claims",
                }
            ),
            allowed_tools=frozenset(
                {
                    "primekg",
                    "ctd",
                    "pubmed",
                    "rag",
                    "hgnc",
                    "ncbi_gene",
                    "ensembl",
                }
            ),
        ),
        SubagentRole.FACT_UNDERSTANDING: RoleCapability(
            role=SubagentRole.FACT_UNDERSTANDING,
            role_version="fact-understanding-v1",
            model_role="fact_understanding",
            prompt_version="fact-understanding-v1",
            output_schema="CandidateClaimBatch",
            allowed_input_fields=frozenset(
                {
                    "target_entity",
                    "allowed_fact_types",
                    "materials",
                    "anchors",
                    "entity_resolutions",
                }
            ),
            forbidden_input_fields=_COMMON_FORBIDDEN
            | frozenset(
                {
                    "question_intent",
                    "question_hypothesis",
                    "candidate_questions",
                    "proposed_answer_values",
                    "discovery_anchor_ids",
                }
            ),
            allowed_tools=frozenset(),
        ),
        SubagentRole.QUESTION_DESIGN: RoleCapability(
            role=SubagentRole.QUESTION_DESIGN,
            role_version="objective-question-design-v1",
            model_role="objective_question_design",
            prompt_version="objective-question-design-v1",
            output_schema="QuestionHypothesisBatch",
            allowed_input_fields=frozenset(
                {
                    "target_entity",
                    "allowed_fact_types",
                    "question_count_upper_bound",
                    "difficulty_targets",
                    "style_targets",
                    "candidate_claims",
                }
            ),
            forbidden_input_fields=_COMMON_FORBIDDEN
            | frozenset(
                {
                    "fact_explanation",
                    "untrusted_material",
                    "raw_content",
                    "materials",
                }
            ),
            allowed_tools=frozenset(),
        ),
        SubagentRole.QUESTION_REALIZATION: RoleCapability(
            role=SubagentRole.QUESTION_REALIZATION,
            role_version="objective-question-realization-v1",
            model_role="objective_question_realization",
            prompt_version="objective-question-realization-v1",
            output_schema="ObjectiveQuestionDraft",
            allowed_input_fields=frozenset(
                {
                    "frozen_question_contract",
                }
            ),
            forbidden_input_fields=_COMMON_FORBIDDEN
            | frozenset(
                {
                    "candidate_claims",
                    "fact_explanation",
                    "untrusted_material",
                    "raw_content",
                    "proposed_answer_values",
                    "discovery_anchor_ids",
                    "question_hypothesis",
                }
            ),
            allowed_tools=frozenset(),
        ),
        SubagentRole.ANSWER_VERIFICATION: RoleCapability(
            role=SubagentRole.ANSWER_VERIFICATION,
            role_version="objective-answer-verification-v1",
            model_role="objective_answer_verification",
            prompt_version="objective-answer-verification-v1",
            output_schema="AnswerResolution",
            allowed_input_fields=frozenset(
                {
                    "blind_answer_query",
                    "verification_plan",
                    "allowed_providers",
                }
            ),
            forbidden_input_fields=_COMMON_FORBIDDEN
            | frozenset(
                {
                    "proposed_answer_values",
                    "discovery_anchor_ids",
                    "question_hypothesis",
                    "fact_explanation",
                }
            ),
            allowed_tools=frozenset(
                {"primekg", "ctd", "pubmed", "rag"}
            ),
        ),
        SubagentRole.RELEASE_REVIEW: RoleCapability(
            role=SubagentRole.RELEASE_REVIEW,
            role_version="objective-release-review-v1",
            model_role="objective_release_review",
            prompt_version="objective-release-review-v1",
            output_schema="ObjectiveValidationReport",
            allowed_input_fields=frozenset(
                {
                    "public_question",
                    "question_answer_contract",
                    "independent_evidence",
                    "validation_skill",
                }
            ),
            forbidden_input_fields=frozenset(
                {
                    "proposed_answer_values",
                    "discovery_anchor_ids",
                    "fact_explanation",
                }
            ),
            allowed_tools=frozenset(),
        ),
    }


def build_objective_subagent_task(
    *,
    task_id: str,
    role: SubagentRole,
    capability: RoleCapability,
    goal: Mapping[str, Any],
    input_artifact_ids: list[str],
    runtime_bundle_id: str = "objective-question-runtime-v1",
    allowed_tools: list[str] | None = None,
    budget: Mapping[str, Any] | None = None,
) -> SubagentTask:
    if role is not capability.role:
        raise RoleContractViolation("task role does not match capability role")
    keys = _all_keys(goal)
    if forbidden := sorted(keys & capability.forbidden_input_fields):
        raise RoleContractViolation(
            f"{role.value} cannot see forbidden fields: {forbidden}"
        )
    if unknown := sorted(set(goal) - capability.allowed_input_fields):
        raise RoleContractViolation(
            f"{role.value} received unknown input fields: {unknown}"
        )
    tools = list(allowed_tools or [])
    if invalid_tools := sorted(set(tools) - capability.allowed_tools):
        raise RoleContractViolation(
            f"{role.value} cannot use tools: {invalid_tools}"
        )
    visible_fields = sorted(set(goal) & capability.allowed_input_fields)
    input_digest = canonical_digest(
        {
            "goal": dict(goal),
            "input_artifact_ids": list(input_artifact_ids),
            "visible_fields": visible_fields,
            "allowed_tools": tools,
        }
    )
    return SubagentTask(
        subtask_id=task_id,
        subtask_revision=0,
        child_slot_id=f"{task_id}.slot.0",
        parent_decision_id=f"{task_id}.parent",
        subagent_role=role.value,
        role_version=capability.role_version,
        goal=dict(goal),
        input_artifact_ids=list(input_artifact_ids),
        input_digest=input_digest,
        runtime_bundle_id=runtime_bundle_id,
        expected_state=f"{role.value.upper()}_PENDING",
        expected_state_revision=0,
        operation_key=f"{task_id}|{role.value}|v1",
        initial_attempt_id=f"{task_id}.attempt.1",
        visible_field_policy=visible_fields,
        allowed_tools=tools,
        budget=dict(
            budget
            or {
                "max_model_calls": 1,
                "max_provider_calls": 0,
            }
        ),
        stop_conditions=["structured_output_emitted", "budget_exhausted"],
        expected_output_schema=capability.output_schema,
    )


def validate_objective_subagent_task(
    task: SubagentTask,
    capability: RoleCapability,
) -> None:
    if task.subagent_role != capability.role.value:
        raise RoleContractViolation(
            f"task role {task.subagent_role!r} does not match "
            f"{capability.role.value!r}"
        )
    if task.role_version != capability.role_version:
        raise RoleContractViolation("task role version is incompatible")
    if task.expected_output_schema != capability.output_schema:
        raise RoleContractViolation("task output schema is incompatible")
    if any(field in task.visible_field_policy for field in capability.forbidden_input_fields):
        raise RoleContractViolation("task visibility policy exposes forbidden fields")
    if invalid_tools := sorted(set(task.allowed_tools) - capability.allowed_tools):
        raise RoleContractViolation(
            f"task requests forbidden tools: {invalid_tools}"
        )


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        keys = {str(key) for key in value}
        for item in value.values():
            keys.update(_all_keys(item))
        return keys
    if isinstance(value, list):
        keys: set[str] = set()
        for item in value:
            keys.update(_all_keys(item))
        return keys
    return set()
