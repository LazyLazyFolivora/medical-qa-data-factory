"""Structured entity providers for genes and related biomedical facts."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any, Callable
from urllib.parse import quote, urlencode

from .evidence_provider import (
    EvidenceProviderCapability,
    EvidenceProviderRequest,
    EvidenceProviderResult,
    ProviderTerminalStatus,
    SideEffectClass,
)
from .objective_question_contracts import text_digest
from .objective_question_persistence import immutable_blob_ref


JsonTransport = Callable[[str, dict[str, str] | None], Any]


def _requests_json(
    url: str,
    headers: dict[str, str] | None = None,
) -> Any:
    import requests

    response = requests.get(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "medical-qa-data-factory/0.1",
            **(headers or {}),
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


class _StructuredProvider:
    provider_id = ""
    provider_version = "v1"
    material_type = "structured_record"
    snapshot_id = ""

    def __init__(self, transport: JsonTransport | None = None) -> None:
        self.transport = transport or _requests_json
        self.capability = EvidenceProviderCapability(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            side_effect_class=SideEffectClass.READ_ONLY,
            supports_idempotency=True,
            supports_status_lookup=False,
            safe_to_repeat=True,
        )

    def lookup_status(self, operation_key: str):
        return None

    def _result(
        self,
        request: EvidenceProviderRequest,
        payload: Any,
        *,
        source_record_id: str,
        fields: list[tuple[str, Any]],
        provider_calls: int,
    ) -> EvidenceProviderResult:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = text_digest(raw)
        safe_record_id = re.sub(
            r"[^A-Za-z0-9._-]",
            "_",
            source_record_id,
        )
        material_id = f"material.{self.provider_id}.{safe_record_id}"
        anchors = []
        for field_name, value in fields:
            if value is None or value == "" or value == []:
                continue
            fragment = (
                json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if not isinstance(value, str)
                else json.dumps(value, ensure_ascii=False)
            )
            start = raw.find(fragment)
            if start < 0:
                continue
            anchor_id = (
                f"anchor.{self.provider_id}."
                f"{safe_record_id}.{field_name}"
            )
            anchors.append(
                {
                    "anchor_id": anchor_id,
                    "material_id": material_id,
                    "document_id": f"{self.provider_id}:{source_record_id}",
                    "section_or_field": field_name,
                    "start_offset": start,
                    "end_offset": start + len(fragment),
                    "quoted_text": fragment,
                    "quote_hash": text_digest(fragment),
                }
            )
        material = {
            "material_id": material_id,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "source_record_id": source_record_id,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "content_hash": digest,
            "raw_content_ref": immutable_blob_ref(digest),
            "material_type": self.material_type,
            "retrieval_request_id": request.request_id,
            "terminal_status": "SUCCEEDED",
        }
        return EvidenceProviderResult(
            request=request,
            terminal_status=ProviderTerminalStatus.SUCCEEDED,
            material_ids=[material_id],
            anchor_ids=[anchor["anchor_id"] for anchor in anchors],
            materials=[material],
            anchors=anchors,
            raw_content_by_material_id={material_id: raw},
            snapshot_id=self.snapshot_id or f"{self.provider_id}:{date.today()}",
            usage={"provider_calls": provider_calls},
        )

    def _failure(
        self,
        request: EvidenceProviderRequest,
        *,
        category: str,
        message: str,
    ) -> EvidenceProviderResult:
        return EvidenceProviderResult(
            request=request,
            terminal_status=ProviderTerminalStatus.FAILED,
            material_ids=[],
            anchor_ids=[],
            error_category=category,
            usage={"error_message": message},
        )

    def _empty(
        self,
        request: EvidenceProviderRequest,
    ) -> EvidenceProviderResult:
        return EvidenceProviderResult(
            request=request,
            terminal_status=ProviderTerminalStatus.EMPTY,
            material_ids=[],
            anchor_ids=[],
        )

    @staticmethod
    def _query(payload: dict[str, Any], provider_id: str) -> dict[str, Any]:
        nested = payload.get(provider_id)
        return dict(nested) if isinstance(nested, dict) else dict(payload)


class HgncMaterialProvider(_StructuredProvider):
    provider_id = "hgnc"
    provider_version = "hgnc-rest-v1"
    material_type = "hgnc_gene_record"

    def retrieve(self, request: EvidenceProviderRequest) -> EvidenceProviderResult:
        query = self._query(request.query, self.provider_id)
        symbol = str(
            query.get("symbol")
            or query.get("mention")
            or query.get("entity_name")
            or ""
        ).strip()
        if not symbol:
            return self._failure(
                request,
                category="HGNC_SYMBOL_MISSING",
                message="HGNC retrieval requires a gene symbol",
            )
        try:
            payload = self.transport(
                f"https://rest.genenames.org/fetch/symbol/{quote(symbol)}",
                {"Accept": "application/json"},
            )
            docs = payload.get("response", {}).get("docs", [])
        except Exception as exc:
            return self._failure(
                request,
                category="HGNC_QUERY_ERROR",
                message=str(exc),
            )
        if not docs:
            return self._empty(request)
        record = docs[0]
        stable_payload = dict(payload)
        header = payload.get("responseHeader")
        if isinstance(header, dict):
            stable_payload["responseHeader"] = {
                key: value
                for key, value in header.items()
                if key != "QTime"
            }
        return self._result(
            request,
            stable_payload,
            source_record_id=str(record.get("hgnc_id") or symbol),
            fields=[
                ("symbol", record.get("symbol")),
                ("name", record.get("name")),
                ("alias_symbol", record.get("alias_symbol")),
                ("prev_symbol", record.get("prev_symbol")),
                ("hgnc_id", record.get("hgnc_id")),
                ("location", record.get("location")),
                ("locus_group", record.get("locus_group")),
                ("locus_type", record.get("locus_type")),
                ("entrez_id", record.get("entrez_id")),
                ("ensembl_gene_id", record.get("ensembl_gene_id")),
                ("status", record.get("status")),
            ],
            provider_calls=1,
        )


class NcbiGeneMaterialProvider(_StructuredProvider):
    provider_id = "ncbi_gene"
    provider_version = "ncbi-eutils-gene-v1"
    material_type = "ncbi_gene_record"

    def retrieve(self, request: EvidenceProviderRequest) -> EvidenceProviderResult:
        query = self._query(request.query, self.provider_id)
        symbol = str(
            query.get("symbol")
            or query.get("mention")
            or query.get("entity_name")
            or ""
        ).strip()
        if not symbol:
            return self._failure(
                request,
                category="NCBI_GENE_SYMBOL_MISSING",
                message="NCBI Gene retrieval requires a gene symbol",
            )
        search_url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
            + urlencode(
                {
                    "db": "gene",
                    "term": f"{symbol}[sym] AND human[orgn]",
                    "retmode": "json",
                    "retmax": "5",
                }
            )
        )
        try:
            search = self.transport(search_url, None)
            ids = search.get("esearchresult", {}).get("idlist", [])
            if not ids:
                return self._empty(request)
            gene_id = str(ids[0])
            summary = self.transport(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
                + urlencode(
                    {
                        "db": "gene",
                        "id": gene_id,
                        "retmode": "json",
                    }
                ),
                None,
            )
            record = summary.get("result", {}).get(gene_id, {})
        except Exception as exc:
            return self._failure(
                request,
                category="NCBI_GENE_QUERY_ERROR",
                message=str(exc),
            )
        if not record:
            return self._empty(request)
        return self._result(
            request,
            {"search": search, "summary": summary},
            source_record_id=f"gene:{gene_id}",
            fields=[
                ("name", record.get("name")),
                ("nomenclaturesymbol", record.get("nomenclaturesymbol")),
                ("nomenclaturename", record.get("nomenclaturename")),
                ("nomenclaturestatus", record.get("nomenclaturestatus")),
                ("otheraliases", record.get("otheraliases")),
                ("otherdesignations", record.get("otherdesignations")),
                ("organism", record.get("organism")),
                ("description", record.get("description")),
                ("chromosome", record.get("chromosome")),
                ("maplocation", record.get("maplocation")),
                ("summary", record.get("summary")),
                ("type", record.get("type")),
                ("mim", record.get("mim")),
                ("uid", record.get("uid")),
            ],
            provider_calls=2,
        )


class EnsemblMaterialProvider(_StructuredProvider):
    provider_id = "ensembl"
    provider_version = "ensembl-rest-v1"
    material_type = "ensembl_gene_record"

    def retrieve(self, request: EvidenceProviderRequest) -> EvidenceProviderResult:
        query = self._query(request.query, self.provider_id)
        symbol = str(
            query.get("symbol")
            or query.get("mention")
            or query.get("entity_name")
            or ""
        ).strip()
        if not symbol:
            return self._failure(
                request,
                category="ENSEMBL_SYMBOL_MISSING",
                message="Ensembl retrieval requires a gene symbol",
            )
        try:
            payload = self.transport(
                "https://rest.ensembl.org/lookup/symbol/homo_sapiens/"
                f"{quote(symbol)}?expand=0;content-type=application/json",
                {"Accept": "application/json"},
            )
        except Exception as exc:
            return self._failure(
                request,
                category="ENSEMBL_QUERY_ERROR",
                message=str(exc),
            )
        if not payload or not payload.get("id"):
            return self._empty(request)
        return self._result(
            request,
            payload,
            source_record_id=str(payload["id"]),
            fields=[
                ("display_name", payload.get("display_name")),
                ("seq_region_name", payload.get("seq_region_name")),
                ("start", payload.get("start")),
                ("end", payload.get("end")),
                ("strand", payload.get("strand")),
                ("assembly_name", payload.get("assembly_name")),
                ("biotype", payload.get("biotype")),
                ("description", payload.get("description")),
            ],
            provider_calls=1,
        )
