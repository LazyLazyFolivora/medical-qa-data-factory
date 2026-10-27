"""Source-neutral evidence and normalized biomedical claim contracts."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator

from ..domain import KGEvidence
from ..domain.common import ContractModel, UtcDateTime
from .models import (
    CtdEvidenceRecord,
    NormalizedEvidenceClaim,
    PubMedEvidenceRecord,
    QuestionIntent,
)


EVIDENCE_CONTRACT_SCHEMA_VERSION = "source-neutral-evidence-v1"


def _digest(payload: dict[str, Any]) -> str:
    body = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _evidence_id(
    *,
    provider_id: str,
    snapshot_id: str,
    source_locator: str,
    content_hash: str,
) -> str:
    digest = _digest(
        {
            "provider_id": provider_id,
            "snapshot_id": snapshot_id,
            "source_locator": source_locator,
            "content_hash": content_hash,
        }
    )
    return f"evidence.{digest[:24]}"


def _claim_id(
    *,
    subject_id: str,
    predicate_family: str,
    object_id: str | None,
    object_value: str | int | float | bool | None,
    qualifiers: dict[str, Any],
    polarity: str,
    normalizer_version: str,
) -> str:
    digest = _digest(
        {
            "subject_id": subject_id,
            "predicate_family": predicate_family,
            "object_id": object_id,
            "object_value": object_value,
            "qualifiers": qualifiers,
            "polarity": polarity,
            "normalizer_version": normalizer_version,
        }
    )
    return f"claim.{digest[:24]}"


class EvidenceRecord(ContractModel):
    """One immutable source observation, independent of a task or tool route."""

    schema_version: Literal["source-neutral-evidence-v1"] = (
        EVIDENCE_CONTRACT_SCHEMA_VERSION
    )
    evidence_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    source_kind: Literal[
        "structured_database",
        "knowledge_graph",
        "literature",
    ]
    snapshot_id: str = Field(min_length=1)
    source_locator: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_type: Literal["structured_relation", "document"]
    subject_id: str | None = None
    predicate_family: str | None = None
    object_id: str | None = None
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    document_text: str | None = None
    retrieved_at: UtcDateTime
    source_metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        source_kind: Literal[
            "structured_database", "knowledge_graph", "literature"
        ],
        snapshot_id: str,
        source_locator: str,
        content_hash: str,
        record_type: Literal["structured_relation", "document"],
        retrieved_at: UtcDateTime | str,
        subject_id: str | None = None,
        predicate_family: str | None = None,
        object_id: str | None = None,
        qualifiers: dict[str, Any] | None = None,
        document_text: str | None = None,
        source_metadata: dict[str, Any] | None = None,
    ) -> "EvidenceRecord":
        return cls(
            evidence_id=_evidence_id(
                provider_id=provider_id,
                snapshot_id=snapshot_id,
                source_locator=source_locator,
                content_hash=content_hash,
            ),
            provider_id=provider_id,
            source_kind=source_kind,
            snapshot_id=snapshot_id,
            source_locator=source_locator,
            content_hash=content_hash,
            record_type=record_type,
            subject_id=subject_id,
            predicate_family=predicate_family,
            object_id=object_id,
            qualifiers=qualifiers or {},
            document_text=document_text,
            retrieved_at=retrieved_at,
            source_metadata=source_metadata or {},
        )

    @model_validator(mode="after")
    def identity_and_shape_are_valid(self) -> "EvidenceRecord":
        expected = _evidence_id(
            provider_id=self.provider_id,
            snapshot_id=self.snapshot_id,
            source_locator=self.source_locator,
            content_hash=self.content_hash,
        )
        if self.evidence_id != expected:
            raise ValueError("evidence ID does not match immutable provenance")
        triple = (self.subject_id, self.predicate_family, self.object_id)
        if self.record_type == "structured_relation" and not all(triple):
            raise ValueError("structured relation evidence needs a complete triple")
        if self.record_type == "document" and not (
            self.document_text and self.document_text.strip()
        ):
            raise ValueError("document evidence needs readable text")
        return self


class NormalizedClaim(ContractModel):
    """A source-neutral semantic claim with explicit evidence roles."""

    schema_version: Literal["source-neutral-evidence-v1"] = (
        EVIDENCE_CONTRACT_SCHEMA_VERSION
    )
    claim_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    predicate_family: str = Field(min_length=1)
    object_id: str | None = None
    object_value: str | int | float | bool | None = None
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    polarity: Literal["increase", "decrease", "neutral", "unknown"] = (
        "unknown"
    )
    normalizer_version: str = Field(min_length=1)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    context_evidence_ids: list[str] = Field(default_factory=list)
    claim_text: str = ""
    legacy_claim_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        subject_id: str,
        predicate_family: str,
        object_id: str | None = None,
        object_value: str | int | float | bool | None = None,
        qualifiers: dict[str, Any] | None = None,
        polarity: Literal[
            "increase", "decrease", "neutral", "unknown"
        ] = "unknown",
        normalizer_version: str,
        supporting_evidence_ids: list[str] | None = None,
        contradicting_evidence_ids: list[str] | None = None,
        context_evidence_ids: list[str] | None = None,
        claim_text: str = "",
        legacy_claim_id: str | None = None,
    ) -> "NormalizedClaim":
        claim_qualifiers = qualifiers or {}
        return cls(
            claim_id=_claim_id(
                subject_id=subject_id,
                predicate_family=predicate_family,
                object_id=object_id,
                object_value=object_value,
                qualifiers=claim_qualifiers,
                polarity=polarity,
                normalizer_version=normalizer_version,
            ),
            subject_id=subject_id,
            predicate_family=predicate_family,
            object_id=object_id,
            object_value=object_value,
            qualifiers=claim_qualifiers,
            polarity=polarity,
            normalizer_version=normalizer_version,
            supporting_evidence_ids=supporting_evidence_ids or [],
            contradicting_evidence_ids=contradicting_evidence_ids or [],
            context_evidence_ids=context_evidence_ids or [],
            claim_text=claim_text,
            legacy_claim_id=legacy_claim_id,
        )

    @model_validator(mode="after")
    def semantic_identity_and_references_are_valid(self) -> "NormalizedClaim":
        if (self.object_id is None) == (self.object_value is None):
            raise ValueError("claim needs exactly one object representation")
        if (
            "polarity" in self.qualifiers
            and self.qualifiers["polarity"] != self.polarity
        ):
            raise ValueError("claim qualifier polarity conflicts with polarity")
        expected = _claim_id(
            subject_id=self.subject_id,
            predicate_family=self.predicate_family,
            object_id=self.object_id,
            object_value=self.object_value,
            qualifiers=self.qualifiers,
            polarity=self.polarity,
            normalizer_version=self.normalizer_version,
        )
        if self.claim_id != expected:
            raise ValueError("claim ID does not match normalized semantics")
        reference_groups = (
            self.supporting_evidence_ids,
            self.contradicting_evidence_ids,
            self.context_evidence_ids,
        )
        for references in reference_groups:
            if len(references) != len(set(references)):
                raise ValueError("claim evidence references must be unique")
        flattened = [item for group in reference_groups for item in group]
        if not flattened:
            raise ValueError("normalized claim needs evidence")
        if len(flattened) != len(set(flattened)):
            raise ValueError("one evidence record cannot have multiple claim roles")
        return self


class EvidenceStore(ContractModel):
    """Closed evidence/claim collection consumed by deterministic matchers."""

    schema_version: Literal["source-neutral-evidence-v1"] = (
        EVIDENCE_CONTRACT_SCHEMA_VERSION
    )
    records: list[EvidenceRecord] = Field(default_factory=list)
    claims: list[NormalizedClaim] = Field(default_factory=list)

    @model_validator(mode="after")
    def identities_and_references_are_closed(self) -> "EvidenceStore":
        evidence_ids = [record.evidence_id for record in self.records]
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence store contains duplicate evidence IDs")
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("evidence store contains duplicate claim IDs")
        known = set(evidence_ids)
        referenced = {
            evidence_id
            for claim in self.claims
            for evidence_id in (
                claim.supporting_evidence_ids
                + claim.contradicting_evidence_ids
                + claim.context_evidence_ids
            )
        }
        if unknown := sorted(referenced - known):
            raise ValueError(
                f"claims reference unknown source-neutral evidence: {unknown}"
            )
        return self


def evidence_from_ctd(
    record: CtdEvidenceRecord,
    *,
    subject_entity_id: str,
    object_entity_id: str,
    retrieved_at: UtcDateTime | str,
) -> EvidenceRecord:
    if record.source_record_locator is None:
        raise ValueError("legacy CTD record has no stable source locator")
    return EvidenceRecord.create(
        provider_id="ctd",
        source_kind="structured_database",
        snapshot_id=record.source_snapshot_id,
        source_locator=record.source_record_locator,
        content_hash=record.content_hash,
        record_type="structured_relation",
        subject_id=subject_entity_id,
        predicate_family="chemical_disease_association",
        object_id=object_entity_id,
        retrieved_at=retrieved_at,
        source_metadata={
            "chemical_id": record.chemical_id,
            "chemical_name": record.chemical_name,
            "disease_id": record.disease_id,
            "disease_name": record.disease_name,
            "direct_evidence": list(record.direct_evidence),
            "inference_genes": list(record.inference_genes),
            "inference_score": record.inference_score,
            "inference_only": not bool(record.direct_evidence),
            "pubmed_ids": list(record.pubmed_ids),
        },
    )


def evidence_from_pubmed(record: PubMedEvidenceRecord) -> EvidenceRecord:
    return EvidenceRecord.create(
        provider_id="pubmed",
        source_kind="literature",
        snapshot_id=record.snapshot_id,
        source_locator=f"pmid:{record.pmid}",
        content_hash=record.content_hash,
        record_type="document",
        document_text=record.abstract or record.title,
        retrieved_at=record.retrieved_at,
        source_metadata={
            "pmid": record.pmid,
            "doi": record.doi,
            "title": record.title,
        },
    )


def evidence_from_kg(
    record: KGEvidence, *, provider_snapshot_id: str
) -> EvidenceRecord:
    source_locator = record.source_record_id or record.evidence_id
    subject_id = record.head.canonical_id or record.head.entity_ref_id
    object_id = record.tail.canonical_id or record.tail.entity_ref_id
    content_hash = _digest(
        {
            "subject_id": subject_id,
            "predicate": record.relation,
            "object_id": object_id,
            "direction": record.direction.value,
            "source_database": record.source_database,
            "source_locator": source_locator,
        }
    )
    return EvidenceRecord.create(
        provider_id=record.source_database,
        source_kind="knowledge_graph",
        snapshot_id=provider_snapshot_id,
        source_locator=source_locator,
        content_hash=content_hash,
        record_type="structured_relation",
        subject_id=subject_id,
        predicate_family=record.relation,
        object_id=object_id,
        retrieved_at=record.retrieved_at,
        source_metadata={
            "legacy_evidence_id": record.evidence_id,
            "direction": record.direction.value,
            "metadata": dict(record.metadata),
        },
    )


_INTENT_PREDICATES = {
    QuestionIntent.RISK_INCREASE: "disease_risk",
    QuestionIntent.RISK_DECREASE: "disease_risk",
    QuestionIntent.BIOMARKER_RESEARCH: "biomarker_relationship",
    QuestionIntent.THERAPEUTIC_RESEARCH: "therapeutic_research",
    QuestionIntent.ASSOCIATION_RESEARCH: "association",
    QuestionIntent.SAFETY: "safety_relationship",
    QuestionIntent.CONTRAINDICATION: "contraindication",
    QuestionIntent.MECHANISM_RESEARCH: "mechanism",
}


def predicate_family_for_intent(
    intent: QuestionIntent, *, fallback_relation: str
) -> str:
    return _INTENT_PREDICATES.get(intent, fallback_relation)


def migrate_legacy_claim(
    claim: NormalizedEvidenceClaim,
    *,
    qualifiers: dict[str, Any] | None = None,
    supporting_evidence_ids: list[str] | None = None,
    contradicting_evidence_ids: list[str] | None = None,
    context_evidence_ids: list[str] | None = None,
) -> NormalizedClaim:
    """Explicit adapter for persisted evidence-enrichment-v1 claims."""
    return NormalizedClaim.create(
        subject_id=claim.seed_entity_id,
        predicate_family=predicate_family_for_intent(
            claim.question_intent,
            fallback_relation=claim.relation,
        ),
        object_id=claim.answer_entity_id,
        qualifiers=qualifiers or {},
        polarity=claim.polarity.value,
        normalizer_version="legacy-enrichment-v1-adapter-v1",
        supporting_evidence_ids=(
            supporting_evidence_ids
            if supporting_evidence_ids is not None
            else list(claim.literature_evidence_ids)
        ),
        contradicting_evidence_ids=contradicting_evidence_ids,
        context_evidence_ids=context_evidence_ids,
        claim_text=claim.claim_text,
        legacy_claim_id=claim.claim_id,
    )
