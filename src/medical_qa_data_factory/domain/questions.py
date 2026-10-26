"""Question specification contracts."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field, model_validator

from .common import (
    SCHEMA_VERSION,
    ChangeRecord,
    ContractModel,
    GenerationMode,
    ValidationCheck,
    ValidationLevel,
    UtcDateTime,
)


class AnswerForm(str, Enum):
    FACT = "fact"
    ENTITY = "entity"
    SET = "set"
    BOOLEAN = "boolean"
    COUNT = "count"
    COMPARISON = "comparison"
    EXPLANATION = "explanation"
    SUMMARY = "summary"


class Answerability(str, Enum):
    ANSWERABLE = "answerable"
    UNANSWERABLE = "unanswerable"
    UNCERTAIN = "uncertain"


class Route(str, Enum):
    KG = "kg"
    LITERATURE = "literature"
    HYBRID = "hybrid"
    REDUNDANT = "redundant"
    UNANSWERABLE = "unanswerable"
    UNKNOWN = "unknown"


class DatasetSplit(str, Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


class ReviewDisposition(str, Enum):
    """What a reviewer decided about a candidate within its review group.

    The disposition is a persistent state. The commands a reviewer issues
    (``keep_both``, ``revise``, ``return``) are not dispositions: they
    resolve into one of these.
    """

    PENDING = "pending"
    #: The candidate is an independent question in its own right.
    KEEP_INDEPENDENT = "keep_independent"
    #: The candidate is a wording variant of another, primary question.
    VARIANT_OF = "variant_of"
    REJECTED = "rejected"


class RankingFactor(ContractModel):
    """One explainable contribution to a candidate's ranking score."""

    code: str = Field(min_length=1)
    value: float = 0.0
    weight: float = 0.0
    contribution: float = 0.0
    explanation: str = ""


class RankingAssessment(ContractModel):
    """A candidate's ranking within its review group.

    The score changes the order candidates are shown in. It is not a quality
    verdict: it never changes a validation level, a review disposition or an
    export decision.
    """

    ranking_version: str = Field(min_length=1)
    total_score: float = 0.0
    factors: list[RankingFactor] = Field(default_factory=list)
    computed_at: UtcDateTime


class QuestionVariant(ContractModel):
    variant_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    language: str = Field(min_length=1)
    persona: str = Field(min_length=1)
    style: str = Field(min_length=1)
    generation_source: str = Field(pattern="^(agent|manual|imported)$")
    semantic_check_status: str = Field(pattern="^(passed|warning|failed)$")


class QuestionTaxonomy(ContractModel):
    domain_intent: str = Field(min_length=1)
    answer_form: AnswerForm
    reasoning_pattern: list[str] = Field(min_length=1)
    hop_complexity: int | None = Field(default=None, ge=0)
    difficulty_tags: list[str] = Field(default_factory=list)


class GoldAnswer(ContractModel):
    answer_mode: AnswerForm
    values: list[dict[str, Any]] = Field(default_factory=list)
    short_answer: str
    explanation: str | None = None
    acceptable_variants: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)


class EvidenceRequirements(ContractModel):
    required_claim_ids: list[str] = Field(default_factory=list)
    required_kg_evidence_ids: list[str] = Field(default_factory=list)
    required_literature_evidence_ids: list[str] = Field(default_factory=list)
    # Ordered multi-hop provenance. Optional so old specs stay readable.
    required_path_ids: list[str] = Field(default_factory=list)
    optional_evidence_ids: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    sufficiency_rule_version: str = Field(min_length=1)


class AblationMode(str, Enum):
    KG_ONLY = "kg_only"
    LITERATURE_ONLY = "literature_only"
    COMBINED = "combined"


class AblationStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class AblationResult(ContractModel):
    """One source-ablation run.

    The result records what the question looked like to a solver that could
    only see one source. The fields that make the run auditable are the
    dependency digest (what was frozen), the gold-hidden flag, the episode
    that ran it, and the snapshots it used.
    """

    mode: AblationMode
    status: AblationStatus
    answer_correct: bool | None = None
    evidence_coverage: float | None = Field(default=None, ge=0, le=1)
    final_decision: str | None = None
    failure_reason: str | None = None
    episode_policy_cost: dict[str, Any] | None = None
    trajectory_id: str | None = None
    #: The version of the ablation protocol. A change to the rules must bump
    #: it, so an old result is not read as if it were the new protocol's.
    ablation_config_version: str = "ablation-v1"
    #: The digest of the frozen question the run was based on.
    question_dependency_digest: str | None = None
    #: The episode that ran this ablation. Each mode gets its own.
    episode_id: str | None = None
    #: The tools this mode was allowed to use.
    allowed_tools: list[str] = Field(default_factory=list)
    #: Whether the gold answer and gold evidence were hidden. A run that saw
    #: the gold may not be used to verify a route.
    gold_hidden: bool = True
    #: The provider snapshots this run used.
    snapshot_ids: list[str] = Field(default_factory=list)
    #: The tool call traces of this run.
    trace_ids: list[str] = Field(default_factory=list)
    #: The answer the mode produced, in the same shape as the gold answer.
    predicted_answer: dict[str, Any] | None = None
    #: The canonical answer ids the mode produced.
    predicted_answer_ids: list[str] = Field(default_factory=list)
    #: The canonical answer ids of the gold, for the comparison.
    gold_answer_ids: list[str] = Field(default_factory=list)
    #: The evidence ids the mode gathered.
    evidence_ids: list[str] = Field(default_factory=list)


class QuestionSpec(ContractModel):
    schema_version: str = SCHEMA_VERSION
    question_id: str = Field(min_length=1)
    logical_question_id: str = Field(min_length=1)
    question_version: str = Field(min_length=1)
    bundle_ids: list[str] = Field(min_length=1)
    generation_mode: GenerationMode
    canonical_question: str = Field(min_length=1)
    question_variants: list[QuestionVariant] = Field(min_length=1)
    taxonomy: QuestionTaxonomy
    query_intent: dict[str, Any]
    answerability: Answerability
    gold_answer: GoldAnswer
    evidence_requirements: EvidenceRequirements
    expected_route: Route
    verified_route: Route = Route.UNKNOWN
    ablation_results: list[AblationResult] = Field(default_factory=list)
    validation_level: ValidationLevel = ValidationLevel.DRAFT
    validation_checks: list[ValidationCheck] = Field(default_factory=list)
    split_group_id: str = Field(min_length=1)
    dataset_split: DatasetSplit | None = None
    # ---- review grouping ----
    #: The stable key of the entity-relation group this question belongs to.
    #: It is derived from the seed entity and the ordered relation chain, so
    #: two candidates that ask the same structural question share it.
    review_group_key: str | None = None
    #: The version of the grouping rules that produced the key.
    review_group_version: str | None = None
    #: The explainable ranking within the group.
    ranking_assessment: RankingAssessment | None = None
    #: What a reviewer decided about this candidate.
    review_disposition: ReviewDisposition = ReviewDisposition.PENDING
    #: The primary question, when this candidate is a wording variant of it.
    variant_of_question_id: str | None = None
    # Backward-compatible link to the generation episode that produced this
    # record. This is the only place an episode reference is stored.
    generation_episode_id: str | None = None
    created_at: UtcDateTime
    changes: list[ChangeRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_variant_ids(self) -> "QuestionSpec":
        ids = [variant.variant_id for variant in self.question_variants]
        if len(ids) != len(set(ids)):
            raise ValueError("question variant ids must be unique")
        return self

