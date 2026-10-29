"""Adapters from existing data sources to the material-provider boundary."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..evidence_enrichment.pubmed import (
    PubMedClient,
    PubMedProviderError,
    PubMedProviderStatus,
)
from ..evidence_enrichment.ctd import CtdEvidenceIndex
from ..knowledge_graph.contracts import EntityLookup, NeighborQuery
from .evidence_provider import (
    EvidenceProviderCapability,
    EvidenceProviderRequest,
    EvidenceProviderResult,
    ProviderTerminalStatus,
    SideEffectClass,
)
from .objective_question_contracts import text_digest
from .objective_question_persistence import immutable_blob_ref


class PrimeKgMaterialProvider:
    def __init__(self, provider: Any) -> None:
        self.provider = provider
        snapshot = provider.snapshot()
        self.capability = EvidenceProviderCapability(
            provider_id=provider.provider_id,
            provider_version=snapshot.adapter_version,
            side_effect_class=SideEffectClass.READ_ONLY,
            supports_idempotency=True,
            supports_status_lookup=False,
            safe_to_repeat=True,
        )

    def retrieve(self, request: EvidenceProviderRequest) -> EvidenceProviderResult:
        query = request.query.get(request.provider_id, request.query)
        if not isinstance(query, dict):
            query = request.query
        try:
            resolution = self.provider.resolve_entity(
                EntityLookup(
                    mention=str(query.get("mention") or query.get("term") or ""),
                    native_id=query.get("native_id"),
                    entity_types=list(query.get("entity_types") or []),
                    limit=10,
                )
            )
            if resolution.selected is None:
                return _empty_result(request)
            edges = self.provider.neighbors(
                NeighborQuery(
                    entity=resolution.selected,
                    direction="both",
                    target_types=list(query.get("target_types") or []),
                    relations=list(query.get("relations") or []),
                    limit=request.material_limit,
                )
            )
        except Exception as exc:
            return _failure_result(
                request,
                category="PRIMEKG_QUERY_ERROR",
                message=str(exc),
            )
        if not edges.edges:
            return _empty_result(request)

        materials: list[dict[str, Any]] = []
        anchors: list[dict[str, Any]] = []
        raw_contents: dict[str, str] = {}
        for edge in edges.edges:
            content = (
                f"{edge.head.name} --{edge.relation}--> {edge.tail.name}\n"
                f"head_id={edge.head.canonical_id}; "
                f"tail_id={edge.tail.canonical_id}; "
                f"head_type={edge.head.entity_type}; "
                f"tail_type={edge.tail.entity_type}"
            )
            material_id = f"material.primekg.{edge.edge_id}"
            anchor_id = f"anchor.primekg.{edge.edge_id}"
            digest = text_digest(content)
            materials.append(
                {
                    "material_id": material_id,
                    "provider_id": self.capability.provider_id,
                    "provider_version": self.capability.provider_version,
                    "source_record_id": edge.edge_id,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "content_hash": digest,
                    "raw_content_ref": immutable_blob_ref(digest),
                    "material_type": "knowledge_graph_edge",
                    "retrieval_request_id": request.request_id,
                    "terminal_status": "SUCCEEDED",
                }
            )
            anchors.append(
                {
                    "anchor_id": anchor_id,
                    "material_id": material_id,
                    "document_id": f"primekg:{edge.edge_id}",
                    "section_or_field": "edge",
                    "start_offset": 0,
                    "end_offset": len(content),
                    "quoted_text": content,
                    "quote_hash": digest,
                }
            )
            raw_contents[material_id] = content
        return EvidenceProviderResult(
            request=request,
            terminal_status=ProviderTerminalStatus.SUCCEEDED,
            material_ids=[item["material_id"] for item in materials],
            anchor_ids=[item["anchor_id"] for item in anchors],
            materials=materials,
            anchors=anchors,
            raw_content_by_material_id=raw_contents,
            snapshot_id=edges.snapshot.data_snapshot_id,
            usage={
                "provider_calls": 2,
                "rows_examined": edges.trace.rows_examined or 0,
            },
        )

    def lookup_status(self, operation_key: str):
        return None


class PubMedMaterialProvider:
    def __init__(self, client: PubMedClient | None = None) -> None:
        self.client = client or PubMedClient()
        self.capability = EvidenceProviderCapability(
            provider_id="pubmed",
            provider_version="pubmed-eutils-v1",
            side_effect_class=SideEffectClass.READ_ONLY,
            supports_idempotency=True,
            supports_status_lookup=False,
            safe_to_repeat=True,
        )

    def retrieve(self, request: EvidenceProviderRequest) -> EvidenceProviderResult:
        term = str(request.query.get("term") or request.query.get("mention") or "")
        queries = [term]
        words = term.split()
        if len(words) > 1:
            queries.extend(
                [
                    " ".join(words[:6]),
                    " ".join(words[:2]),
                    words[0],
                ]
            )
        queries = list(dict.fromkeys(query for query in queries if query))
        search = None
        for query in queries:
            search = self.client.search_result(
                query, limit=request.material_limit
            )
            if search.status is not PubMedProviderStatus.EMPTY:
                break
        assert search is not None
        if search.status is PubMedProviderStatus.EMPTY:
            return _empty_result(request, snapshot_id=search.snapshot_id)
        if search.status is not PubMedProviderStatus.SUCCESS:
            return _failure_result(
                request,
                category=f"PUBMED_{search.status.value.upper()}",
                message=search.error_message or search.status.value,
                terminal=ProviderTerminalStatus.UNKNOWN
                if search.status in {
                    PubMedProviderStatus.TIMEOUT,
                    PubMedProviderStatus.NETWORK_ERROR,
                }
                else ProviderTerminalStatus.FAILED,
            )
        fetched = self.client.fetch_result(search.pmids, limit=request.material_limit)
        if not fetched.records:
            return _empty_result(
                request,
                snapshot_id=fetched.snapshot_id,
            )
        materials: list[dict[str, Any]] = []
        anchors: list[dict[str, Any]] = []
        raw_contents: dict[str, str] = {}
        for record in fetched.records:
            content = f"{record.title}\n{record.abstract}".strip()
            if not content:
                continue
            material_id = f"material.pubmed.{record.pmid}"
            anchor_id = f"anchor.pubmed.{record.pmid}"
            digest = text_digest(content)
            materials.append(
                {
                    "material_id": material_id,
                    "provider_id": "pubmed",
                    "provider_version": self.capability.provider_version,
                    "source_record_id": f"pmid:{record.pmid}",
                    "retrieved_at": record.retrieved_at.isoformat(),
                    "content_hash": digest,
                    "raw_content_ref": immutable_blob_ref(digest),
                    "material_type": "literature_abstract",
                    "retrieval_request_id": request.request_id,
                    "terminal_status": "SUCCEEDED",
                }
            )
            anchors.append(
                {
                    "anchor_id": anchor_id,
                    "material_id": material_id,
                    "document_id": f"pubmed:{record.pmid}",
                    "section_or_field": "title_and_abstract",
                    "start_offset": 0,
                    "end_offset": len(content),
                    "quoted_text": content,
                    "quote_hash": digest,
                }
            )
            raw_contents[material_id] = content
        if not materials:
            return _empty_result(request, snapshot_id=fetched.snapshot_id)
        return EvidenceProviderResult(
            request=request,
            terminal_status=ProviderTerminalStatus.SUCCEEDED,
            material_ids=[item["material_id"] for item in materials],
            anchor_ids=[item["anchor_id"] for item in anchors],
            materials=materials,
            anchors=anchors,
            raw_content_by_material_id=raw_contents,
            snapshot_id=fetched.snapshot_id,
            usage={
                "provider_calls": len(queries) + 1,
                "documents_retrieved": len(materials),
            },
        )

    def lookup_status(self, operation_key: str):
        return None


class CtdMaterialProvider:
    def __init__(self, index: CtdEvidenceIndex | None = None) -> None:
        self.index = index
        self.capability = EvidenceProviderCapability(
            provider_id="ctd",
            provider_version="ctd-evidence-index-v1",
            side_effect_class=SideEffectClass.READ_ONLY,
            supports_idempotency=True,
            supports_status_lookup=False,
            safe_to_repeat=True,
        )

    def retrieve(self, request: EvidenceProviderRequest) -> EvidenceProviderResult:
        if self.index is None:
            return _failure_result(
                request,
                category="CTD_INDEX_NOT_CONFIGURED",
                message="CTD index is not configured",
            )
        query = request.query.get("ctd", request.query)
        if not isinstance(query, dict):
            query = request.query
        chemical_id = str(
            query.get("chemical_id")
            or query.get("native_id")
            or ""
        ).strip()
        if not chemical_id:
            return _failure_result(
                request,
                category="CTD_CHEMICAL_ID_MISSING",
                message="CTD retrieval requires chemical_id",
            )
        try:
            records = self.index.enumerate_diseases(
                chemical_id=chemical_id
            )[: request.material_limit]
        except Exception as exc:
            return _failure_result(
                request,
                category="CTD_QUERY_ERROR",
                message=str(exc),
            )
        if not records:
            return _empty_result(request)
        materials: list[dict[str, Any]] = []
        anchors: list[dict[str, Any]] = []
        raw_contents: dict[str, str] = {}
        for record in records:
            content = (
                f"{record.chemical_name} ({record.chemical_id}) "
                f"--chemical_disease_association--> "
                f"{record.disease_name} ({record.disease_id or 'no id'})\n"
                f"direct_evidence={record.direct_evidence}; "
                f"inference_genes={record.inference_genes}; "
                f"pubmed_ids={record.pubmed_ids}"
            )
            identity = record.disease_id or record.disease_name
            material_id = f"material.ctd.{chemical_id}.{identity}"
            anchor_id = f"anchor.ctd.{chemical_id}.{identity}"
            digest = text_digest(content)
            materials.append(
                {
                    "material_id": material_id,
                    "provider_id": "ctd",
                    "provider_version": self.capability.provider_version,
                    "source_record_id": (
                        record.source_record_locator
                        or f"ctd:{chemical_id}:{identity}"
                    ),
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "content_hash": digest,
                    "raw_content_ref": immutable_blob_ref(digest),
                    "material_type": "ctd_chemical_disease_record",
                    "retrieval_request_id": request.request_id,
                    "terminal_status": "SUCCEEDED",
                }
            )
            anchors.append(
                {
                    "anchor_id": anchor_id,
                    "material_id": material_id,
                    "document_id": f"ctd:{chemical_id}:{identity}",
                    "section_or_field": "chemical_disease_record",
                    "start_offset": 0,
                    "end_offset": len(content),
                    "quoted_text": content,
                    "quote_hash": digest,
                }
            )
            raw_contents[material_id] = content
        return EvidenceProviderResult(
            request=request,
            terminal_status=ProviderTerminalStatus.SUCCEEDED,
            material_ids=[item["material_id"] for item in materials],
            anchor_ids=[item["anchor_id"] for item in anchors],
            materials=materials,
            anchors=anchors,
            raw_content_by_material_id=raw_contents,
            snapshot_id=self.index.metadata().get("snapshot_id"),
            usage={"provider_calls": 1, "documents_retrieved": len(materials)},
        )

    def lookup_status(self, operation_key: str):
        return None


def _empty_result(
    request: EvidenceProviderRequest,
    *,
    snapshot_id: str | None = None,
) -> EvidenceProviderResult:
    return EvidenceProviderResult(
        request=request,
        terminal_status=ProviderTerminalStatus.EMPTY,
        material_ids=[],
        anchor_ids=[],
        snapshot_id=snapshot_id,
    )


def _failure_result(
    request: EvidenceProviderRequest,
    *,
    category: str,
    message: str,
    terminal: ProviderTerminalStatus = ProviderTerminalStatus.FAILED,
) -> EvidenceProviderResult:
    return EvidenceProviderResult(
        request=request,
        terminal_status=terminal,
        material_ids=[],
        anchor_ids=[],
        error_category=category,
        usage={"error_message": message},
    )
