"""Task-first contracts for constructing verifiable biomedical questions.

Candidate discovery is deliberately separated from task validation.  A
candidate may propose an answer, but only a complete validation report can
promote it to a :class:`ValidatedTask`; the authoritative answer set always
comes from the validation world.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator

from ..domain.common import ContractModel, UtcDateTime, utc_now
from .logical_query import LogicalQuery, QueryConstraint, QueryStep


TASK_CONTRACT_SCHEMA_VERSION = "task-construction-v2"


TaskQuestionType = Literal[
    "single_relation_entity",
    "constrained_entity",
    "hidden_bridge_two_hop",
    "explicit_bridge_two_hop",
    "study_context_qualified_disease",
    "ctd_direct_evidence_disease",
]

StudyRelationIntent = Literal[
    "association_research",
    "biomarker_research",
    "therapeutic_research",
    "risk_increase",
    "risk_decrease",
    "safety",
]


class ProviderSnapshotRef(ContractModel):
    """One immutable provider snapshot used by a validation environment."""

    provider_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    schema_version: str | None = None
    manifest_hash: str | None = None
    completeness: str = Field(min_length=1)
    detail: dict[str, Any] = Field(default_factory=dict)


class ValidationEnvironment(ContractModel):
    """The replayable world in which answers and cardinality are certified."""

    schema_version: str = TASK_CONTRACT_SCHEMA_VERSION
    environment_id: str = Field(min_length=1)
    providers: list[ProviderSnapshotRef] = Field(min_length=1)
    completeness_scope: str = Field(min_length=1)
    created_at: UtcDateTime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def provider_ids_are_unique(self) -> "ValidationEnvironment":
        ids = [item.provider_id for item in self.providers]
        if len(ids) != len(set(ids)):
            raise ValueError("validation environment provider IDs must be unique")
        return self


class ClaimPattern(ContractModel):
    """Source-neutral semantic condition needed to complete a task."""

    subject_id: str = Field(min_length=1)
    predicate_family: str = Field(min_length=1)
    object_id: str | None = None
    object_value: str | int | float | bool | None = None
    qualifier_constraints: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def has_an_object_constraint(self) -> "ClaimPattern":
        if self.object_id is None and self.object_value is None:
            raise ValueError("a claim pattern needs object_id or object_value")
        if self.object_id is not None and self.object_value is not None:
            raise ValueError("object_id and object_value are mutually exclusive")
        return self


class InformationRequirement(ContractModel):
    """Information that must be established, independent of a tool route."""

    requirement_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    claim_pattern: ClaimPattern
    evidence_quality_rule: str = "structured_or_supported_literature"
    required: bool = True
    weight: float = Field(default=1.0, gt=0)


class AcceptableEvidenceSet(ContractModel):
    """A source-neutral Boolean completion alternative."""

    all_of: list[str] = Field(default_factory=list)
    any_of: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def is_not_empty(self) -> "AcceptableEvidenceSet":
        if not self.all_of and not self.any_of:
            raise ValueError("an acceptable evidence set cannot be empty")
        return self


class CompletionRule(ContractModel):
    """Deterministic rule for deciding whether a task is complete."""

    mode: Literal["any_acceptable_set_satisfied"] = (
        "any_acceptable_set_satisfied"
    )
    require_answer_contract_match: Literal[True] = True
    require_complete_validation: Literal[True] = True


class StudyContext(ContractModel):
    """Biomedical qualifiers that must be visible in the public question."""

    population: str | None = None
    exposure_context: str | None = None
    dose_or_regimen: str | None = None
    exposure_duration: str | None = None
    study_type: str | None = None
    sample_type: str | None = None
    disease_category: str = Field(min_length=1)
    temporal_scope: str | None = None
    relation_intent: StudyRelationIntent
    polarity: Literal["increase", "decrease", "neutral", "unknown"] = (
        "unknown"
    )
    additional_qualifiers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def has_public_disambiguator(self) -> "StudyContext":
        values = (
            self.population,
            self.exposure_context,
            self.dose_or_regimen,
            self.exposure_duration,
            self.study_type,
            self.sample_type,
            self.temporal_scope,
        )
        if not any(values) and not self.additional_qualifiers:
            raise ValueError(
                "study context needs a public disambiguator beyond the "
                "disease category and relation"
            )
        expected_polarity = {
            "risk_increase": "increase",
            "risk_decrease": "decrease",
        }.get(self.relation_intent)
        if expected_polarity and self.polarity != expected_polarity:
            raise ValueError(
                f"{self.relation_intent} requires {expected_polarity} polarity"
            )
        return self


class AnswerContract(ContractModel):
    """The normalized answer and its declared cardinality."""

    answer_type: Literal["entity", "entity_set", "scalar", "categorical"]
    cardinality: Literal["exactly_one", "one_or_more", "exact_set"]
    canonical_answer_ids: list[str] = Field(default_factory=list)
    accepted_aliases: list[str] = Field(default_factory=list)
    normalization_rule: str = "canonical identity match"
    scope_statement: str = Field(min_length=1)

    @model_validator(mode="after")
    def cardinality_matches_answers(self) -> "AnswerContract":
        unique = list(dict.fromkeys(self.canonical_answer_ids))
        if unique != self.canonical_answer_ids:
            raise ValueError("canonical answers must be unique and stable")
        if self.cardinality == "exactly_one" and len(unique) != 1:
            raise ValueError("exactly_one requires one canonical answer")
        if self.cardinality == "one_or_more" and not unique:
            raise ValueError("one_or_more requires at least one canonical answer")
        return self


class TaskCandidate(ContractModel):
    """A discovery-time task hypothesis that has not been validated."""

    schema_version: str = TASK_CONTRACT_SCHEMA_VERSION
    candidate_id: str = Field(min_length=1)
    seed_entity_id: str = Field(min_length=1)
    question_type: TaskQuestionType
    steps: list[QueryStep] = Field(min_length=1)
    constraints: list[QueryConstraint] = Field(default_factory=list)
    proposed_answer_ids: list[str] = Field(min_length=1)
    private_answer_names: list[str] = Field(min_length=1)
    answer_entity_type: str | None = None
    snapshot_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    candidate_claim_ids: list[str] = Field(default_factory=list)
    public_study_context: StudyContext | None = None
    public_entity_names: dict[str, str] = Field(default_factory=dict)
    discovery_source: str = Field(min_length=1)
    generator_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def hidden_answers_are_not_public(self) -> "TaskCandidate":
        if set(self.proposed_answer_ids) & set(self.public_entity_names):
            raise ValueError("answer entities cannot appear in the public name map")
        hidden_names = {item.casefold() for item in self.private_answer_names}
        public_names = {
            item.casefold() for item in self.public_entity_names.values()
        }
        if hidden_names & public_names:
            raise ValueError("answer names cannot appear in the public name map")
        if self.question_type == "study_context_qualified_disease":
            if self.public_study_context is None:
                raise ValueError(
                    "a study-context task needs a public study context"
                )
            if not self.candidate_claim_ids:
                raise ValueError(
                    "a study-context task needs candidate claims"
                )
            if self.answer_entity_type != "disease":
                raise ValueError(
                    "a study-context disease task needs disease answers"
                )
        return self

    def semantic_digest(self) -> str:
        payload = {
            "seed_entity_id": self.seed_entity_id,
            "question_type": self.question_type,
            "steps": [step.model_dump(mode="json") for step in self.steps],
            "constraints": [
                item.model_dump(mode="json") for item in self.constraints
            ],
            "answer_entity_type": self.answer_entity_type,
            "snapshot_id": self.snapshot_id,
            "public_study_context": (
                self.public_study_context.model_dump(mode="json")
                if self.public_study_context is not None
                else None
            ),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()


class TaskSpec(ContractModel):
    """A compiled task awaiting authoritative validation."""

    schema_version: str = TASK_CONTRACT_SCHEMA_VERSION
    task_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    seed_entity_id: str = Field(min_length=1)
    question_type: TaskQuestionType
    query: LogicalQuery
    proposed_answer_contract: AnswerContract
    information_requirements: list[InformationRequirement] = Field(min_length=1)
    acceptable_evidence_sets: list[AcceptableEvidenceSet] = Field(min_length=1)
    completion_rule: CompletionRule = Field(default_factory=CompletionRule)
    validation_environment: ValidationEnvironment
    candidate_claim_ids: list[str] = Field(default_factory=list)
    candidate_evidence_ids: list[str] = Field(default_factory=list)
    public_study_context: StudyContext | None = None
    public_entity_names: dict[str, str] = Field(default_factory=dict)
    generator_version: str = "unknown"

    @model_validator(mode="after")
    def references_are_closed(self) -> "TaskSpec":
        requirement_ids = [item.requirement_id for item in self.information_requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("information requirement IDs must be unique")
        known = set(requirement_ids)
        referenced = {
            requirement_id
            for evidence_set in self.acceptable_evidence_sets
            for requirement_id in (*evidence_set.all_of, *evidence_set.any_of)
        }
        unknown = sorted(referenced - known)
        if unknown:
            raise ValueError(f"unknown information requirements: {unknown}")
        if self.query.snapshot_id not in {
            item.snapshot_id for item in self.validation_environment.providers
        }:
            raise ValueError("logical query snapshot is outside the validation world")
        if self.question_type == "study_context_qualified_disease":
            if self.public_study_context is None:
                raise ValueError(
                    "a study-context task needs a public study context"
                )
            if not self.candidate_claim_ids:
                raise ValueError(
                    "a study-context task needs candidate claims"
                )
            if not self.candidate_evidence_ids:
                raise ValueError(
                    "a study-context task needs candidate evidence"
                )
            if self.query.answer_entity_type != "disease":
                raise ValueError(
                    "a study-context disease task needs disease answers"
                )
        return self

    def evidence_dependency_digest(self) -> str:
        """Return the identity of hidden evidence dependencies and semantics."""

        payload = {
            "task_id": self.task_id,
            "environment_id": self.validation_environment.environment_id,
            "provider_snapshots": [
                item.model_dump(mode="json")
                for item in self.validation_environment.providers
            ],
            "candidate_claim_ids": sorted(set(self.candidate_claim_ids)),
            "candidate_evidence_ids": sorted(set(self.candidate_evidence_ids)),
            "information_requirements": [
                item.model_dump(mode="json")
                for item in self.information_requirements
            ],
            "acceptable_evidence_sets": [
                item.model_dump(mode="json")
                for item in self.acceptable_evidence_sets
            ],
            "completion_rule": self.completion_rule.model_dump(mode="json"),
            "public_study_context": (
                self.public_study_context.model_dump(mode="json")
                if self.public_study_context is not None
                else None
            ),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode(
                "utf-8"
            )
        ).hexdigest()


class AnswerCandidateOutcome(ContractModel):
    """Validation outcome for one entity in the declared candidate universe."""

    answer_id: str = Field(min_length=1)
    status: Literal[
        "matched",
        "not_matched",
        "ambiguous",
        "unreadable",
        "provider_failed",
        "not_evaluated",
    ]
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def matched_outcome_has_support(self) -> "AnswerCandidateOutcome":
        if self.status == "matched" and (
            not self.claim_ids or not self.evidence_ids
        ):
            raise ValueError(
                "a matched answer candidate needs claim and evidence references"
            )
        return self


class ValidationReport(ContractModel):
    """Immutable result of executing and checking a TaskSpec."""

    schema_version: str = TASK_CONTRACT_SCHEMA_VERSION
    report_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    query_id: str = Field(min_length=1)
    query_digest: str = Field(min_length=1)
    environment_id: str = Field(min_length=1)
    passed: bool
    reason_codes: list[str] = Field(default_factory=list)
    slice_answer_ids: list[str] = Field(default_factory=list)
    world_answer_ids: list[str] = Field(default_factory=list)
    result_complete: bool
    rows_examined: int = Field(default=0, ge=0)
    matched_claim_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    competitor_outcomes: list[AnswerCandidateOutcome] = Field(
        default_factory=list
    )
    evidence_dependency_digest: str | None = None
    validator_version: str = Field(min_length=1)
    validated_at: UtcDateTime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def a_passing_report_has_no_failure_reason(self) -> "ValidationReport":
        if self.passed and self.reason_codes:
            raise ValueError("a passing validation report cannot have failure reasons")
        if self.passed and not self.result_complete:
            raise ValueError("an incomplete result cannot pass validation")
        if not self.passed and not self.reason_codes:
            raise ValueError("a failed validation report needs a reason code")
        if self.passed and self.competitor_outcomes:
            unknown_statuses = {
                "ambiguous",
                "unreadable",
                "provider_failed",
                "not_evaluated",
            }
            if any(
                item.status in unknown_statuses
                for item in self.competitor_outcomes
            ):
                raise ValueError(
                    "a passing validation report cannot contain an "
                    "unknown competitor"
                )
            matched = sorted(
                {
                    item.answer_id
                    for item in self.competitor_outcomes
                    if item.status == "matched"
                }
            )
            if matched != sorted(set(self.world_answer_ids)):
                raise ValueError(
                    "matched competitor outcomes must equal world answers"
                )
        return self


class ValidatedTask(ContractModel):
    """A task whose answer and semantics were certified by a validator."""

    schema_version: str = TASK_CONTRACT_SCHEMA_VERSION
    task: TaskSpec
    answer_contract: AnswerContract
    validation_report_id: str = Field(min_length=1)
    gold_claim_ids: list[str] = Field(default_factory=list)
    gold_evidence_ids: list[str] = Field(default_factory=list)
    validated_at: UtcDateTime


def promote_validated_task(
    task: TaskSpec, report: ValidationReport
) -> ValidatedTask:
    """Promote only a matching, complete and passing validation report."""

    if not report.passed:
        raise ValueError("the validation report did not pass")
    if not report.result_complete:
        raise ValueError("the validation result is incomplete")
    if report.task_id != task.task_id:
        raise ValueError("validation report task ID does not match")
    if report.query_id != task.query.query_id:
        raise ValueError("validation report query ID does not match")
    if report.query_digest != task.query.dependency_digest:
        raise ValueError("validation report query digest does not match")
    if report.environment_id != task.validation_environment.environment_id:
        raise ValueError("validation report environment does not match")
    if task.question_type == "study_context_qualified_disease":
        if (
            report.evidence_dependency_digest
            != task.evidence_dependency_digest()
        ):
            raise ValueError(
                "validation report evidence dependency digest does not match"
            )
        if not report.competitor_outcomes:
            raise ValueError(
                "study-context validation needs competitor outcomes"
            )
        if not report.matched_claim_ids or not report.supporting_evidence_ids:
            raise ValueError(
                "study-context validation needs gold claim and evidence references"
            )
        if not set(report.matched_claim_ids).issubset(
            task.candidate_claim_ids
        ) or not set(report.supporting_evidence_ids).issubset(
            task.candidate_evidence_ids
        ):
            raise ValueError(
                "validation report gold references are outside task dependencies"
            )

    answer_contract = task.proposed_answer_contract.model_copy(
        update={"canonical_answer_ids": list(report.world_answer_ids)}
    )
    # Re-validate after model_copy: Pydantic intentionally skips validators on
    # copy, while promotion must enforce the authoritative cardinality.
    answer_contract = AnswerContract.model_validate(
        answer_contract.model_dump(mode="python")
    )
    return ValidatedTask(
        task=task,
        answer_contract=answer_contract,
        validation_report_id=report.report_id,
        gold_claim_ids=list(report.matched_claim_ids),
        gold_evidence_ids=list(report.supporting_evidence_ids),
        validated_at=report.validated_at,
    )


class PlannerQuestionInput(ContractModel):
    """The allow-listed, gold-free view visible to a planning policy."""

    schema_version: str = TASK_CONTRACT_SCHEMA_VERSION
    qid: str = Field(min_length=1)
    question: str = Field(min_length=1)
    qtype: str = Field(min_length=1)
    budget: dict[str, int] = Field(default_factory=dict)
    max_steps: int = Field(default=8, ge=1)
    sufficiency_threshold: float = Field(default=1.0, ge=0, le=1)
    note: str = ""
    environment_ref: str = Field(min_length=1)


class EvaluationTarget(ContractModel):
    """Hidden truth used by the evaluator, never by the planning policy."""

    schema_version: str = TASK_CONTRACT_SCHEMA_VERSION
    qid: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    answer_contract: AnswerContract
    information_requirements: list[InformationRequirement]
    acceptable_evidence_sets: list[AcceptableEvidenceSet]
    completion_rule: CompletionRule = Field(default_factory=CompletionRule)
    gold_claim_ids: list[str] = Field(default_factory=list)
    gold_evidence_ids: list[str] = Field(default_factory=list)
    evidence_catalog_ref: str | None = None
    validation_environment: ValidationEnvironment
    validation_report_ref: str = Field(min_length=1)

    @classmethod
    def from_validated_task(
        cls, qid: str, validated: ValidatedTask
    ) -> "EvaluationTarget":
        return cls(
            qid=qid,
            task_id=validated.task.task_id,
            answer_contract=validated.answer_contract,
            information_requirements=list(
                validated.task.information_requirements
            ),
            acceptable_evidence_sets=list(
                validated.task.acceptable_evidence_sets
            ),
            completion_rule=validated.task.completion_rule,
            gold_claim_ids=list(validated.gold_claim_ids),
            gold_evidence_ids=list(validated.gold_evidence_ids),
            evidence_catalog_ref=(
                f"evidence-catalog:{validated.task.task_id}"
                if validated.gold_evidence_ids
                else None
            ),
            validation_environment=validated.task.validation_environment,
            validation_report_ref=validated.validation_report_id,
        )
