"""Structured entity providers for genes and related biomedical facts."""
from __future__ import annotations

import json
import re
import time
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

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = requests.get(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "medical-qa-data-factory/0.1",
                    **(headers or {}),
                },
                timeout=30,
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.HTTPError(
                    f"temporary provider response: HTTP {response.status_code}",
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
        # A source record can return different bytes on later reads. Bind offsets
        # to the captured version while keeping document_id stable across reads.
        snapshot_key = f"{self.provider_id}.{safe_record_id}.{digest[:20]}"
        material_id = f"material.{snapshot_key}"
        anchors = [
            {
                "anchor_id": f"anchor.{snapshot_key}.record",
                "material_id": material_id,
                "document_id": f"{self.provider_id}:{source_record_id}",
                "section_or_field": "record",
                "start_offset": 0,
                "end_offset": len(raw),
                "quoted_text": raw,
                "quote_hash": text_digest(raw),
            }
        ]
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
            anchor_id = f"anchor.{snapshot_key}.{field_name}"
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
        calls = 0
        try:
            calls += 1
            payload = self.transport(
                "https://rest.genenames.org/fetch/symbol/"
                f"{quote(symbol)}",
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
            try:
                calls += 1
                search_payload = self.transport(
                    f"https://rest.genenames.org/search/{quote(symbol)}",
                    {"Accept": "application/json"},
                )
                candidates = search_payload.get("response", {}).get(
                    "docs",
                    [],
                )
            except Exception as exc:
                return self._failure(
                    request,
                    category="HGNC_SEARCH_ERROR",
                    message=str(exc),
                )
            docs = []
            for candidate in candidates[: request.material_limit]:
                hgnc_id = str(
                    candidate.get("hgnc_id", "")
                    if isinstance(candidate, dict)
                    else ""
                ).strip()
                if not hgnc_id:
                    continue
                try:
                    calls += 1
                    detail_payload = self.transport(
                        "https://rest.genenames.org/fetch/id/"
                        f"{quote(hgnc_id)}",
                        {"Accept": "application/json"},
                    )
                except Exception:
                    continue
                docs.extend(
                    record
                    for record in detail_payload.get("response", {}).get(
                        "docs",
                        [],
                    )
                    if isinstance(record, dict)
                )
            if not docs:
                return self._failure(
                    request,
                    category="HGNC_DETAIL_UNAVAILABLE",
                    message=(
                        "HGNC returned search candidates but no complete "
                        "gene record"
                    ),
                )
        results = []
        for record in docs[: request.material_limit]:
            if not isinstance(record, dict):
                continue
            results.append(
                self._result(
                    request,
                    {
                        "response": {
                            "docs": [record],
                        }
                    },
                    source_record_id=str(
                        record.get("hgnc_id") or symbol
                    ),
                    fields=[
                        ("symbol", record.get("symbol")),
                        ("name", record.get("name")),
                        ("alias_name", record.get("alias_name")),
                        ("alias_symbol", record.get("alias_symbol")),
                        ("prev_name", record.get("prev_name")),
                        ("prev_symbol", record.get("prev_symbol")),
                        ("hgnc_id", record.get("hgnc_id")),
                        ("location", record.get("location")),
                        ("locus_group", record.get("locus_group")),
                        ("locus_type", record.get("locus_type")),
                        ("gene_group", record.get("gene_group")),
                        ("entrez_id", record.get("entrez_id")),
                        (
                            "ensembl_gene_id",
                            record.get("ensembl_gene_id"),
                        ),
                        ("uniprot_ids", record.get("uniprot_ids")),
                        ("omim_id", record.get("omim_id")),
                        ("refseq_accession", record.get("refseq_accession")),
                        ("ccds_id", record.get("ccds_id")),
                        ("ucsc_id", record.get("ucsc_id")),
                        ("mane_select", record.get("mane_select")),
                        ("pubmed_id", record.get("pubmed_id")),
                        ("orphanet", record.get("orphanet")),
                        ("mgd_id", record.get("mgd_id")),
                        ("rgd_id", record.get("rgd_id")),
                        ("status", record.get("status")),
                        ("date_modified", record.get("date_modified")),
                    ],
                    provider_calls=calls,
                )
            )
        return _combine_results(
            request,
            results,
            provider_id=self.provider_id,
            snapshot_id=self.snapshot_id
            or f"{self.provider_id}:{date.today()}",
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
        native_id = str(query.get("native_id") or "").strip()
        # Identity lookup must not return genes that merely mention this gene.
        # NCBI's Gene Name field includes preferred symbols and aliases.
        name_field = "Gene Name" if query.get("symbol") else "Gene/Protein Name"
        if not symbol and not native_id:
            return self._failure(
                request,
                category="NCBI_GENE_SYMBOL_MISSING",
                message="NCBI Gene retrieval requires a gene symbol",
            )
        try:
            search_url = (
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
                + urlencode(
                    {
                        "db": "gene",
                        "term": (
                            f"{native_id}[uid]"
                            if native_id.isdigit()
                            else f"{symbol}[{name_field}] AND human[orgn]"
                        ),
                        "retmode": "json",
                        "retmax": "5",
                    }
                )
            )
            search = self.transport(search_url, None)
            ids = search.get("esearchresult", {}).get("idlist", [])
            if not ids:
                return self._empty(request)
            summary = self.transport(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
                + urlencode(
                    {
                        "db": "gene",
                        "id": ",".join(str(item) for item in ids),
                        "retmode": "json",
                    }
                ),
                None,
            )
        except Exception as exc:
            return self._failure(
                request,
                category="NCBI_GENE_QUERY_ERROR",
                message=str(exc),
            )
        records = summary.get("result", {})
        if not isinstance(records, dict):
            return self._empty(request)
        results = []
        for candidate_id in ids:
            record = records.get(str(candidate_id), {})
            if not isinstance(record, dict) or not record:
                continue
            results.append(
                self._result(
                    request,
                    {
                        "search": search,
                        "summary": {
                            "uid": record.get("uid") or candidate_id,
                            "record": record,
                        },
                    },
                    source_record_id=f"gene:{candidate_id}",
                    fields=[
                        ("name", record.get("name")),
                        (
                            "nomenclaturesymbol",
                            record.get("nomenclaturesymbol"),
                        ),
                        (
                            "nomenclaturename",
                            record.get("nomenclaturename"),
                        ),
                        (
                            "nomenclaturestatus",
                            record.get("nomenclaturestatus"),
                        ),
                        ("otheraliases", record.get("otheraliases")),
                        (
                            "otherdesignations",
                            record.get("otherdesignations"),
                        ),
                        ("organism", record.get("organism")),
                        ("description", record.get("description")),
                        ("chromosome", record.get("chromosome")),
                        ("maplocation", record.get("maplocation")),
                        ("summary", record.get("summary")),
                        ("genomicinfo", record.get("genomicinfo")),
                        ("locationhist", record.get("locationhist")),
                        ("chrsort", record.get("chrsort")),
                        ("chrstart", record.get("chrstart")),
                        ("geneweight", record.get("geneweight")),
                        ("currentid", record.get("currentid")),
                        ("type", record.get("type")),
                        ("mim", record.get("mim")),
                        ("uid", record.get("uid")),
                    ],
                    provider_calls=2,
                )
            )
        if not results:
            return self._empty(request)
        return EvidenceProviderResult(
            request=request,
            terminal_status=ProviderTerminalStatus.SUCCEEDED,
            material_ids=[
                material_id
                for result in results
                for material_id in result.material_ids
            ],
            anchor_ids=[
                anchor_id
                for result in results
                for anchor_id in result.anchor_ids
            ],
            materials=[
                material
                for result in results
                for material in result.materials
            ],
            anchors=[
                anchor for result in results for anchor in result.anchors
            ],
            raw_content_by_material_id={
                material_id: content
                for result in results
                for material_id, content in (
                    result.raw_content_by_material_id.items()
                )
            },
            snapshot_id=self.snapshot_id or f"{self.provider_id}:{date.today()}",
            usage={
                "provider_calls": sum(
                    int(result.usage.get("provider_calls", 0))
                    for result in results
                ),
                "candidate_count": len(results),
            },
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
        native_id = str(query.get("native_id") or "").strip()
        if not symbol and not native_id:
            return self._failure(
                request,
                category="ENSEMBL_SYMBOL_MISSING",
                message="Ensembl retrieval requires a gene symbol",
            )
        try:
            if native_id.upper().startswith("ENSG"):
                payload = self.transport(
                    "https://rest.ensembl.org/lookup/id/"
                    f"{quote(native_id)}?expand=1;content-type=application/json",
                    {"Accept": "application/json"},
                )
            else:
                payload = self.transport(
                    "https://rest.ensembl.org/lookup/symbol/homo_sapiens/"
                    f"{quote(symbol)}?expand=1;content-type=application/json",
                    {"Accept": "application/json"},
                )
        except Exception as exc:
            try:
                xrefs = self.transport(
                    "https://rest.ensembl.org/xrefs/symbol/homo_sapiens/"
                    f"{quote(symbol)}?content-type=application/json",
                    {"Accept": "application/json"},
                )
            except Exception as xref_exc:
                return self._failure(
                    request,
                    category="ENSEMBL_QUERY_ERROR",
                    message=f"{exc}; xref fallback: {xref_exc}",
                )
            records = []
            for item in (
                xrefs[: request.material_limit]
                if isinstance(xrefs, list)
                else []
            ):
                record_id = (
                    str(item.get("id", ""))
                    if isinstance(item, dict)
                    else ""
                )
                if not record_id.upper().startswith("ENSG"):
                    continue
                try:
                    detail = self.transport(
                        "https://rest.ensembl.org/lookup/id/"
                        f"{quote(record_id)}"
                        "?expand=1;content-type=application/json",
                        {"Accept": "application/json"},
                    )
                except Exception:
                    continue
                if isinstance(detail, dict) and detail.get("id"):
                    records.append(detail)
            return _combine_results(
                request,
                [
                    self._result(
                        request,
                        record,
                        source_record_id=str(record["id"]),
                        fields=[
                            ("display_name", record.get("display_name")),
                            (
                                "seq_region_name",
                                record.get("seq_region_name"),
                            ),
                            ("start", record.get("start")),
                            ("end", record.get("end")),
                            ("strand", record.get("strand")),
                            ("assembly_name", record.get("assembly_name")),
                            ("biotype", record.get("biotype")),
                            ("description", record.get("description")),
                            (
                                "canonical_transcript",
                                record.get("canonical_transcript"),
                            ),
                            ("transcripts", record.get("transcripts")),
                            ("xrefs", record.get("xrefs")),
                        ],
                        provider_calls=2,
                    )
                    for record in records
                ],
                provider_id=self.provider_id,
                snapshot_id=self.snapshot_id
                or f"{self.provider_id}:{date.today()}",
            )
        if not payload or not payload.get("id"):
            return self._empty(request)
        payload = dict(payload)
        provider_calls = 1
        try:
            provider_calls += 1
            payload["xrefs"] = self.transport(
                "https://rest.ensembl.org/xrefs/id/"
                f"{quote(str(payload['id']))}?content-type=application/json",
                {"Accept": "application/json"},
            )
        except Exception:
            payload["xrefs"] = []
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
                (
                    "canonical_transcript",
                    payload.get("canonical_transcript"),
                ),
                ("transcripts", payload.get("transcripts")),
                ("xrefs", payload.get("xrefs")),
            ],
            provider_calls=provider_calls,
        )


def _combine_results(
    request: EvidenceProviderRequest,
    results: list[EvidenceProviderResult],
    *,
    provider_id: str,
    snapshot_id: str,
) -> EvidenceProviderResult:
    if not results:
        return EvidenceProviderResult(
            request=request,
            terminal_status=ProviderTerminalStatus.EMPTY,
        )
    return EvidenceProviderResult(
        request=request,
        terminal_status=ProviderTerminalStatus.SUCCEEDED,
        material_ids=[
            material_id
            for result in results
            for material_id in result.material_ids
        ],
        anchor_ids=[
            anchor_id
            for result in results
            for anchor_id in result.anchor_ids
        ],
        materials=[
            material for result in results for material in result.materials
        ],
        anchors=[
            anchor for result in results for anchor in result.anchors
        ],
        raw_content_by_material_id={
            material_id: content
            for result in results
            for material_id, content in (
                result.raw_content_by_material_id.items()
            )
        },
        snapshot_id=snapshot_id,
        usage={
            "provider_calls": sum(
                int(result.usage.get("provider_calls", 0))
                for result in results
            ),
            "candidate_count": len(results),
            "provider_id": provider_id,
        },
    )
