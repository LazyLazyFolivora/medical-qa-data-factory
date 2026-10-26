"""Regression tests for the known PrimeKG identity-isolation defects.

The production snapshot contains the same native id in several entity types
(``disease:8315`` is prostate cancer while ``gene/protein:8315`` is BRAP), so
an id-only neighbour query silently returns the wrong rows.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from medical_qa_data_factory.knowledge_graph import EntityLookup, NeighborQuery
from medical_qa_data_factory.knowledge_graph.providers.primekg import (
    PrimeKGSQLiteProvider,
    init_manifest,
    manifest_path_for,
    read_manifest,
    refresh_manifest,
)

COLLIDING_ROWS = [
    (
        "prostate cancer",
        "contraindication",
        "Estradiol",
        "8315",
        "D001",
        "disease",
        "drug",
        "DrugBank",
    ),
    ("BRAP", "interacts_with", "aspirin", "8315", "D002", "gene", "drug", "DrugBank"),
    ("aspirin", "treats", "headache", "D003", "B156", "drug", "disease", "DrugBank"),
    ("PTGS1", "interacts_with", "aspirin", "5742", "D003", "gene", "drug", "DrugBank"),
]


@pytest.fixture
def colliding_db(tmp_path: Path) -> Path:
    path = tmp_path / "collide.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE edges (head TEXT, relation TEXT, tail TEXT, head_id TEXT,"
        " tail_id TEXT, head_type TEXT, tail_type TEXT, source TEXT)"
    )
    connection.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?)", COLLIDING_ROWS)
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def colliding(colliding_db: Path):
    provider = PrimeKGSQLiteProvider(colliding_db)
    yield provider
    provider.close()


def _resolve(provider, native_id: str, entity_type: str):
    return provider.resolve_entity(
        EntityLookup(mention=native_id, native_id=native_id, entity_types=[entity_type])
    )


def test_disease_8315_does_not_resolve_to_brap(colliding) -> None:
    resolution = _resolve(colliding, "8315", "disease")
    assert {item.entity.canonical_id for item in resolution.candidates} == {
        "primekg:disease:8315"
    }
    assert resolution.selected is not None
    assert resolution.selected.name == "prostate cancer"


def test_gene_8315_resolves_to_the_gene(colliding) -> None:
    assert {item.entity.canonical_id for item in _resolve(colliding, "8315", "gene").candidates} == {
        "primekg:gene:8315"
    }


def test_neighbors_never_cross_entity_types_for_the_same_native_id(colliding) -> None:
    entity = _resolve(colliding, "8315", "disease").selected
    assert entity is not None
    result = colliding.neighbors(NeighborQuery(entity=entity, direction="both", limit=50))
    names = {edge.head.name for edge in result.edges} | {
        edge.tail.name for edge in result.edges
    }
    assert "BRAP" not in names
    assert "prostate cancer" in names


def test_outgoing_target_type_equal_to_source_type_returns_empty(colliding) -> None:
    entity = _resolve(colliding, "D003", "drug").selected
    assert entity is not None
    result = colliding.neighbors(
        NeighborQuery(entity=entity, direction="outgoing", target_types=["drug"], limit=50)
    )
    assert result.edges == []


def test_outgoing_target_type_filters_only_the_tail(colliding) -> None:
    entity = _resolve(colliding, "D003", "drug").selected
    assert entity is not None
    result = colliding.neighbors(
        NeighborQuery(entity=entity, direction="outgoing", target_types=["disease"], limit=50)
    )
    assert {edge.tail.entity_type for edge in result.edges} == {"disease"}


def test_incoming_target_type_filters_only_the_head(colliding) -> None:
    entity = _resolve(colliding, "D003", "drug").selected
    assert entity is not None
    result = colliding.neighbors(
        NeighborQuery(entity=entity, direction="incoming", target_types=["gene"], limit=50)
    )
    assert {edge.head.entity_type for edge in result.edges} == {"gene"}


def test_both_with_target_types_splits_into_two_directional_queries(colliding) -> None:
    entity = _resolve(colliding, "D003", "drug").selected
    assert entity is not None
    result = colliding.neighbors(
        NeighborQuery(entity=entity, direction="both", target_types=["disease"], limit=50)
    )
    assert result.trace.normalized_request["split"] is True
    for edge in result.edges:
        if edge.head.canonical_id == entity.canonical_id:
            assert edge.tail.entity_type == "disease"
        else:
            assert edge.head.entity_type == "disease"


def test_missing_upstream_identity_returns_no_candidates(colliding) -> None:
    resolution = _resolve(colliding, "D999999", "disease")
    assert resolution.candidates == []
    assert resolution.ambiguous is False


def test_wrong_entity_type_for_known_id_returns_nothing(colliding) -> None:
    assert _resolve(colliding, "D003", "pathway").candidates == []


def test_duplicate_logical_rows_collapse(colliding_db: Path) -> None:
    connection = sqlite3.connect(colliding_db)
    connection.execute("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?)", COLLIDING_ROWS[0])
    connection.commit()
    connection.close()
    provider = PrimeKGSQLiteProvider(colliding_db)
    try:
        entity = _resolve(provider, "8315", "disease").selected
        assert entity is not None
        result = provider.neighbors(
            NeighborQuery(entity=entity, relations=["contraindication"], limit=50)
        )
        assert len({edge.edge_id for edge in result.edges}) == len(result.edges)
    finally:
        provider.close()


def test_small_snapshot_hashes_exactly_once(colliding, monkeypatch) -> None:
    import medical_qa_data_factory.knowledge_graph.providers.primekg as module

    calls: list[Path] = []
    original = module._sha256_file

    def _tracking(path: Path, **kwargs):
        calls.append(Path(path))
        return original(path, **kwargs)

    monkeypatch.setattr(module, "_sha256_file", _tracking)
    fresh = PrimeKGSQLiteProvider(colliding.database_path)
    fresh.close()
    assert len(calls) <= 1
    assert colliding.snapshot_state()["sha256"]


def test_manifest_init_writes_and_refuses_overwrite(colliding_db: Path) -> None:
    assert manifest_path_for(colliding_db).exists() is False
    payload = init_manifest(colliding_db)
    assert len(payload["sha256"]) == 64
    assert read_manifest(colliding_db)["sha256"] == payload["sha256"]
    with pytest.raises(Exception):
        init_manifest(colliding_db)


def test_manifest_refresh_keeps_audit_backup(colliding_db: Path) -> None:
    first = init_manifest(colliding_db)
    second = refresh_manifest(colliding_db)
    assert second["sha256"] == first["sha256"]
    manifest = manifest_path_for(colliding_db)
    assert manifest.with_name(f"{manifest.stem}.previous.json").exists()


def test_stale_manifest_is_detected_without_hashing(colliding_db: Path, monkeypatch) -> None:
    import medical_qa_data_factory.knowledge_graph.providers.primekg as module

    init_manifest(colliding_db)
    connection = sqlite3.connect(colliding_db)
    connection.execute("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?)", COLLIDING_ROWS[0])
    connection.commit()
    connection.close()

    calls: list[Path] = []

    def _tracking(path: Path, **kwargs):  # pragma: no cover - counting helper
        calls.append(Path(path))
        raise AssertionError("a stale snapshot must not trigger a full rescan")

    monkeypatch.setattr(module, "_sha256_file", _tracking)
    provider = PrimeKGSQLiteProvider(colliding_db)
    try:
        assert provider.snapshot_state()["stale_reason"] == "snapshot_stale"
    finally:
        provider.close()
    assert calls == []


def test_large_snapshot_without_manifest_is_never_hashed(tmp_path, monkeypatch) -> None:
    import medical_qa_data_factory.knowledge_graph.providers.primekg as module

    path = tmp_path / "big.sqlite"
    path.write_bytes(b"\0" * (module.MANIFEST_REQUIRED_ABOVE_BYTES + 1))
    monkeypatch.setattr(
        module,
        "_sha256_file",
        lambda *a, **k: pytest.fail("large snapshot must not be hashed implicitly"),
    )
    provider = PrimeKGSQLiteProvider(path)
    try:
        state = provider.snapshot_state()
        assert state["stale_reason"] == "snapshot_manifest_missing"
        assert state["sha256"] is None
    finally:
        provider.close()
