"""Structured biomedical sources for entity facts such as gene location."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from medical_qa_data_factory.generation.evidence_provider import (
    EvidenceProviderRequest,
    ProviderTerminalStatus,
)
from medical_qa_data_factory.generation.fact_source_policy import (
    default_objective_fact_types,
    objective_fact_source_route,
)
from medical_qa_data_factory.generation.structured_biomedical_providers import (
    EnsemblMaterialProvider,
    HgncMaterialProvider,
    NcbiGeneMaterialProvider,
)
from medical_qa_data_factory.generation.objective_question_contracts import EvidenceAnchor, EvidenceMaterial
from medical_qa_data_factory.generation.objective_question_persistence import ObjectiveQuestionPersistence
from medical_qa_data_factory.generation.subagent_contracts import (
    SubagentRole,
    default_objective_role_registry,
)


NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def test_ensembl_changed_xrefs_preserve_both_versions_and_replay(tmp_path):
    store = ObjectiveQuestionPersistence(tmp_path)
    xrefs = []

    def transport(url, headers):
        if "/xrefs/" in url:
            return list(xrefs)
        return {"id": "ENSG00000204438", "display_name": "GPANK1"}

    provider = EnsemblMaterialProvider(transport=transport)

    def retrieve_and_save():
        result = provider.retrieve(_request("ensembl"))
        material = EvidenceMaterial.model_validate(result.materials[0])
        raw = result.raw_content_by_material_id[material.material_id].encode()
        status = store.save_material(material, raw)
        for item in result.anchors:
            store.save_anchor(EvidenceAnchor.model_validate(item))
        return result, status

    first, status = retrieve_and_save()
    assert status == "written"
    xrefs.append({"dbname": "HGNC", "primary_id": "example"})
    second, status = retrieve_and_save()
    assert status == "written"
    replay, status = retrieve_and_save()
    assert status == "existing"
    assert replay.material_ids == second.material_ids
    assert replay.anchor_ids == second.anchor_ids
    assert first.material_ids != second.material_ids
    assert set(first.anchor_ids).isdisjoint(second.anchor_ids)
    for snapshot in (first, second):
        for anchor_id in snapshot.anchor_ids:
            anchor = store.load_anchor(anchor_id)
            assert anchor.document_id == "ensembl:ENSG00000204438"
            material = store.load_material(anchor.material_id)
            assert material.material_id == snapshot.material_ids[0]
            raw = store.load_raw_content(material).decode()
            assert raw[anchor.start_offset:anchor.end_offset] == anchor.quoted_text


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


def test_material_retrieval_role_allows_structured_gene_providers() -> None:
    capability = default_objective_role_registry()[
        SubagentRole.MATERIAL_RETRIEVAL
    ]

    assert {"hgnc", "ncbi_gene", "ensembl"} <= capability.allowed_tools


def test_default_fact_types_include_literature_not_only_gene_metadata() -> None:
    gene_types = default_objective_fact_types(
        "gene",
        available_providers=["hgnc", "ncbi_gene", "ensembl", "pubmed"],
    )
    disease_types = default_objective_fact_types(
        "disease",
        available_providers=["primekg", "pubmed"],
    )

    assert "observation_association" in gene_types
    assert "observation_association" in disease_types
    assert "gene_location" not in disease_types


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
    assert any(
        item["section_or_field"] == "record"
        and "Xp22.2" in item["quoted_text"]
        for item in result.anchors
    )
    assert calls[0].endswith("/fetch/symbol/ZRSR2")


def test_hgnc_query_timing_does_not_change_material_identity() -> None:
    docs = [
        {
            "hgnc_id": "HGNC:17273",
            "symbol": "KIF15",
            "name": "kinesin family member 15",
            "status": "Approved",
            "location": "3p21.31",
            "locus_group": "protein-coding gene",
            "locus_type": "gene with protein product",
            "entrez_id": "56992",
            "ensembl_gene_id": "ENSG00000163808",
        }
    ]

    def retrieve(qtime: int):
        payload = {
            "responseHeader": {"QTime": qtime, "status": 0},
            "response": {
                "numFound": 1,
                "numFoundExact": True,
                "start": 0,
                "docs": docs,
            },
        }
        return HgncMaterialProvider(
            transport=lambda url, headers=None: payload
        ).retrieve(_request("hgnc"))

    first = retrieve(1)
    second = retrieve(0)

    assert first.materials[0]["content_hash"] == second.materials[0]["content_hash"]
    assert (
        first.raw_content_by_material_id[first.material_ids[0]]
        == second.raw_content_by_material_id[second.material_ids[0]]
    )


def test_hgnc_falls_back_to_search_then_fetches_full_record() -> None:
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str] | None = None) -> Any:
        calls.append(url)
        if "/fetch/symbol/" in url:
            return {"response": {"docs": []}}
        if "/search/" in url:
            return {
                "response": {
                    "docs": [
                        {
                            "hgnc_id": "HGNC:8983",
                            "symbol": "PI4KA",
                        }
                    ]
                }
            }
        return {
            "response": {
                "docs": [
                    {
                        "hgnc_id": "HGNC:8983",
                        "symbol": "PI4KA",
                        "name": "phosphatidylinositol 4-kinase alpha",
                        "status": "Approved",
                        "location": "22q11.21",
                        "locus_group": "protein-coding gene",
                        "locus_type": "gene with protein product",
                        "uniprot_ids": ["P42356"],
                    }
                ]
            }
        }

    result = HgncMaterialProvider(transport=transport).retrieve(
        _request("hgnc")
    )

    assert result.terminal_status is ProviderTerminalStatus.SUCCEEDED
    assert calls == [
        "https://rest.genenames.org/fetch/symbol/ZRSR2",
        "https://rest.genenames.org/search/ZRSR2",
        "https://rest.genenames.org/fetch/id/HGNC%3A8983",
    ]
    assert "22q11.21" in result.raw_content_by_material_id[
        result.material_ids[0]
    ]


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
                    "nomenclaturesymbol": "ZRSR2",
                    "nomenclaturename": "zinc finger CCCH-type, RNA binding motif 2",
                    "nomenclaturestatus": "Official",
                    "organism": {
                        "commonname": "human",
                        "scientificname": "Homo sapiens",
                        "taxid": 9606,
                    },
                    "description": "zinc finger CCCH-type containing 22",
                    "chromosome": "X",
                    "maplocation": "Xp22.2",
                    "summary": "This gene encodes a spliceosome component.",
                    "genomicinfo": [
                        {
                            "chraccver": "NC_000023.11",
                            "chrstart": 15800000,
                            "chrstop": 15700000,
                        }
                    ],
                },
            }
        }

    result = NcbiGeneMaterialProvider(transport=transport).retrieve(
        _request("ncbi_gene")
    )

    assert result.terminal_status is ProviderTerminalStatus.SUCCEEDED
    assert len(calls) == 2
    assert any("Xp22.2" in item["quoted_text"] for item in result.anchors)
    assert any(
        item["anchor_id"].endswith(".nomenclaturestatus")
        and "Official" in item["quoted_text"]
        for item in result.anchors
    )
    assert any(
        item["anchor_id"].endswith(".genomicinfo")
        for item in result.anchors
    )
    assert result.materials[0]["source_record_id"] == "gene:8233"


def test_ncbi_gene_provider_returns_multiple_candidates_for_screening() -> None:
    def transport(url: str, headers: dict[str, str] | None = None) -> Any:
        if "esearch.fcgi" in url:
            return {"esearchresult": {"count": "2", "idlist": ["1", "2"]}}
        return {
            "result": {
                "uids": ["1", "2"],
                "1": {
                    "uid": "1",
                    "name": "GENE1",
                    "nomenclaturesymbol": "GENE1",
                    "description": "first candidate",
                    "organism": {"scientificname": "Homo sapiens"},
                },
                "2": {
                    "uid": "2",
                    "name": "GENE2",
                    "nomenclaturesymbol": "GENE2",
                    "description": "second candidate",
                    "organism": {"scientificname": "Homo sapiens"},
                },
            }
        }

    result = NcbiGeneMaterialProvider(transport=transport).retrieve(
        _request("ncbi_gene")
    )

    assert result.terminal_status is ProviderTerminalStatus.SUCCEEDED
    assert len(result.materials) == 2
    assert {
        material["source_record_id"] for material in result.materials
    } == {"gene:1", "gene:2"}


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
        "canonical_transcript": "ENST00000255882",
        "transcripts": [{"id": "ENST00000255882", "is_canonical": 1}],
    }
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str] | None = None) -> Any:
        calls.append(url)
        return payload

    result = EnsemblMaterialProvider(transport=transport).retrieve(
        _request("ensembl")
    )

    assert result.terminal_status is ProviderTerminalStatus.SUCCEEDED
    assert result.materials[0]["source_record_id"] == "ENSG00000169249"
    assert any("GRCh38" in item["quoted_text"] for item in result.anchors)
    assert any("protein_coding" in item["quoted_text"] for item in result.anchors)
    assert any("ENST00000255882" in item["quoted_text"] for item in result.anchors)
    assert calls[0].endswith(
        "lookup/symbol/homo_sapiens/ZRSR2?expand=1;content-type=application/json"
    )
    assert any("/xrefs/id/ENSG00000169249" in call for call in calls)
