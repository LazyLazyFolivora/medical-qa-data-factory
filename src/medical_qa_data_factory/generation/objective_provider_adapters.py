"""Adapters from existing data sources to the material-provider boundary."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

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


JsonGetTransport = Callable[[str], Any]


def _requests_pubtator_json(url: str) -> Any:
    import requests

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = requests.get(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "medical-qa-data-factory/0.1",
                },
                timeout=40,
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.HTTPError(
                    f"temporary PubTator response: HTTP {response.status_code}",
                    response=response,
                )
            response.raise_for_status()
            return response.json()
        except (
            requests.Timeout,
            requests.ConnectionError,
        ) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.25 * attempt)
        except requests.HTTPError as exc:
            status = (
                exc.response.status_code
                if getattr(exc, "response", None) is not None
                else None
            )
            if status not in {429} and (status or 0) < 500:
                raise
            last_error = exc
            if attempt < 3:
                time.sleep(0.25 * attempt)
        except ValueError as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.25 * attempt)
    assert last_error is not None
    raise last_error


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
            direction = str(query.get("direction") or "both")
            if direction not in {"both", "incoming", "outgoing"}:
                direction = "both"
            edges = self.provider.neighbors(
                NeighborQuery(
                    entity=resolution.selected,
                    direction=direction,
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
    def __init__(
        self,
        client: PubMedClient | None = None,
        *,
        fulltext_transport: JsonGetTransport | None = None,
        max_fulltext_chars: int = 16000,
    ) -> None:
        self.client = client or PubMedClient()
        self.fulltext_transport = (
            fulltext_transport or _requests_pubtator_json
        )
        self.max_fulltext_chars = max(1000, max_fulltext_chars)
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
                    " ".join(words[:3]),
                ]
            )
        if len(words) > 6:
            # Only relax instruction-like queries to the leading term. Normal
            # short biomedical queries keep their full meaning.
            queries.append(words[0])
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

    def retrieve_details(
        self,
        request: EvidenceProviderRequest,
        *,
        material_ids: list[str],
    ) -> EvidenceProviderResult:
        pmids = [
            material_id.removeprefix("material.pubmed.")
            for material_id in material_ids
            if material_id.startswith("material.pubmed.")
        ]
        if not pmids:
            return _empty_result(request)
        url = (
            "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/"
            "publications/export/biocjson?pmids="
            + quote(",".join(pmids), safe=",")
        )
        try:
            payload = self.fulltext_transport(url)
            documents = _pubtator_documents(payload)
        except Exception as exc:
            return _failure_result(
                request,
                category="PUBMED_FULLTEXT_ERROR",
                message=str(exc),
            )
        materials: list[dict[str, Any]] = []
        anchors: list[dict[str, Any]] = []
        raw_contents: dict[str, str] = {}
        fallback_errors: list[dict[str, str]] = []
        for pmid in pmids:
            document = documents.get(pmid)
            sections = _pubtator_sections(document or {})
            if not _has_substantive_sections(sections):
                try:
                    pmc_payload = self.fulltext_transport(
                        "https://www.ncbi.nlm.nih.gov/research/bionlp/"
                        "RESTful/pmcoa.cgi/BioC_json/"
                        f"{quote(pmid)}/unicode"
                    )
                    pmc_documents = _pubtator_documents(pmc_payload)
                    pmc_document = (
                        pmc_documents.get(pmid)
                        or next(iter(pmc_documents.values()), {})
                    )
                    sections = _pubtator_sections(pmc_document)
                except Exception as exc:
                    fallback_errors.append(
                        {
                            "pmid": pmid,
                            "error_category": type(exc).__name__,
                            "error_message": str(exc),
                        }
                    )
            if not _has_substantive_sections(sections):
                continue
            content = "\n\n".join(
                f"[{section_name}]\n{text}"
                for section_name, text in sections.items()
            )
            if len(content) > self.max_fulltext_chars:
                content = content[: self.max_fulltext_chars].rstrip()
            material_id = f"material.pubmed.{pmid}"
            digest = text_digest(content)
            materials.append(
                {
                    "material_id": material_id,
                    "provider_id": "pubmed",
                    "provider_version": self.capability.provider_version,
                    "source_record_id": f"pmid:{pmid}",
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "content_hash": digest,
                    "raw_content_ref": immutable_blob_ref(digest),
                    "material_type": "literature_full_text",
                    "retrieval_request_id": request.request_id,
                    "terminal_status": "SUCCEEDED",
                }
            )
            raw_contents[material_id] = content
            cursor = 0
            for section_name, text in sections.items():
                quoted = text
                if cursor + len(quoted) > len(content):
                    quoted = quoted[: max(0, len(content) - cursor)]
                if not quoted:
                    break
                start = content.find(quoted, cursor)
                if start < 0:
                    start = cursor
                end = start + len(quoted)
                anchor_text = content[start:end]
                anchor_id = (
                    f"anchor.pubmed.{pmid}."
                    f"{_safe_anchor_field(section_name)}"
                )
                anchors.append(
                    {
                        "anchor_id": anchor_id,
                        "material_id": material_id,
                        "document_id": f"pubmed:{pmid}",
                        "section_or_field": section_name,
                        "start_offset": start,
                        "end_offset": end,
                        "quoted_text": anchor_text,
                        "quote_hash": text_digest(anchor_text),
                    }
                )
                cursor = end
        if not materials:
            return _empty_result(request)
        return EvidenceProviderResult(
            request=request,
            terminal_status=ProviderTerminalStatus.SUCCEEDED,
            material_ids=[item["material_id"] for item in materials],
            anchor_ids=[item["anchor_id"] for item in anchors],
            materials=materials,
            anchors=anchors,
            raw_content_by_material_id=raw_contents,
            snapshot_id=f"pubtator:{datetime.now(timezone.utc).date()}",
            usage={
                "provider_calls": 1,
                "full_text_documents": len(materials),
                "fallback_errors": fallback_errors,
            },
        )


def _pubtator_documents(payload: Any) -> dict[str, dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("PubTator3"), list):
            candidates = payload["PubTator3"]
        elif isinstance(payload.get("documents"), list):
            candidates = payload["documents"]
        else:
            candidates = [payload]
    elif isinstance(payload, list):
        candidates = payload
    else:
        return {}
    documents: dict[str, dict[str, Any]] = {}
    pending = list(candidates)
    while pending:
        item = pending.pop(0)
        if not isinstance(item, dict):
            continue
        nested = item.get("documents")
        if isinstance(nested, list):
            pending.extend(nested)
        pmid = _pubtator_pmid(item)
        if pmid:
            documents[pmid] = item
    return documents


def _pubtator_pmid(document: dict[str, Any]) -> str:
    for value in (
        document.get("pmid"),
        document.get("PMID"),
        (document.get("infons") or {}).get("article-id_pmid"),
        document.get("id"),
    ):
        normalized = str(value or "").strip()
        if normalized.upper().startswith("PMID:"):
            normalized = normalized.split(":", 1)[1]
        if normalized.isdigit():
            return normalized
    return ""


def _pubtator_sections(document: dict[str, Any]) -> dict[str, str]:
    sections: dict[str, str] = {}
    passages = document.get("passages")
    if not isinstance(passages, list):
        return sections
    for passage in passages:
        if not isinstance(passage, dict):
            continue
        infons = passage.get("infons") or {}
        section = str(
            infons.get("section_type")
            or infons.get("type")
            or "text"
        ).strip()
        text = str(passage.get("text") or "").strip()
        if not text:
            continue
        if section in sections:
            sections[section] = f"{sections[section]}\n{text}"
        else:
            sections[section] = text
    return sections


def _has_substantive_sections(sections: dict[str, str]) -> bool:
    return any(
        section.casefold()
        not in {
            "title",
            "abstract",
            "front",
            "abstract_title_1",
        }
        for section in sections
    )


def _safe_anchor_field(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() else "_"
        for character in value.casefold()
    ).strip("_")
    return cleaned or "text"


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
            )
            disease_filter = str(
                query.get("disease_name") or ""
            ).strip().casefold()
            if disease_filter:
                records = [
                    record
                    for record in records
                    if disease_filter in record.disease_name.casefold()
                    or disease_filter in str(record.disease_id or "").casefold()
                ]
            records = records[: request.material_limit]
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
