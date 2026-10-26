"""Evidence and claim contracts."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field, model_validator

from .common import (
    SCHEMA_VERSION,
    ChangeRecord,
    ContractModel,
    EntityRef,
    ToolSnapshot,
    UtcDateTime,
    ValidationCheck,
    ValidationLevel,
)


class EvidenceDirection(str, Enum):
    FORWARD = "forward"
    BACKWARD = "backward"
    BIDIRECTIONAL = "bidirectional"
    UNKNOWN = "unknown"


class ClaimType(str, Enum):
    FACT = "fact"
    ASSOCIATION = "association"
    CAUSAL = "causal"
    MECHANISM = "mechanism"
    COMPARATIVE = "comparative"
    UNCERTAINTY = "uncertainty"


class ClaimStatus(str, Enum):
    PROPOSED = "proposed"
    SUPPORTED = "supported"
    CONFLICTED = "conflicted"
    INSUFFICIENT = "insufficient"
    REJECTED = "rejected"


class SuggestedSource(str, Enum):
    KG = "kg"
    LITERATURE = "literature"
    EITHER = "either"
    UNKNOWN = "unknown"


class KGEvidence(ContractModel):
    evidence_id: str = Field(min_length=1)
    head: EntityRef
    relation: str = Field(min_length=1)
    tail: EntityRef
    direction: EvidenceDirection
    source_database: str = Field(min_length=1)
    source_record_id: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    retrieval_action: dict[str, Any]
    retrieved_at: UtcDateTime
    metadata: dict[str, Any] = Field(default_factory=dict)


class LiteratureEvidence(ContractModel):
    evidence_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    pmid: str | None = None
    doi: str | None = None
    title: str = Field(min_length=1)
    year: int | None = Field(default=None, ge=1800)
    url: str | None = None
    publication_type: str | None = None
    evidence_text: str = Field(min_length=1)
    location: dict[str, Any] = Field(default_factory=dict)
    entities: list[EntityRef] = Field(default_factory=list)
    retrieval_query: str = Field(min_length=1)
    retrieval_rank: int | None = Field(default=None, ge=1)
    retrieved_at: UtcDateTime
    license_note: str | None = None


class Claim(ContractModel):
    claim_id: str = Field(min_length=1)
    claim_text: str = Field(min_length=1)
    claim_type: ClaimType
    subject: EntityRef | None = None
    predicate: str | None = None
    object: EntityRef | str | None = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    context_evidence_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    status: ClaimStatus
    rationale: str

    @model_validator(mode="after")
    def supported_claim_has_evidence(self) -> "Claim":
        if self.status is ClaimStatus.SUPPORTED and not self.supporting_evidence_ids:
            raise ValueError("supported claim requires supporting evidence")
        return self


class EvidenceConflict(ContractModel):
    conflict_id: str = Field(min_length=1)
    claim_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=2)
    conflict_type: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class EvidenceGap(ContractModel):
    gap_id: str = Field(min_length=1)
    gap_type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    suggested_source: SuggestedSource
    blocking: bool


class EvidenceBundle(ContractModel):
    schema_version: str = SCHEMA_VERSION
    bundle_id: str = Field(min_length=1)
    bundle_version: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    seed_entities: list[EntityRef] = Field(min_length=1)
    kg_evidence: list[KGEvidence] = Field(default_factory=list)
    literature_evidence: list[LiteratureEvidence] = Field(default_factory=list)
    # Ordered multi-hop provenance. Imported lazily to avoid a cycle with the
    # generation layer that owns the path contracts.
    paths: list[Any] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    conflicts: list[EvidenceConflict] = Field(default_factory=list)
    evidence_gaps: list[EvidenceGap] = Field(default_factory=list)
    tool_snapshots: list[ToolSnapshot] = Field(default_factory=list)
    generation_usage_ids: list[str] = Field(default_factory=list)
    status: ValidationLevel = ValidationLevel.DRAFT
    validation_checks: list[ValidationCheck] = Field(default_factory=list)
    created_at: UtcDateTime
    changes: list[ChangeRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_claim_evidence_references(self) -> "EvidenceBundle":
        known = {item.evidence_id for item in self.kg_evidence}
        known.update(item.evidence_id for item in self.literature_evidence)
        referenced: set[str] = set()
        for claim in self.claims:
            referenced.update(claim.supporting_evidence_ids)
            referenced.update(claim.contradicting_evidence_ids)
            referenced.update(claim.context_evidence_ids)
        unknown = referenced - known
        if unknown:
            raise ValueError(f"claims reference unknown evidence ids: {sorted(unknown)}")
        return self

