"""Join PrimeKG CTD edges, PubMed text and a structured evidence claim."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import Field, model_validator

from ..domain import KGEvidence, LiteratureEvidence
from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from .ctd import CtdEvidenceIndex
from .contracts import (
    EvidenceRecord,
    EvidenceStore,
    NormalizedClaim,
    evidence_from_ctd,
    evidence_from_kg,
    evidence_from_pubmed,
    migrate_legacy_claim,
)
from .models import (
    CtdEvidenceRecord,
    EnrichmentStatus,
    EvidencePolarity,
    EvidenceSupportLevel,
    NormalizedEvidenceClaim,
    QuestionIntent,
    PubMedEvidenceRecord,
)
from .pubmed import PubMedClient


class EvidenceAssessment(ContractModel):
    status: EnrichmentStatus
    question_intent: QuestionIntent
    support_level: EvidenceSupportLevel
    polarity: EvidencePolarity = EvidencePolarity.UNKNOWN
    claim_text: str = ""
    study_type: str | None = None
    population: str | None = None
    supported_pmid_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def terminal_intents_are_not_supported(self) -> "EvidenceAssessment":
        if self.status is EnrichmentStatus.SUPPORTED and self.question_intent in {
            QuestionIntent.INSUFFICIENT,
            QuestionIntent.CONFLICTING,
        }:
            raise ValueError("unsupported intent cannot have supported status")
        return self


class EvidenceEnrichmentResult(ContractModel):
    edge_id: str = Field(min_length=1)
    status: EnrichmentStatus
    claim: NormalizedEvidenceClaim | None = None
    ctd_records: list[CtdEvidenceRecord] = Field(default_factory=list)
    pubmed_records: list[PubMedEvidenceRecord] = Field(default_factory=list)
    literature_evidence: list[LiteratureEvidence] = Field(default_factory=list)
    evidence_records: list[EvidenceRecord] = Field(default_factory=list)
    normalized_claim: NormalizedClaim | None = None
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def source_neutral_references_are_closed(self) -> "EvidenceEnrichmentResult":
        if self.normalized_claim is not None:
            EvidenceStore(
                records=self.evidence_records,
                claims=[self.normalized_claim],
            )
        return self


def _stable_claim_id(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"evidence-claim.{digest}"


def _stable_literature_id(pmid: str, content_hash: str) -> str:
    digest = hashlib.sha256(
        f"pubmed|{pmid}|{content_hash}".encode("utf-8")
    ).hexdigest()[:24]
    return f"lit.pubmed.{digest}"


class EvidenceEnrichmentService:
    """Enrich one PrimeKG edge into a question-eligible claim."""

    def __init__(
        self,
        *,
        ctd_index: CtdEvidenceIndex | None,
        model: Any,
        pubmed: PubMedClient | None = None,
        max_abstracts: int = 5,
    ) -> None:
        self.ctd_index = ctd_index
        self.model = model
        self.pubmed = pubmed or PubMedClient()
        self.max_abstracts = max_abstracts

    def enrich(
        self,
        edge: KGEvidence,
        *,
        seed_entity_id: str,
        answer_entity_id: str,
    ) -> EvidenceEnrichmentResult:
        upstream_source = str(edge.metadata.get("upstream_source") or "")
        if upstream_source.casefold() != "ctd":
            return EvidenceEnrichmentResult(
                edge_id=edge.evidence_id,
                status=EnrichmentStatus.UNMAPPED,
                reason_codes=["source_not_ctd"],
            )
        seed = edge.head if edge.head.canonical_id == seed_entity_id else edge.tail
        answer = (
            edge.tail
            if edge.head.canonical_id == seed_entity_id
            else edge.head
        )
        chemical_id = (
            seed.canonical_id.split(":")[-1] if seed.canonical_id else ""
        )
        if not chemical_id:
            return EvidenceEnrichmentResult(
                edge_id=edge.evidence_id,
                status=EnrichmentStatus.UNMAPPED,
                reason_codes=["missing_chemical_id"],
            )
        ctd_records = (
            self.ctd_index.lookup(
                chemical_id=str(chemical_id),
                disease_name=answer.canonical_name or answer.raw_mention,
            )
            if self.ctd_index is not None
            else []
        )
        direct_pmids = sorted(
            {
                pmid
                for record in ctd_records
                if record.direct_evidence
                for pmid in record.pubmed_ids
            }
        )
        query = (
            f'"{seed.canonical_name}" AND '
            f'"{answer.canonical_name}"'
        )
        pmids = direct_pmids
        if not pmids:
            pmids = self.pubmed.search(query, limit=self.max_abstracts)
        pubmed_records = self.pubmed.fetch(pmids, limit=self.max_abstracts)
        readable_pubmed = [
            item for item in pubmed_records if item.abstract.strip()
        ]
        if not readable_pubmed:
            return EvidenceEnrichmentResult(
                edge_id=edge.evidence_id,
                status=EnrichmentStatus.INSUFFICIENT,
                ctd_records=ctd_records,
                reason_codes=["no_readable_pubmed_abstract"],
            )
        request = StructuredGenerationRequest(
            invocation_id=f"enrich.{edge.evidence_id}",
            role="evidence_curator",
            system_prompt_version="curator-v1",
            payload={
                "kg_edge": {
                    "seed_entity_id": seed_entity_id,
                    "answer_entity_id": answer_entity_id,
                    "seed_name": seed.canonical_name,
                    "answer_name": answer.canonical_name,
                    "relation": edge.relation,
                },
                "ctd_records": [
                    {
                        "direct_evidence": item.direct_evidence,
                        "inference_gene_count": len(item.inference_genes),
                        "inference_genes_sample": item.inference_genes[:20],
                        "inference_score": item.inference_score,
                        "pubmed_id_count": len(item.pubmed_ids),
                        "pubmed_ids_sample": item.pubmed_ids[:20],
                    }
                    for item in ctd_records
                ],
                "pubmed_records": [
                    {
                        "pmid": item.pmid,
                        "title": item.title,
                        "abstract": item.abstract,
                        "doi": item.doi,
                    }
                    for item in readable_pubmed
                ],
                "allowed_intents": [item.value for item in QuestionIntent],
                "rules": [
                    "the answer entity and seed entity are fixed",
                    "do not infer a risk direction unless the abstract states it",
                    "mark conflicting evidence as conflicting",
                    "cite only supplied PMIDs",
                ],
            },
            idempotency_key=(
                f"{edge.evidence_id}:evidence-enrichment:"
                f"{','.join(item.pmid for item in pubmed_records)}"
            ),
        )
        response = self.model.generate_structured(request, EvidenceAssessment)
        assessment = EvidenceAssessment.model_validate(response.parsed)
        known_pmids = {item.pmid for item in readable_pubmed}
        unknown_pmids = sorted(set(assessment.supported_pmid_ids) - known_pmids)
        if unknown_pmids:
            return EvidenceEnrichmentResult(
                edge_id=edge.evidence_id,
                status=EnrichmentStatus.INSUFFICIENT,
                ctd_records=ctd_records,
                pubmed_records=pubmed_records,
                reason_codes=["unknown_pmid_reference", *unknown_pmids],
            )
        if assessment.status is not EnrichmentStatus.SUPPORTED:
            return EvidenceEnrichmentResult(
                edge_id=edge.evidence_id,
                status=assessment.status,
                ctd_records=ctd_records,
                pubmed_records=pubmed_records,
                reason_codes=assessment.reason_codes,
            )
        if not assessment.supported_pmid_ids:
            return EvidenceEnrichmentResult(
                edge_id=edge.evidence_id,
                status=EnrichmentStatus.INSUFFICIENT,
                ctd_records=ctd_records,
                pubmed_records=pubmed_records,
                reason_codes=["supported_claim_without_pmid"],
            )
        literature_by_pmid: dict[str, LiteratureEvidence] = {}
        for item in readable_pubmed:
            if item.pmid not in assessment.supported_pmid_ids:
                continue
            text = item.abstract or item.title
            evidence_id = _stable_literature_id(item.pmid, item.content_hash)
            literature_by_pmid[item.pmid] = LiteratureEvidence(
                evidence_id=evidence_id,
                document_id=f"pmid:{item.pmid}",
                pmid=item.pmid,
                doi=item.doi,
                title=item.title or f"PubMed article {item.pmid}",
                evidence_text=text,
                location={
                    "start": 0,
                    "end": len(text),
                    "content_hash": item.content_hash,
                    "provider_snapshot_id": item.snapshot_id,
                    "support_verdict": "supports",
                    "kg_evidence_id": edge.evidence_id,
                },
                entities=[seed, answer],
                retrieval_query=(
                    query
                ),
                retrieval_rank=(
                    assessment.supported_pmid_ids.index(item.pmid) + 1
                ),
                retrieved_at=item.retrieved_at,
            )
        snapshots = sorted(
            {
                *[item.source_snapshot_id for item in ctd_records],
                *[item.snapshot_id for item in readable_pubmed],
            }
        )
        claim = NormalizedEvidenceClaim(
            claim_id=_stable_claim_id(
                edge.evidence_id,
                assessment.question_intent.value,
                *assessment.supported_pmid_ids,
            ),
            kg_edge_id=edge.evidence_id,
            seed_entity_id=seed_entity_id,
            answer_entity_id=answer_entity_id,
            answer_entity_type=answer.entity_type,
            relation=edge.relation,
            question_intent=assessment.question_intent,
            support_level=assessment.support_level,
            polarity=assessment.polarity,
            claim_text=assessment.claim_text,
            source_snapshot_ids=snapshots,
            ctd_source_id=(
                ctd_records[0].content_hash if ctd_records else None
            ),
            pubmed_ids=list(assessment.supported_pmid_ids),
            literature_evidence_ids=[
                literature_by_pmid[pmid].evidence_id
                for pmid in assessment.supported_pmid_ids
                if pmid in literature_by_pmid
            ],
            reason_codes=list(assessment.reason_codes),
        )
        source_neutral_records = [
            evidence_from_ctd(
                item,
                subject_entity_id=seed_entity_id,
                object_entity_id=answer_entity_id,
                retrieved_at=edge.retrieved_at,
            )
            for item in ctd_records
        ]
        pubmed_evidence_by_pmid = {
            item.pmid: evidence_from_pubmed(item) for item in readable_pubmed
        }
        source_neutral_records.extend(pubmed_evidence_by_pmid.values())
        kg_snapshot_id = str(
            edge.retrieval_action.get("snapshot_id")
            or edge.metadata.get("provider_snapshot_id")
            or ""
        )
        kg_record = (
            evidence_from_kg(edge, provider_snapshot_id=kg_snapshot_id)
            if kg_snapshot_id
            else None
        )
        if kg_record is not None:
            source_neutral_records.append(kg_record)
        source_neutral_records = list(
            {
                item.evidence_id: item for item in source_neutral_records
            }.values()
        )
        normalized_claim = migrate_legacy_claim(
            claim,
            qualifiers={
                key: value
                for key, value in {
                    "study_type": assessment.study_type,
                    "population": assessment.population,
                }.items()
                if value is not None
            },
            supporting_evidence_ids=[
                pubmed_evidence_by_pmid[pmid].evidence_id
                for pmid in assessment.supported_pmid_ids
            ],
            context_evidence_ids=[
                *[
                    item.evidence_id
                    for item in source_neutral_records
                    if item.provider_id == "ctd"
                ],
                *([kg_record.evidence_id] if kg_record is not None else []),
            ],
        )
        return EvidenceEnrichmentResult(
            edge_id=edge.evidence_id,
            status=EnrichmentStatus.SUPPORTED,
            claim=claim,
            ctd_records=ctd_records,
            pubmed_records=readable_pubmed,
            literature_evidence=list(literature_by_pmid.values()),
            evidence_records=source_neutral_records,
            normalized_claim=normalized_claim,
        )
