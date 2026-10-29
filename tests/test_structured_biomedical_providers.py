"""Structured biomedical sources for entity facts such as gene location."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from medical_qa_data_factory.generation.evidence_provider import (
    EvidenceProviderRequest,
    ProviderTerminalStatus,
)
from medical_qa_data_factory.generation.fact_source_policy import (
    objective_fact_source_route,
)
from medical_qa_data_factory.generation.structured_biomedical_providers import (
    EnsemblMaterialProvider,
    HgncMaterialProvider,
    NcbiGeneMaterialProvider,
)


NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def _request(provider_id: str) -> EvidenceProviderRequest:
    return EvidenceProviderRequest(
        request_id=f"request.{provider_id}",
        provider_id=provider_id,
        provider_version="v1",
        operation_key=f"run|{provider_id}|ZRSR2",
        query={"symbol": "ZRSR2", "native_id": "8233"},
        requested_at=NOW,
        material_limit=5,
    )


def test_gene_location_routes_to_hgnc_then_structured_verifiers() -> None:
    route = objective_fact_source_route("gene_location")

    assert route.primary_providers == ("hgnc",)
    assert route.verification_providers == ("ncbi_gene", "ensembl")
    assert "pubmed" in route.supporting_providers


def test_hgnc_provider_returns_location_classification_and_cross_ids() -> None:
    payload = {
        "responseHeader": {"status": 0},
        "response": {
            "numFound": 1,
            "docs": [
                {
                    "hgnc_id": "HGNC:23019",
                    "symbol": "ZRSR2",
                    "name": (
                        "zinc finger CCCH-type, RNA binding motif and "
                        "serine/arginine rich 2"
                    ),
                    "status": "Approved",
                    "location": "Xp22.2",
                    "locus_group": "protein-coding gene",
                    "locus_type": "gene with protein product",
                    "entrez_id": "8233",
                    "ensembl_gene_id": "ENSG00000169249",
                    "prev_symbol": ["U2AF1L2"],
                }
            ],
        },
    }
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str] | None = None) -> Any:
        calls.append(url)
        return payload

    result = HgncMaterialProvider(transport=transport).retrieve(_request("hgnc"))

    assert result.terminal_status is ProviderTerminalStatus.SUCCEEDED
    assert len(result.materials) == 1
    assert "Xp22.2" in result.raw_content_by_material_id[
        result.material_ids[0]
    ]
    assert any("Xp22.2" in item["quoted_text"] for item in result.anchors)
    assert calls[0].endswith("/fetch/symbol/ZRSR2")


def test_ncbi_gene_provider_searches_symbol_then_fetches_summary() -> None:
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str] | None = None) -> Any:
        calls.append(url)
        if "esearch.fcgi" in url:
            return {"esearchresult": {"count": "1", "idlist": ["8233"]}}
        return {
            "result": {
                "uids": ["8233"],
                "8233": {
                    "uid": "8233",
                    "name": "ZRSR2",
                    "description": "zinc finger CCCH-type containing 22",
                    "chromosome": "X",
                    "maplocation": "Xp22.2",
                    "summary": "This gene encodes a spliceosome component.",
                },
            }
        }

    result = NcbiGeneMaterialProvider(transport=transport).retrieve(
        _request("ncbi_gene")
    )

    assert result.terminal_status is ProviderTerminalStatus.SUCCEEDED
    assert len(calls) == 2
    assert any("Xp22.2" in item["quoted_text"] for item in result.anchors)
    assert result.materials[0]["source_record_id"] == "gene:8233"


def test_ensembl_provider_returns_assembly_location_and_biotype() -> None:
    payload = {
        "id": "ENSG00000169249",
        "display_name": "ZRSR2",
        "description": "zinc finger CCCH-type, RNA binding motif 2",
        "seq_region_name": "X",
        "start": 15789557,
        "end": 15816234,
        "strand": 1,
        "assembly_name": "GRCh38",
        "biotype": "protein_coding",
    }
    result = EnsemblMaterialProvider(
        transport=lambda url, headers=None: payload
    ).retrieve(_request("ensembl"))

    assert result.terminal_status is ProviderTerminalStatus.SUCCEEDED
    assert result.materials[0]["source_record_id"] == "ENSG00000169249"
    assert any("GRCh38" in item["quoted_text"] for item in result.anchors)
    assert any("protein_coding" in item["quoted_text"] for item in result.anchors)
