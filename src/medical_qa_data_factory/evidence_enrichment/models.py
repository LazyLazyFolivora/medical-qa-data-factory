"""Versioned contracts for source enrichment and evidence claims."""
from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from ..domain.common import ContractModel, UtcDateTime


ENRICHMENT_SCHEMA_VERSION = "evidence-enrichment-v1"


class EnrichmentStatus(str, Enum):
    SUPPORTED = "supported"
    INSUFFICIENT = "insufficient"
    CONFLICTING = "conflicting"
    AMBIGUOUS_MAPPING = "ambiguous_mapping"
    UNMAPPED = "unmapped"


class EvidenceSupportLevel(str, Enum):
    DIRECT = "direct"
    LITERATURE_SUPPORTED = "literature_supported"
    INFERRED = "inferred"
    INSUFFICIENT = "insufficient"


class EvidencePolarity(str, Enum):
    INCREASE = "increase"
    DECREASE = "decrease"
    NEUTRAL = "neutral"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class QuestionIntent(str, Enum):
    ASSOCIATION_RESEARCH = "association_research"
    BIOMARKER_RESEARCH = "biomarker_research"
    THERAPEUTIC_RESEARCH = "therapeutic_research"
    MECHANISM_RESEARCH = "mechanism_research"
    RISK_INCREASE = "risk_increase"
    RISK_DECREASE = "risk_decrease"
    CONTRAINDICATION = "contraindication"
    SAFETY = "safety"
    INSUFFICIENT = "insufficient"
    CONFLICTING = "conflicting"


class CtdEvidenceRecord(ContractModel):
    chemical_id: str = Field(min_length=1)
    chemical_name: str = Field(min_length=1)
    disease_id: str | None = None
    disease_name: str = Field(min_length=1)
    direct_evidence: list[str] = Field(default_factory=list)
    inference_genes: list[str] = Field(default_factory=list)
    inference_score: float | None = None
    pubmed_ids: list[str] = Field(default_factory=list)
    source_snapshot_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)


class PubMedEvidenceRecord(ContractModel):
    pmid: str = Field(min_length=1)
    title: str = ""
    abstract: str = ""
    doi: str | None = None
    snapshot_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    retrieved_at: UtcDateTime

    @model_validator(mode="after")
    def has_text(self) -> "PubMedEvidenceRecord":
        if not self.title.strip() and not self.abstract.strip():
            raise ValueError("a PubMed record must contain a title or abstract")
        return self


class EvidenceClaim(ContractModel):
    """One normalized claim before it becomes a question blueprint."""

    schema_version: str = ENRICHMENT_SCHEMA_VERSION
    claim_id: str = Field(min_length=1)
    kg_edge_id: str = Field(min_length=1)
    subject_entity_id: str = Field(min_length=1)
    object_entity_id: str = Field(min_length=1)
    subject_name: str = Field(min_length=1)
    object_name: str = Field(min_length=1)
    relation: str = Field(min_length=1)
    question_intent: QuestionIntent
    support_level: EvidenceSupportLevel = EvidenceSupportLevel.LITERATURE_SUPPORTED
    polarity: EvidencePolarity = EvidencePolarity.UNKNOWN
    study_type: str | None = None
    population: str | None = None
    claim_text: str = ""
    evidence_quote_ids: list[str] = Field(default_factory=list)
    ctd_source_id: str | None = None
    pubmed_ids: list[str] = Field(default_factory=list)
    source_snapshot_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)


class NormalizedEvidenceClaim(ContractModel):
    """A claim plus the exact entity identities required by the QA contract."""

    schema_version: str = ENRICHMENT_SCHEMA_VERSION
    claim_id: str = Field(min_length=1)
    kg_edge_id: str = Field(min_length=1)
    seed_entity_id: str = Field(min_length=1)
    answer_entity_id: str = Field(min_length=1)
    answer_entity_type: str | None = None
    relation: str = Field(min_length=1)
    question_intent: QuestionIntent
    support_level: EvidenceSupportLevel
    polarity: EvidencePolarity = EvidencePolarity.UNKNOWN
    claim_text: str = ""
    source_snapshot_ids: list[str] = Field(default_factory=list)
    ctd_source_id: str | None = None
    pubmed_ids: list[str] = Field(default_factory=list)
    literature_evidence_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def eligible_intent(self) -> "NormalizedEvidenceClaim":
        if self.question_intent in {
            QuestionIntent.INSUFFICIENT,
            QuestionIntent.CONFLICTING,
        }:
            raise ValueError("insufficient/conflicting claims are not eligible")
        if self.seed_entity_id == self.answer_entity_id:
            raise ValueError("the answer cannot be the seed entity")
        return self
