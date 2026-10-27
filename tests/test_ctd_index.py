from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from medical_qa_data_factory.cli import main
from medical_qa_data_factory.evidence_enrichment.ctd import (
    CtdDiseaseCrosswalk,
    CtdDiseaseCrosswalkEntry,
    CtdEvidenceIndex,
    CtdMappingStatus,
    audit_primekg_ctd_edge,
    build_ctd_index,
)
from medical_qa_data_factory.evidence_enrichment.primekg_ctd_identity import (
    build_primekg_ctd_identity_snapshot,
)
from medical_qa_data_factory.knowledge_graph.contracts import (
    GraphEdge,
    GraphEntity,
)
from medical_qa_data_factory.knowledge_graph.providers.primekg import (
    PrimeKGSQLiteProvider,
)


def _write_rows(path: Path, rows: list[tuple[str, ...]]) -> Path:
    source = path / "ctd.csv.gz"
    with gzip.open(source, "wt", encoding="utf-8", newline="") as handle:
        handle.write("# CTD test fixture\n")
        for row in rows:
            handle.write("\t".join(row) + "\n")
    return source


def _row(
    disease_name: str,
    disease_id: str,
    *,
    chemical_id: str = "D000001",
    direct_evidence: str = "marker/mechanism",
    pubmed_ids: str = "100",
) -> tuple[str, ...]:
    return (
        "Chemical One",
        chemical_id,
        "",
        disease_name,
        disease_id,
        direct_evidence,
        "GENE1",
        "1.0",
        "",
        pubmed_ids,
    )


def _edge(disease_name: str, disease_id: str) -> GraphEdge:
    return GraphEdge(
        provider_id="primekg",
        edge_id=f"edge-{disease_id}",
        head=GraphEntity(
            provider_id="primekg",
            native_id="D000001",
            canonical_id="primekg:exposure:D000001",
            name="Chemical One",
            entity_type="exposure",
        ),
        relation="exposure_disease",
        tail=GraphEntity(
            provider_id="primekg",
            native_id=disease_id,
            canonical_id=f"primekg:disease:{disease_id}",
            name=disease_name,
            entity_type="disease",
        ),
        source="CTD",
    )


def test_enumeration_is_complete_deterministic_and_canonicalized(
    tmp_path: Path,
) -> None:
    rows = [
        _row("Alpha syndrome", "MESH:D000010", pubmed_ids="100"),
        _row("Alpha disease", "MESH:D000010", pubmed_ids="101"),
        _row("Shared name", "MESH:D000020"),
        _row("Shared name", "MESH:D000021"),
    ]
    rows.extend(
        _row(f"Disease {index:04d}", f"MESH:C{index:06d}")
        for index in range(1_005)
    )
    index_path = tmp_path / "ctd.sqlite"
    metadata = build_ctd_index(_write_rows(tmp_path, rows), index_path)
    index = CtdEvidenceIndex(index_path)

    first = index.enumerate_diseases(chemical_id="D000001")
    second = index.enumerate_diseases(chemical_id="D000001")

    assert len(first) == 1_008
    assert first == second
    assert metadata["rows_processed"] == 1_009
    assert metadata["row_count"] == 1_009
    assert metadata["source_release"] == "ctd.csv.gz"
    alpha = next(item for item in first if item.disease_id == "MESH:D000010")
    assert alpha.disease_aliases == ["Alpha disease", "Alpha syndrome"]
    assert alpha.pubmed_ids == ["100", "101"]
    assert len([item for item in first if item.disease_name == "Shared name"]) == 2


def test_record_can_be_read_back_by_snapshot_locator_and_hash(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "ctd.sqlite"
    build_ctd_index(
        _write_rows(tmp_path, [_row("Alpha disease", "MESH:D000010")]),
        index_path,
    )
    index = CtdEvidenceIndex(index_path)
    record = index.enumerate_diseases(chemical_id="D000001")[0]

    rebuilt = index.read_record(
        source_snapshot_id=record.source_snapshot_id,
        source_record_locator=record.source_record_locator,
        content_hash=record.content_hash,
    )

    assert rebuilt == record
    with pytest.raises(ValueError, match="content hash"):
        index.read_record(
            source_snapshot_id=record.source_snapshot_id,
            source_record_locator=record.source_record_locator,
            content_hash="wrong",
        )


def test_index_fails_closed_when_manifest_does_not_match_database(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "ctd.sqlite"
    build_ctd_index(
        _write_rows(tmp_path, [_row("Alpha disease", "MESH:D000010")]),
        index_path,
    )
    manifest_path = index_path.parent / f"{index_path.name}.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest"):
        CtdEvidenceIndex(index_path)


def test_mapping_audit_distinguishes_exact_crosswalk_ambiguous_and_unmapped(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "ctd.sqlite"
    build_ctd_index(
        _write_rows(
            tmp_path,
            [
                _row("Exact disease", "MESH:D000010"),
                _row("Shared disease", "MESH:D000020"),
                _row("Shared disease", "MESH:D000021"),
                _row("Crosswalk disease", "MESH:D000030"),
            ],
        ),
        index_path,
    )
    index = CtdEvidenceIndex(index_path)
    crosswalk = CtdDiseaseCrosswalk(
        entries=[
            CtdDiseaseCrosswalkEntry(
                primekg_disease_id="PKG-30",
                ctd_disease_ids=["MESH:D000030"],
            )
        ]
    )

    exact = audit_primekg_ctd_edge(
        _edge("Exact disease", "PKG-10"), index=index, crosswalk=crosswalk
    )
    ambiguous = audit_primekg_ctd_edge(
        _edge("Shared disease", "PKG-20"), index=index, crosswalk=crosswalk
    )
    mapped = audit_primekg_ctd_edge(
        _edge("Different public name", "PKG-30"),
        index=index,
        crosswalk=crosswalk,
    )
    unmapped = audit_primekg_ctd_edge(
        _edge("Missing disease", "PKG-40"), index=index, crosswalk=crosswalk
    )

    assert exact.status is CtdMappingStatus.MAPPED_EXACT
    assert len(exact.ctd_record_locators) == 1
    assert ambiguous.status is CtdMappingStatus.AMBIGUOUS
    assert "ctd_mapping_ambiguous" in ambiguous.reason_codes
    assert mapped.status is CtdMappingStatus.MAPPED_CROSSWALK
    assert unmapped.status is CtdMappingStatus.UNMAPPED
    assert "ctd_mapping_unmapped" in unmapped.reason_codes

    conflicting_crosswalk = CtdDiseaseCrosswalk(
        entries=[
            CtdDiseaseCrosswalkEntry(
                primekg_disease_id="PKG-10",
                ctd_disease_ids=["MESH:D000030"],
            )
        ]
    )
    conflict = audit_primekg_ctd_edge(
        _edge("Exact disease", "PKG-10"),
        index=index,
        crosswalk=conflicting_crosswalk,
    )
    assert conflict.status is CtdMappingStatus.AMBIGUOUS
    assert conflict.reason_codes == ["ctd_crosswalk_name_conflict"]


def test_identity_snapshot_maps_only_unambiguous_source_records(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "ctd.sqlite"
    build_ctd_index(
        _write_rows(
            tmp_path,
            [
                _row("Exact disease", "MESH:D000010"),
                _row("Shared disease", "MESH:D000020"),
                _row("Shared disease", "MESH:D000021"),
            ],
        ),
        index_path,
    )
    index = CtdEvidenceIndex(index_path)
    snapshot, audit = build_primekg_ctd_identity_snapshot(
        [
            _edge("Exact disease", "PKG-10"),
            _edge("Shared disease", "PKG-20"),
            _edge("Missing disease", "PKG-40"),
        ],
        index=index,
        primekg_snapshot_id="primekg:test",
    )

    assert snapshot.method == "primekg_ctd_source_exact_alias_v1"
    assert snapshot.primekg_snapshot_id == "primekg:test"
    assert snapshot.ctd_snapshot_id == index.metadata()["snapshot_id"]
    assert snapshot.chemical_crosswalk.ctd_ids_for(
        "primekg:exposure:D000001"
    ) == {"D000001"}
    assert snapshot.disease_crosswalk.ctd_ids_for(
        "primekg:disease:PKG-10"
    ) == {"MESH:D000010"}
    assert snapshot.disease_crosswalk.ctd_ids_for(
        "primekg:disease:PKG-20"
    ) == set()
    assert snapshot.disease_crosswalk.ctd_ids_for(
        "primekg:disease:PKG-40"
    ) == set()
    assert snapshot.audit_counts["mapped_exact"] == 1
    assert snapshot.audit_counts["ambiguous"] == 1
    assert snapshot.audit_counts["unmapped"] == 1
    assert snapshot.fully_mapped_chemical_ids == []
    assert len(audit) == 3


def test_primekg_source_edge_enumeration_uses_complete_bounded_pages(
    tmp_path: Path,
) -> None:
    database = tmp_path / "primekg.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE edges (
                head TEXT, relation TEXT, tail TEXT,
                head_id TEXT, tail_id TEXT,
                head_type TEXT, tail_type TEXT, source TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "Chemical One",
                    "exposure_disease",
                    f"Disease {index}",
                    "D000001",
                    f"PKG-{index}",
                    "exposure",
                    "disease",
                    "CTD",
                )
                for index in range(1_005)
            ]
            + [
                (
                    "Chemical One",
                    "exposure_protein",
                    "GENE1",
                    "D000001",
                    "1",
                    "exposure",
                    "gene/protein",
                    "CTD",
                ),
                (
                    "Chemical Two",
                    "exposure_disease",
                    "Other disease",
                    "D000002",
                    "PKG-X",
                    "exposure",
                    "disease",
                    "OTHER",
                ),
            ],
        )

    with PrimeKGSQLiteProvider(database) as provider:
        edges = list(
            provider.iter_edges_by_source(
                source="CTD", relation="exposure_disease", page_size=97
            )
        )

    assert len(edges) == 1_005
    assert [edge.tail.native_id for edge in edges[:3]] == [
        "PKG-0",
        "PKG-1",
        "PKG-2",
    ]


def test_ctd_audit_cli_reports_every_mapping_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    index_path = tmp_path / "ctd.sqlite"
    build_ctd_index(
        _write_rows(
            tmp_path,
            [
                _row("Exact disease", "MESH:D000010"),
                _row("Shared disease", "MESH:D000020"),
                _row("Shared disease", "MESH:D000021"),
            ],
        ),
        index_path,
    )
    database = tmp_path / "primekg.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE edges (
                head TEXT, relation TEXT, tail TEXT,
                head_id TEXT, tail_id TEXT,
                head_type TEXT, tail_type TEXT, source TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "Chemical One",
                    "exposure_disease",
                    "Exact disease",
                    "D000001",
                    "PKG-10",
                    "exposure",
                    "disease",
                    "CTD",
                ),
                (
                    "Chemical One",
                    "exposure_disease",
                    "Shared disease",
                    "D000001",
                    "PKG-20",
                    "exposure",
                    "disease",
                    "CTD",
                ),
                (
                    "Chemical One",
                    "exposure_disease",
                    "Missing disease",
                    "D000001",
                    "PKG-30",
                    "exposure",
                    "disease",
                    "CTD",
                ),
            ],
        )

    exit_code = main(
        [
            "evidence",
            "ctd",
            "audit",
            "--index",
            str(index_path),
            "--primekg-database",
            str(database),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["total_edges"] == 3
    assert payload["status_counts"] == {
        "ambiguous": 1,
        "mapped_exact": 1,
        "unmapped": 1,
    }
    assert len(payload["records"]) == 3

