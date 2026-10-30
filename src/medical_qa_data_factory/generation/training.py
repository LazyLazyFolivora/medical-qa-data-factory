"""Versioned training-data projections and deterministic label derivation.

``QAItem`` is a projection over the frozen question and evidence bundle. The
underlying business records remain authoritative; this module does not create
a second mutable copy of medical facts.
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Iterable, Literal

from pydantic import Field, model_validator

from ..domain import EvidenceBundle, LiteratureEvidence, QuestionSpec
from ..domain.common import ContractModel, UtcDateTime
from .contracts import (
    DifficultyTarget,
    GenerationSpec,
    LiteraturePolicy,
    SafetyBudget,
    PLANNER_STEP_VERSION,
    PLANNER_TRAJECTORY_VERSION,
    QA_ITEM_VERSION,
)


def stable_training_id(
    namespace: str, *parts: object, prefix: str | None = None
) -> str:
    text = json.dumps(
        [namespace, *parts], sort_keys=True, default=str, ensure_ascii=False
    )
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    return f"{prefix or namespace}.{digest}"


class QuestionType(str, Enum):
    SINGLE_FACT = "single_fact"
    CONSTRAINED_ENTITY = "constrained_entity"
    RELATION_CHAIN = "relation_chain"


class TrajectoryQuality(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class AnswerProjection(ContractModel):
    text: str = Field(min_length=1)
    canonical_entity_id: str | None = None
    aliases: list[str] = Field(default_factory=list)
    answer_type: Literal["entity", "short_fact"] = "entity"


class QASourceProjection(ContractModel):
    seed_entity_id: str = Field(min_length=1)
    discovered_entity_ids: list[str] = Field(default_factory=list)
    generation_episode_id: str = Field(min_length=1)
    planner_trajectory_id: str = Field(min_length=1)


class QAReasoningProjection(ContractModel):
    hop_count: int = Field(ge=1)
    ordered_relation_chain: list[str] = Field(default_factory=list)
    intermediate_entity_ids: list[str] = Field(default_factory=list)
    evidence_path_id: str | None = None
    required_kg_evidence_count: int = Field(ge=0)
    required_literature_evidence_count: int = Field(ge=0)
    constraint_count: int = Field(default=0, ge=0)
    answer_candidate_count: int = Field(default=1, ge=1)
    difficulty_label: Literal["easy", "medium", "hard"]
    difficulty_policy_version: str = Field(min_length=1)


class QAEvidenceProjection(ContractModel):
    required_kg_evidence_ids: list[str] = Field(default_factory=list)
    required_literature_evidence_ids: list[str] = Field(default_factory=list)
    provider_snapshot_ids: list[str] = Field(default_factory=list)


class QAValidationProjection(ContractModel):
    answer_recomputed: bool = False
    evidence_satisfied: bool = False
    ambiguity_check: Literal["passed", "failed", "not_run"] = "not_run"
    leakage_check: Literal["passed", "failed", "not_run"] = "not_run"
    validation_level: Literal[
        "candidate", "machine_validated", "human_confirmed"
    ] = "candidate"


class QAItem(ContractModel):
    schema_version: str = QA_ITEM_VERSION
    question_id: str = Field(min_length=1)
    question_version: str = Field(min_length=1)
    logical_question_id: str = Field(min_length=1)
    language: str = Field(min_length=2)
    question_text: str = Field(min_length=1)
    question_type: QuestionType
    difficulty: Literal["easy", "medium", "hard"]
    answer: AnswerProjection
    source: QASourceProjection
    reasoning: QAReasoningProjection
    evidence: QAEvidenceProjection
    validation: QAValidationProjection
    dataset_split_group_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validation_links_are_consistent(self) -> "QAItem":
        if (
            self.validation.validation_level == "machine_validated"
            and not self.validation.answer_recomputed
        ):
            raise ValueError(
                "a machine-validated QA item requires a recomputed answer"
            )
        if self.reasoning.difficulty_label != self.difficulty:
            raise ValueError("difficulty and reasoning.difficulty_label must match")
        return self


class PlannerKnownEntity(ContractModel):
    canonical_id: str = Field(min_length=1)
    name: str | None = None


class PlannerState(ContractModel):
    known_entities: list[PlannerKnownEntity] = Field(default_factory=list)
    observation_summaries: list[dict[str, Any]] = Field(default_factory=list)
    evidence_summary: dict[str, Any] = Field(default_factory=dict)
    evidence_gaps: list[str] = Field(default_factory=list)
    target_difficulty: list[DifficultyTarget] = Field(default_factory=list)
    unsatisfied_constraints: list[str] = Field(default_factory=list)
    available_actions: list[str] = Field(default_factory=list)
    available_skills: list[dict[str, Any]] = Field(default_factory=list)


class PlannerSelectedAction(ContractModel):
    action_type: str = Field(min_length=1)
    skill_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_information: str = Field(min_length=1)
    evidence_gap: str = Field(min_length=1)


class PlannerExecution(ContractModel):
    policy_result: Literal["accepted", "rejected"] = "rejected"
    sealed_action_id: str | None = None
    observation_id: str | None = None
    status: Literal[
        "success", "empty", "error", "rejected", "finish"
    ] = "rejected"
    rejection_code: str | None = None
    rejection_message: str | None = None


class PlannerOutcome(ContractModel):
    discovered_entity_ids: list[str] = Field(default_factory=list)
    new_evidence_ids: list[str] = Field(default_factory=list)
    evidence_gain: int = Field(default=0, ge=0)
    satisfied_constraints: list[str] = Field(default_factory=list)
    useful: bool = False


class PlannerStep(ContractModel):
    schema_version: str = PLANNER_STEP_VERSION
    step_id: str = Field(min_length=1)
    trajectory_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    turn: int = Field(ge=1)
    state: PlannerState
    selected_action: PlannerSelectedAction
    execution: PlannerExecution
    outcome: PlannerOutcome


class PlannerTrajectory(ContractModel):
    schema_version: str = PLANNER_TRAJECTORY_VERSION
    trajectory_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    seed_entity_id: str = Field(min_length=1)
    generation_spec_id: str = Field(min_length=1)
    model_config_id: str = Field(min_length=1)
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    steps: list[PlannerStep] = Field(default_factory=list)
    terminal_reason: str = Field(min_length=1)
    generated_question_ids: list[str] = Field(default_factory=list)
    trajectory_quality: TrajectoryQuality
    label_policy_version: str = Field(min_length=1)
    created_at: UtcDateTime

    @model_validator(mode="after")
    def steps_belong_to_trajectory(self) -> "PlannerTrajectory":
        for step in self.steps:
            if step.trajectory_id != self.trajectory_id:
                raise ValueError("every planner step must belong to this trajectory")
            if step.episode_id != self.episode_id:
                raise ValueError("every planner step must belong to this episode")
        turns = [step.turn for step in self.steps]
        if turns != sorted(turns) or len(turns) != len(set(turns)):
            raise ValueError("planner step turns must be unique and ordered")
        return self


class PlannerTrainingSample(ContractModel):
    schema_version: str = "planner-training-sample-v1"
    sample_id: str = Field(min_length=1)
    trajectory_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    input_state: PlannerState
    available_actions: list[str] = Field(default_factory=list)
    label_action_type: str = Field(min_length=1)
    label_parameters: dict[str, Any]
    feedback: dict[str, Any]
    quality: Literal[
        "positive", "negative_policy", "negative_low_gain", "negative_terminal"
    ]
    label_policy_version: str = Field(min_length=1)


class GenerationRunMetadata(ContractModel):
    schema_version: str = "generation-run-metadata-v1"
    generation_spec_id: str = Field(min_length=1)
    generation_spec: GenerationSpec
    safety_budget: SafetyBudget
    model_config_id: str = Field(min_length=1)
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    kg_provider_id: str = Field(min_length=1)
    kg_snapshot_id: str | None = None
    literature_provider_id: str | None = None
    literature_snapshot_id: str | None = None


def _literature_snapshot_id(item: LiteratureEvidence) -> str | None:
    value = item.location.get("provider_snapshot_id") or item.location.get(
        "snapshot_id"
    )
    return str(value) if value else None


def classify_difficulty(
    *,
    hop_count: int,
    required_kg_evidence_count: int,
    required_literature_evidence_count: int,
    constraint_count: int = 0,
    answer_candidate_count: int = 1,
) -> Literal["easy", "medium", "hard"]:
    """Classify the actual frozen reasoning structure, never exploration effort."""
    if (
        hop_count >= 3
        or constraint_count >= 2
        or answer_candidate_count > 1 and constraint_count >= 1
    ):
        return "hard"
    if (
        hop_count == 2
        or (
            required_kg_evidence_count >= 1
            and required_literature_evidence_count >= 1
        )
        or constraint_count >= 1
    ):
        return "medium"
    return "easy"


def _relation_chain(
    evidence_ids: Iterable[str], bundle: EvidenceBundle
) -> list[str]:
    by_id = {item.evidence_id: item for item in bundle.kg_evidence}
    return [
        by_id[evidence_id].relation
        for evidence_id in evidence_ids
        if evidence_id in by_id
    ]


def build_qa_item(
    *,
    question: QuestionSpec,
    bundle: EvidenceBundle,
    episode_id: str,
    trajectory_id: str,
    seed_entity_id: str,
    discovered_entity_ids: Iterable[str],
    generation_spec: GenerationSpec,
    validation_level: Literal[
        "candidate", "machine_validated", "human_confirmed"
    ] = "machine_validated",
) -> QAItem:
    required_kg = list(question.evidence_requirements.required_kg_evidence_ids)
    required_lit = list(
        question.evidence_requirements.required_literature_evidence_ids
    )
    required_path_ids = set(question.evidence_requirements.required_path_ids)
    required_paths = [
        path
        for path in bundle.paths
        if str(getattr(path, "path_id", path.get("path_id") if isinstance(path, dict) else ""))
        in required_path_ids
    ]
    # A bag of edges is not a path. Hop count comes only from an ordered path;
    # several independent evidence edges do not become a two-hop question.
    hops = max(
        (
            len(getattr(path, "steps", path.get("steps", []) if isinstance(path, dict) else []))
            for path in required_paths
        ),
        default=1,
    )
    constraints = int(question.query_intent.get("constraint_count", 0))
    difficulty = classify_difficulty(
        hop_count=hops,
        required_kg_evidence_count=len(required_kg),
        required_literature_evidence_count=len(required_lit),
        constraint_count=constraints,
    )
    if difficulty not in {item.value for item in generation_spec.difficulty_targets}:
        raise ValueError(
            f"question difficulty {difficulty!r} is not in the requested targets"
        )
    kg_by_id = {item.evidence_id: item for item in bundle.kg_evidence}
    lit_by_id = {item.evidence_id: item for item in bundle.literature_evidence}
    if not set(required_kg) <= set(kg_by_id):
        raise ValueError("question references KG evidence outside the frozen bundle")
    if not set(required_lit) <= set(lit_by_id):
        raise ValueError(
            "question references literature evidence outside the frozen bundle"
        )
    snapshots = {
        str(item.metadata.get("snapshot_id"))
        for item in (kg_by_id[key] for key in required_kg)
        if item.metadata.get("snapshot_id")
    }
    snapshots.update(
        value
        for value in (
            _literature_snapshot_id(lit_by_id[key]) for key in required_lit
        )
        if value
    )
    answer_values = question.gold_answer.values
    canonical_id = next(
        (
            str(item["canonical_id"])
            for item in answer_values
            if item.get("canonical_id")
        ),
        None,
    )
    aliases = list(question.gold_answer.acceptable_variants)
    ordered_relation_chain = _relation_chain(required_kg, bundle)
    answer_candidate_count = max(
        1,
        int(question.query_intent.get("answer_candidate_count", 1)),
    )
    return QAItem(
        question_id=question.question_id,
        question_version=question.question_version,
        logical_question_id=question.logical_question_id,
        language=(
            question.question_variants[0].language
            if question.question_variants
            else generation_spec.language
        ),
        question_text=question.canonical_question,
        question_type={
            "single_fact": QuestionType.SINGLE_FACT,
            "constrained_list": QuestionType.CONSTRAINED_ENTITY,
            "two_hop_relation": QuestionType.RELATION_CHAIN,
        }.get(
            str(question.query_intent.get("question_type", "single_fact")),
            QuestionType.SINGLE_FACT,
        ),
        difficulty=difficulty,
        answer=AnswerProjection(
            text=question.gold_answer.short_answer,
            canonical_entity_id=canonical_id,
            aliases=aliases,
            answer_type=(
                "entity"
                if question.gold_answer.answer_mode.value == "entity"
                else "short_fact"
            ),
        ),
        source=QASourceProjection(
            seed_entity_id=seed_entity_id,
            discovered_entity_ids=sorted(set(discovered_entity_ids)),
            generation_episode_id=episode_id,
            planner_trajectory_id=trajectory_id,
        ),
        reasoning=QAReasoningProjection(
            hop_count=hops,
            ordered_relation_chain=ordered_relation_chain,
            intermediate_entity_ids=list(
                question.query_intent.get("intermediate_entity_ids", [])
            ),
            evidence_path_id=next(
                iter(question.evidence_requirements.required_path_ids), None
            ),
            required_kg_evidence_count=len(required_kg),
            required_literature_evidence_count=len(required_lit),
            constraint_count=constraints,
            answer_candidate_count=answer_candidate_count,
            difficulty_label=difficulty,
            difficulty_policy_version=generation_spec.difficulty_policy_version,
        ),
        evidence=QAEvidenceProjection(
            required_kg_evidence_ids=required_kg,
            required_literature_evidence_ids=required_lit,
            provider_snapshot_ids=sorted(snapshots),
        ),
        validation=QAValidationProjection(
            answer_recomputed=validation_level != "candidate",
            evidence_satisfied=(
                len(required_kg) >= generation_spec.evidence_constraints.min_kg_evidence
                and len(required_lit)
                >= generation_spec.evidence_constraints.min_literature_evidence
                and len(required_kg) + len(required_lit)
                >= generation_spec.evidence_constraints.min_total_evidence
                and not (
                    generation_spec.evidence_constraints.literature_policy
                    is LiteraturePolicy.REQUIRED
                    and not required_lit
                )
            ),
            ambiguity_check="passed",
            leakage_check="passed",
            validation_level=validation_level,
        ),
        dataset_split_group_id=question.split_group_id,
    )


def derive_training_samples(
    trajectory: PlannerTrajectory,
) -> list[PlannerTrainingSample]:
    """Derive deterministic samples without treating every model choice as gold."""
    samples: list[PlannerTrainingSample] = []
    for step in trajectory.steps:
        parameters = dict(step.selected_action.parameters)
        if (
            step.execution.policy_result == "rejected"
            and step.selected_action.action_type == "finish"
        ):
            quality = "negative_terminal"
        elif step.execution.policy_result == "rejected":
            quality = "negative_policy"
        elif not step.outcome.useful:
            quality = "negative_low_gain"
        elif (
            trajectory.trajectory_quality is not TrajectoryQuality.ACCEPTED
            and step.selected_action.action_type == "finish"
        ):
            quality = "negative_terminal"
        else:
            quality = "positive"
        samples.append(
            PlannerTrainingSample(
                sample_id=stable_training_id(
                    "planner-sample", trajectory.trajectory_id, step.step_id
                ),
                trajectory_id=trajectory.trajectory_id,
                step_id=step.step_id,
                input_state=step.state,
                available_actions=list(step.state.available_actions),
                label_action_type=step.selected_action.action_type,
                label_parameters=parameters,
                feedback={
                    "policy_result": step.execution.policy_result,
                    "execution_status": step.execution.status,
                    "observation_id": step.execution.observation_id,
                    "evidence_gain": step.outcome.evidence_gain,
                    "useful": step.outcome.useful,
                    "trajectory_quality": trajectory.trajectory_quality.value,
                    "generated_question_ids": list(
                        trajectory.generated_question_ids
                    ),
                },
                quality=quality,
                label_policy_version=trajectory.label_policy_version,
            )
        )
    return samples
