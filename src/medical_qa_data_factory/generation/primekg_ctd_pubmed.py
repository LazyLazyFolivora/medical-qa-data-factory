"""Validate CTD/PubMed evidence for a PrimeKG-native task candidate."""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any, Literal

from pydantic import Field

from ..domain import KGEvidence
from ..domain.common import ContractModel
from ..knowledge_graph.contracts import (
    GraphEntity,
    NeighborQuery,
)
from ..knowledge_graph.ports import KnowledgeGraphProvider
from ..evidence_enrichment.ctd import (
    CtdDiseaseCrosswalk,
    CtdEvidenceIndex,
    CtdMappingStatus,
)
from ..evidence_enrichment.pubmed import (
    PubMedClient,
    PubMedFetchResult,
    PubMedProviderStatus,
)
from ..evidence_enrichment.pubmed_evidence import (
    PubMedCorpusManifest,
    build_corpus_manifest,
)
from ..llm.contracts import StructuredGenerationRequest
from .evidence_fusion import (
    CtdEvidenceExtraction,
    CtdEvidenceFusion,
    CtdEvidenceFusionStatus,
    StudyContextIntent,
    fuse_ctd_pubmed_evidence,
)


EvidenceKind = Literal["marker/mechanism", "therapeutic"]


class PrimeKgCtdDiseaseMapping(ContractModel):
    status: CtdMappingStatus
    primekg_disease_id: str
    ctd_disease_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)


class PrimeKgCtdDiseaseMapper:
    """Resolve a PrimeKG disease to CTD disease IDs without model guessing."""

    def __init__(self, crosswalk: CtdDiseaseCrosswalk) -> None:
        self.by_primekg = {
            entry.primekg_disease_id: sorted(set(entry.ctd_disease_ids))
            for entry in crosswalk.entries
        }
        uses: dict[str, set[str]] = {}
        for primekg_id, ctd_ids in self.by_primekg.items():
            for ctd_id in ctd_ids:
                uses.setdefault(ctd_id, set()).add(primekg_id)
        self.ambiguous_ctd_ids = {
            ctd_id for ctd_id, primekg_ids in uses.items() if len(primekg_ids) > 1
        }

    def resolve(self, primekg_disease_id: str) -> PrimeKgCtdDiseaseMapping:
        ctd_ids = self.by_primekg.get(primekg_disease_id, [])
        if not ctd_ids:
            return PrimeKgCtdDiseaseMapping(
                status=CtdMappingStatus.UNMAPPED,
                primekg_disease_id=primekg_disease_id,
                reason_codes=["ctd_mapping_unmapped"],
            )
        if any(item in self.ambiguous_ctd_ids for item in ctd_ids):
            return PrimeKgCtdDiseaseMapping(
                status=CtdMappingStatus.AMBIGUOUS,
                primekg_disease_id=primekg_disease_id,
                ctd_disease_ids=ctd_ids,
                reason_codes=["ctd_mapping_ambiguous"],
            )
        if len(ctd_ids) != 1:
            return PrimeKgCtdDiseaseMapping(
                status=CtdMappingStatus.AMBIGUOUS,
                primekg_disease_id=primekg_disease_id,
                ctd_disease_ids=ctd_ids,
                reason_codes=["ctd_mapping_ambiguous"],
            )
        return PrimeKgCtdDiseaseMapping(
            status=CtdMappingStatus.MAPPED_CROSSWALK,
            primekg_disease_id=primekg_disease_id,
            ctd_disease_ids=ctd_ids,
        )


class PrimeKgCtdPubMedResult(ContractModel):
    status: Literal["accepted", "rejected"]
    primekg_subject_id: str
    primekg_object_id: str
    mapping_status: CtdMappingStatus
    ctd_record_ids: list[str] = Field(default_factory=list)
    pubmed_corpus: PubMedCorpusManifest | None = None
    fusion: CtdEvidenceFusion | None = None
    reason_codes: list[str] = Field(default_factory=list)


class PrimeKgCtdPubMedService:
    """Run a deterministic PrimeKG/CTD/PubMed evidence pipeline."""

    version = "primekg-ctd-pubmed-v1"

    def __init__(
        self,
        *,
        ctd_index: CtdEvidenceIndex,
        disease_mapper: PrimeKgCtdDiseaseMapper,
        pubmed: PubMedClient,
        model: Any,
        primekg_edge_verifier: Callable[[KGEvidence], bool],
    ) -> None:
        self.ctd_index = ctd_index
        self.disease_mapper = disease_mapper
        self.pubmed = pubmed
        self.model = model
        self.primekg_edge_verifier = primekg_edge_verifier

    def validate(
        self,
        *,
        edge: KGEvidence,
        seed_entity_id: str,
        answer_entity_id: str,
        evidence_kind: EvidenceKind,
    ) -> PrimeKgCtdPubMedResult:
        if evidence_kind not in {"marker/mechanism", "therapeutic"}:
            raise ValueError(f"unsupported evidence kind: {evidence_kind}")
        if str(edge.metadata.get("upstream_source", "")).casefold() != "ctd":
            return self._reject(
                seed_entity_id,
                answer_entity_id,
                CtdMappingStatus.UNMAPPED,
                ["source_not_ctd"],
            )
        if (
            edge.head.canonical_id != seed_entity_id
            or edge.tail.canonical_id != answer_entity_id
        ):
            return self._reject(
                seed_entity_id,
                answer_entity_id,
                CtdMappingStatus.UNMAPPED,
                ["primekg_entity_mismatch"],
            )
        if not self.primekg_edge_verifier(edge):
            return self._reject(
                seed_entity_id,
                answer_entity_id,
                CtdMappingStatus.UNMAPPED,
                ["primekg_edge_mismatch"],
            )

        return self.evaluate_answer(
            seed_entity_id=seed_entity_id,
            answer_entity_id=answer_entity_id,
            subject_name=edge.head.canonical_name or seed_entity_id,
            object_name=edge.tail.canonical_name or answer_entity_id,
            evidence_kind=evidence_kind,
            source_edge_id=edge.evidence_id,
            retrieved_at=edge.retrieved_at,
        )

    def evaluate_answer(
        self,
        *,
        seed_entity_id: str,
        answer_entity_id: str,
        subject_name: str,
        object_name: str,
        evidence_kind: EvidenceKind,
        source_edge_id: str,
        retrieved_at,
    ) -> PrimeKgCtdPubMedResult:
        """Assess one graph answer without requiring that edge's source.

        Candidate discovery separately requires a CTD-sourced PrimeKG edge.
        Competitor evaluation must still inspect every PrimeKG answer,
        including answers reached through other PrimeKG sources.
        """

        if evidence_kind not in {"marker/mechanism", "therapeutic"}:
            raise ValueError(f"unsupported evidence kind: {evidence_kind}")
        mapping = self.disease_mapper.resolve(answer_entity_id)
        if mapping.status is not CtdMappingStatus.MAPPED_CROSSWALK:
            return self._reject(
                seed_entity_id,
                answer_entity_id,
                mapping.status,
                mapping.reason_codes,
            )

        chemical_id = _native_id(seed_entity_id)
        records = self.ctd_index.enumerate_diseases(
            chemical_id=chemical_id
        )
        target_ids = set(mapping.ctd_disease_ids)
        direct_records = [
            record
            for record in records
            if record.disease_id in target_ids
            and evidence_kind in record.direct_evidence
        ]
        record_ids = sorted(
            record.source_record_locator or record.content_hash
            for record in direct_records
        )
        if not direct_records:
            return self._reject(
                seed_entity_id,
                answer_entity_id,
                mapping.status,
                ["ctd_direct_evidence_missing"],
                ctd_record_ids=record_ids,
            )
        pmids = sorted(
            {
                pmid
                for record in direct_records
                for pmid in record.pubmed_ids
                if pmid
            }
        )
        if not pmids:
            return self._reject(
                seed_entity_id,
                answer_entity_id,
                mapping.status,
                ["pubmed_document_missing"],
                ctd_record_ids=record_ids,
            )
        fetched = self.pubmed.fetch_result(pmids, limit=None)
        pubmed_reasons = _pubmed_failure_reasons(fetched)
        if pubmed_reasons:
            return self._reject(
                seed_entity_id,
                answer_entity_id,
                mapping.status,
                pubmed_reasons,
                ctd_record_ids=record_ids,
            )
        manifest = build_corpus_manifest(
            fetched.records,
            requested_pmids=pmids,
        )
        request = StructuredGenerationRequest(
            invocation_id=(
                "primekg-ctd-pubmed."
                + hashlib.sha256(
                    f"{source_edge_id}|{evidence_kind}".encode("utf-8")
                ).hexdigest()[:16]
            ),
            role="evidence_curator",
            system_prompt_version="curator-v2",
            payload={
                "primekg_edge": {
                    "subject_id": seed_entity_id,
                    "object_id": answer_entity_id,
                    "subject_name": subject_name,
                    "object_name": object_name,
                },
                "ctd_disease_ids": mapping.ctd_disease_ids,
                "allowed_evidence_kind": evidence_kind,
                "allowed_intents": [item.value for item in StudyContextIntent],
                "pubmed_records": [
                    record.model_dump(mode="json")
                    for record in fetched.records
                ],
                "rules": [
                    "assess every supplied PMID exactly once",
                    "use supports, contradicts, context_only or uncertain",
                    "quote a verbatim abstract span with exact offsets",
                    "do not infer efficacy or causation",
                    "preserve only study qualifiers stated in the abstract",
                ],
            },
            idempotency_key=(
                f"{source_edge_id}:{evidence_kind}:"
                f"{','.join(pmids)}"
            ),
        )
        response = self.model.generate_structured(
            request,
            CtdEvidenceExtraction,
        )
        extraction = CtdEvidenceExtraction.model_validate(response.parsed)
        fusion = fuse_ctd_pubmed_evidence(
            ctd_records=direct_records,
            pubmed_records=fetched.records,
            extraction=extraction,
            seed_entity_id=seed_entity_id,
            answer_entity_id=answer_entity_id,
            ctd_retrieved_at=retrieved_at,
        )
        if fusion.status is not CtdEvidenceFusionStatus.ELIGIBLE:
            return self._reject(
                seed_entity_id,
                answer_entity_id,
                mapping.status,
                fusion.reason_codes or ["claim_not_supported"],
                ctd_record_ids=record_ids,
                pubmed_corpus=manifest,
                fusion=fusion,
            )
        return PrimeKgCtdPubMedResult(
            status="accepted",
            primekg_subject_id=seed_entity_id,
            primekg_object_id=answer_entity_id,
            mapping_status=mapping.status,
            ctd_record_ids=record_ids,
            pubmed_corpus=manifest,
            fusion=fusion,
        )

    @staticmethod
    def _reject(
        seed_entity_id: str,
        answer_entity_id: str,
        mapping_status: CtdMappingStatus,
        reason_codes: list[str],
        *,
        ctd_record_ids: list[str] | None = None,
        pubmed_corpus: PubMedCorpusManifest | None = None,
        fusion: CtdEvidenceFusion | None = None,
    ) -> PrimeKgCtdPubMedResult:
        return PrimeKgCtdPubMedResult(
            status="rejected",
            primekg_subject_id=seed_entity_id,
            primekg_object_id=answer_entity_id,
            mapping_status=mapping_status,
            ctd_record_ids=ctd_record_ids or [],
            pubmed_corpus=pubmed_corpus,
            fusion=fusion,
            reason_codes=sorted(set(reason_codes)),
        )


def _native_id(canonical_id: str) -> str:
    parts = canonical_id.split(":")
    if len(parts) < 3:
        raise ValueError(f"invalid PrimeKG canonical ID: {canonical_id}")
    return parts[-1]


def _pubmed_failure_reasons(result: PubMedFetchResult) -> list[str]:
    reasons: list[str] = []
    if result.status not in {
        PubMedProviderStatus.SUCCESS,
        PubMedProviderStatus.NO_ABSTRACT,
    }:
        reasons.append(
            "pubmed_empty"
            if result.status is PubMedProviderStatus.EMPTY
            else f"pubmed_{result.status.value}"
        )
    if result.truncated:
        reasons.append("pubmed_fetch_truncated")
    if result.missing_pmids:
        reasons.append("pubmed_document_missing")
    if result.unexpected_pmids:
        reasons.append("pubmed_unexpected_records")
    if result.no_abstract_pmids or (
        result.status is PubMedProviderStatus.NO_ABSTRACT
        and not any(record.abstract.strip() for record in result.records)
    ):
        reasons.append("pubmed_no_abstract")
    return reasons


class PrimeKGCtdEdgeVerifier:
    """Verify a candidate CTD-sourced edge against a pinned PrimeKG provider."""

    def __init__(
        self,
        provider: KnowledgeGraphProvider,
        *,
        page_size: int = 500,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        self.provider = provider
        self.page_size = page_size

    def __call__(self, edge: KGEvidence) -> bool:
        head = _graph_entity(edge.head.canonical_id, edge.head.canonical_name or "")
        cursor: str | None = None
        while True:
            try:
                result = self.provider.neighbors(
                    NeighborQuery(
                        entity=head,
                        direction="outgoing",
                        relations=[edge.relation],
                        target_types=(
                            [edge.tail.entity_type]
                            if edge.tail.entity_type
                            else []
                        ),
                        limit=self.page_size,
                        cursor=cursor,
                    )
                )
            except Exception:
                return False
            if any(
                candidate.source == "CTD"
                and candidate.relation == edge.relation
                and candidate.head.canonical_id == edge.head.canonical_id
                and candidate.tail.canonical_id == edge.tail.canonical_id
                for candidate in result.edges
            ):
                return True
            if not result.truncated:
                return False
            if not result.next_cursor or result.next_cursor == cursor:
                return False
            cursor = result.next_cursor


def _graph_entity(canonical_id: str, name: str) -> GraphEntity:
    parts = canonical_id.split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"invalid PrimeKG canonical ID: {canonical_id}")
    provider_id, entity_type, native_id = parts
    return GraphEntity(
        provider_id=provider_id,
        native_id=native_id,
        canonical_id=canonical_id,
        name=name or canonical_id,
        entity_type=entity_type,
    )
