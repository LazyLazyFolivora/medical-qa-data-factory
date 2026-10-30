"""Join PrimeKG CTD edges, PubMed text and a structured evidence claim."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import Field, model_validator

from ..domain import KGEvidence, LiteratureEvidence
from ..domain.common import ContractModel
from ..generation.evidence_fusion import (
    CtdEvidenceExtraction,
    CtdEvidenceFusion,
    CtdEvidenceFusionStatus,
    PubMedFindingVerdict,
    StudyContextIntent,
    fuse_ctd_pubmed_evidence,
)
from ..llm.contracts import StructuredGenerationRequest
from .ctd import CtdEvidenceIndex
from .contracts import (
    EvidenceRecord,
    EvidenceStore,
    NormalizedClaim,
    evidence_from_ctd,
    evidence_from_kg,
)
from .models import (
    CtdEvidenceRecord,
    EnrichmentStatus,
    NormalizedEvidenceClaim,
    QuestionIntent,
    PubMedEvidenceRecord,
)
from .pubmed import PubMedClient, PubMedProviderStatus
from .pubmed_evidence import (
    PubMedCorpusManifest,
    PubMedEvidenceSpan,
    build_span_candidates,
    build_corpus_manifest,
)


class EvidenceEnrichmentResult(ContractModel):
    edge_id: str = Field(min_length=1)
    status: EnrichmentStatus
    claim: NormalizedEvidenceClaim | None = None
    ctd_records: list[CtdEvidenceRecord] = Field(default_factory=list)
    pubmed_records: list[PubMedEvidenceRecord] = Field(default_factory=list)
    literature_evidence: list[LiteratureEvidence] = Field(default_factory=list)
    pubmed_spans: list[PubMedEvidenceSpan] = Field(default_factory=list)
    pubmed_corpus_manifest: PubMedCorpusManifest | None = None
    fusion: CtdEvidenceFusion | None = None
    evidence_records: list[EvidenceRecord] = Field(default_factory=list)
    normalized_claim: NormalizedClaim | None = None
    reason_codes: list[str] = Field(default_factory=list)
    retrieval_query: str | None = None
    retrieval_mode: str | None = None

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
        if max_abstracts < 1:
            raise ValueError("max_abstracts must be positive")
        self.max_abstracts = max_abstracts

    def enrich(
        self,
        edge: KGEvidence,
        *,
        seed_entity_id: str,
        answer_entity_id: str,
        retrieval_query: str | None = None,
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
        query = retrieval_query or (
            f'"{seed.canonical_name}" AND '
            f'"{answer.canonical_name}"'
        )
        searched_pmids: list[str] = []
        search_required = retrieval_query is not None or not direct_pmids
        if search_required:
            search_result = self.pubmed.search_result(
                query, limit=self.max_abstracts
            )
            if search_result.status is not PubMedProviderStatus.SUCCESS:
                return EvidenceEnrichmentResult(
                    edge_id=edge.evidence_id,
                    status=EnrichmentStatus.INSUFFICIENT,
                    ctd_records=ctd_records,
                    retrieval_query=query,
                    retrieval_mode="search",
                    reason_codes=[
                        (
                            "pubmed_empty_search"
                            if search_result.status
                            is PubMedProviderStatus.EMPTY
                            else f"pubmed_{search_result.status.value}"
                        )
                    ],
                )
            searched_pmids = search_result.pmids
        pmids = sorted(set(direct_pmids) | set(searched_pmids))
        retrieval_mode = (
            "search_plus_ctd_pmids"
            if searched_pmids and direct_pmids
            else "search"
            if searched_pmids
            else "ctd_pmids"
        )
        fetch_result = self.pubmed.fetch_result(
            pmids, limit=self.max_abstracts
        )
        if fetch_result.status not in {
            PubMedProviderStatus.SUCCESS,
            PubMedProviderStatus.NO_ABSTRACT,
        }:
            return EvidenceEnrichmentResult(
                edge_id=edge.evidence_id,
                status=EnrichmentStatus.INSUFFICIENT,
                ctd_records=ctd_records,
                retrieval_query=query,
                retrieval_mode=retrieval_mode,
                reason_codes=[
                    (
                        "pubmed_empty_result"
                        if fetch_result.status is PubMedProviderStatus.EMPTY
                        else f"pubmed_{fetch_result.status.value}"
                    )
                ],
            )
        pubmed_records = fetch_result.records
        if fetch_result.truncated:
            return EvidenceEnrichmentResult(
                edge_id=edge.evidence_id,
                status=EnrichmentStatus.INSUFFICIENT,
                ctd_records=ctd_records,
                pubmed_records=pubmed_records,
                retrieval_query=query,
                retrieval_mode=retrieval_mode,
                reason_codes=["pubmed_fetch_truncated"],
            )
        if fetch_result.unexpected_pmids:
            return EvidenceEnrichmentResult(
                edge_id=edge.evidence_id,
                status=EnrichmentStatus.INSUFFICIENT,
                ctd_records=ctd_records,
                pubmed_records=pubmed_records,
                retrieval_query=query,
                retrieval_mode=retrieval_mode,
                reason_codes=[
                    "pubmed_unexpected_records",
                    *fetch_result.unexpected_pmids,
                ],
            )
        if fetch_result.missing_pmids:
            return EvidenceEnrichmentResult(
                edge_id=edge.evidence_id,
                status=EnrichmentStatus.INSUFFICIENT,
                ctd_records=ctd_records,
                pubmed_records=pubmed_records,
                retrieval_query=query,
                retrieval_mode=retrieval_mode,
                reason_codes=[
                    "pubmed_missing_records",
                    *fetch_result.missing_pmids,
                ],
            )
        corpus_manifest = build_corpus_manifest(
            pubmed_records,
            requested_pmids=fetch_result.requested_pmids,
        )
        readable_pubmed = [
            item for item in pubmed_records if item.abstract.strip()
        ]
        if not readable_pubmed:
            return EvidenceEnrichmentResult(
                edge_id=edge.evidence_id,
                status=EnrichmentStatus.INSUFFICIENT,
                ctd_records=ctd_records,
                pubmed_records=pubmed_records,
                pubmed_corpus_manifest=corpus_manifest,
                retrieval_query=query,
                retrieval_mode=retrieval_mode,
                reason_codes=["pubmed_no_abstract"],
            )
        request = StructuredGenerationRequest(
            invocation_id=f"enrich.{edge.evidence_id}",
            role="evidence_curator",
            system_prompt_version="curator-v2",
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
                "citation_candidates": build_span_candidates(
                    readable_pubmed
                ),
                "allowed_intents": [
                    item.value for item in StudyContextIntent
                ],
                "rules": [
                    "the answer entity and seed entity are fixed",
                    "assess every supplied PMID exactly once",
                    "use supports, contradicts, context_only or uncertain",
                    "do not infer a risk direction unless the abstract states it",
                    "preserve population, study type, exposure and time qualifiers",
                    "do not upgrade association into causation or efficacy",
                    "cite an exact abstract character span and verbatim quote",
                    (
                        "copy citation pmid, text_field, start, end and quote "
                        "from exactly one supplied citation_candidates entry"
                    ),
                ],
            },
            idempotency_key=(
                f"{edge.evidence_id}:evidence-enrichment:"
                f"{','.join(item.pmid for item in pubmed_records)}"
            ),
        )
        response = self.model.generate_structured(
            request,
            CtdEvidenceExtraction,
        )
        extraction = CtdEvidenceExtraction.model_validate(response.parsed)
        fusion = fuse_ctd_pubmed_evidence(
            ctd_records=ctd_records,
            pubmed_records=pubmed_records,
            extraction=extraction,
            seed_entity_id=seed_entity_id,
            answer_entity_id=answer_entity_id,
            ctd_retrieved_at=edge.retrieved_at,
        )
        pubmed_spans = [item.span for item in fusion.pubmed_assessments]
        if fusion.status is not CtdEvidenceFusionStatus.ELIGIBLE:
            status = (
                EnrichmentStatus.CONFLICTING
                if fusion.status is CtdEvidenceFusionStatus.CONFLICTING
                else EnrichmentStatus.INSUFFICIENT
            )
            return EvidenceEnrichmentResult(
                edge_id=edge.evidence_id,
                status=status,
                ctd_records=ctd_records,
                pubmed_records=pubmed_records,
                pubmed_spans=pubmed_spans,
                pubmed_corpus_manifest=corpus_manifest,
                fusion=fusion,
                evidence_records=fusion.evidence_records,
                retrieval_query=query,
                retrieval_mode=retrieval_mode,
                reason_codes=fusion.reason_codes,
            )

        assert fusion.claim is not None
        support_assessments = sorted(
            (
                item
                for item in fusion.pubmed_assessments
                if item.verdict is PubMedFindingVerdict.SUPPORTS
            ),
            key=lambda item: item.pmid,
        )
        primary = support_assessments[0]
        assert primary.intent is not None
        question_intent = QuestionIntent(primary.intent.value)
        records_by_pmid = {item.pmid: item for item in readable_pubmed}
        literature_by_pmid: dict[str, LiteratureEvidence] = {}
        for assessment in support_assessments:
            item = records_by_pmid[assessment.pmid]
            evidence_id = _stable_literature_id(
                item.pmid,
                assessment.span.quote_hash,
            )
            literature_by_pmid[item.pmid] = LiteratureEvidence(
                evidence_id=evidence_id,
                document_id=f"pmid:{item.pmid}",
                pmid=item.pmid,
                doi=item.doi,
                title=item.title or f"PubMed article {item.pmid}",
                evidence_text=assessment.span.quote,
                location={
                    "start": assessment.span.start,
                    "end": assessment.span.end,
                    "content_hash": assessment.span.quote_hash,
                    "provider_snapshot_id": item.snapshot_id,
                    "support_verdict": assessment.verdict.value,
                    "kg_evidence_id": edge.evidence_id,
                },
                entities=[seed, answer],
                retrieval_query=query,
                retrieval_rank=(
                    [item.pmid for item in support_assessments].index(
                        assessment.pmid
                    )
                    + 1
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
                question_intent.value,
                *[item.pmid for item in support_assessments],
            ),
            kg_edge_id=edge.evidence_id,
            seed_entity_id=seed_entity_id,
            answer_entity_id=answer_entity_id,
            answer_entity_type=answer.entity_type,
            relation=edge.relation,
            question_intent=question_intent,
            support_level=primary.support_level,
            polarity=primary.polarity,
            claim_text=primary.claim_text,
            source_snapshot_ids=snapshots,
            ctd_source_id=hashlib.sha256(
                "\n".join(
                    sorted(item.content_hash for item in ctd_records)
                ).encode("utf-8")
            ).hexdigest(),
            pubmed_ids=[item.pmid for item in support_assessments],
            literature_evidence_ids=[
                literature_by_pmid[item.pmid].evidence_id
                for item in support_assessments
            ],
            reason_codes=list(fusion.reason_codes),
        )
        source_neutral_records = list(fusion.evidence_records)
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
        normalized_claim = fusion.claim
        if kg_record is not None:
            normalized_claim = NormalizedClaim.create(
                subject_id=normalized_claim.subject_id,
                predicate_family=normalized_claim.predicate_family,
                object_id=normalized_claim.object_id,
                qualifiers=normalized_claim.qualifiers,
                polarity=normalized_claim.polarity,
                normalizer_version=normalized_claim.normalizer_version,
                supporting_evidence_ids=(
                    normalized_claim.supporting_evidence_ids
                ),
                contradicting_evidence_ids=(
                    normalized_claim.contradicting_evidence_ids
                ),
                context_evidence_ids=[
                    *normalized_claim.context_evidence_ids,
                    kg_record.evidence_id,
                ],
                claim_text=normalized_claim.claim_text,
                legacy_claim_id=normalized_claim.legacy_claim_id,
            )
        return EvidenceEnrichmentResult(
            edge_id=edge.evidence_id,
            status=EnrichmentStatus.SUPPORTED,
            claim=claim,
            ctd_records=ctd_records,
            pubmed_records=pubmed_records,
            literature_evidence=list(literature_by_pmid.values()),
            pubmed_spans=pubmed_spans,
            pubmed_corpus_manifest=corpus_manifest,
            fusion=fusion,
            evidence_records=source_neutral_records,
            normalized_claim=normalized_claim,
            retrieval_query=query,
            retrieval_mode=retrieval_mode,
        )
