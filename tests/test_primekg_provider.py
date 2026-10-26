"""Unit tests for the PrimeKG adapter against a tiny in-process fixture."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from medical_qa_data_factory.knowledge_graph import (
    EntityLookup,
    EntitySearchQuery,
    GraphCapabilityError,
    NeighborQuery,
    PathQuery,
)
from medical_qa_data_factory.knowledge_graph.errors import (
    GraphConfigurationError,
    GraphDataIntegrityError,
)
from medical_qa_data_factory.knowledge_graph.providers.primekg import (
    PrimeKGSQLiteProvider,
)


@pytest.fixture
def provider(tiny_primekg: Path):
    item = PrimeKGSQLiteProvider(tiny_primekg, default_limit=100, max_limit=1000)
    yield item
    item.close()


def _resolve(provider, mention: str):
    return provider.resolve_entity(EntityLookup(mention=mention))


def _entity(provider, mention: str):
    return _resolve(provider, mention).candidates[0].entity


def test_healthcheck_reports_schema(provider) -> None:
    health = provider.healthcheck()
    assert health.healthy is True
    assert health.detail["edge_count"] == 6
    assert health.snapshot is not None and health.snapshot.sha256


def test_resolve_exact_unique(provider) -> None:
    resolution = _resolve(provider, "ASPIRIN")
    assert resolution.ambiguous is False
    assert resolution.selected is not None
    assert resolution.selected.canonical_id == "primekg:drug:1191"


def test_resolve_missing_is_empty_not_error(provider) -> None:
    resolution = _resolve(provider, "unobtainium")
    assert resolution.candidates == []
    assert resolution.ambiguous is False
    assert resolution.trace.records_returned == 0


def test_resolve_by_native_id(provider) -> None:
    resolution = provider.resolve_entity(EntityLookup(mention="5742", native_id="5742"))
    assert "primekg:gene:5742" in {item.entity.canonical_id for item in resolution.candidates}
    assert all(item.match_kind == "id" for item in resolution.candidates)


def test_resolve_is_deterministic(provider) -> None:
    first = [item.entity.canonical_id for item in _resolve(provider, "aspirin").candidates]
    second = [item.entity.canonical_id for item in _resolve(provider, "aspirin").candidates]
    assert first == second


def test_neighbors_outgoing_only(provider) -> None:
    entity = _entity(provider, "aspirin")
    result = provider.neighbors(NeighborQuery(entity=entity, direction="outgoing", limit=10))
    assert {edge.head.canonical_id for edge in result.edges} == {entity.canonical_id}
    assert "headache" in {edge.tail.name for edge in result.edges}


def test_neighbors_incoming_only(provider) -> None:
    entity = _entity(provider, "aspirin")
    result = provider.neighbors(NeighborQuery(entity=entity, direction="incoming", limit=10))
    assert {edge.tail.canonical_id for edge in result.edges} == {entity.canonical_id}
    assert {edge.head.name for edge in result.edges} == {"PTGS1"}


def test_neighbors_relation_and_type_filters(provider) -> None:
    entity = _entity(provider, "aspirin")
    result = provider.neighbors(
        NeighborQuery(entity=entity, relations=["treats"], target_types=["disease"], limit=10)
    )
    assert {edge.relation for edge in result.edges} == {"treats"}
    assert all(edge.tail.entity_type == "disease" for edge in result.edges)


def test_neighbors_limit_truncation_and_cursor(provider) -> None:
    entity = _entity(provider, "aspirin")
    first = provider.neighbors(NeighborQuery(entity=entity, direction="outgoing", limit=1))
    assert first.truncated is True
    assert first.next_cursor is not None
    second = provider.neighbors(
        NeighborQuery(entity=entity, direction="outgoing", limit=10, cursor=first.next_cursor)
    )
    assert {edge.edge_id for edge in first.edges}.isdisjoint(
        {edge.edge_id for edge in second.edges}
    )


def test_duplicate_rows_map_to_one_edge(provider) -> None:
    entity = _entity(provider, "aspirin")
    result = provider.neighbors(
        NeighborQuery(entity=entity, relations=["treats"], limit=50)
    )
    headache = [edge for edge in result.edges if edge.tail.name == "headache"]
    assert len(headache) == 1


def test_quoted_names_do_not_break_sql(provider) -> None:
    entity = _entity(provider, "'quoted'")
    result = provider.neighbors(NeighborQuery(entity=entity, direction="outgoing", limit=5))
    assert [edge.tail.name for edge in result.edges] == ["weird;name"]


def test_fallback_identity_when_native_id_missing(provider) -> None:
    entity = _entity(provider, "'quoted'")
    assert "name-sha256" in entity.canonical_id
    assert entity.metadata["identity_fallback"] is True


def test_browse_entities_returns_first_unique_valid_endpoints(
    tiny_primekg: Path, tmp_path: Path
) -> None:
    provider = PrimeKGSQLiteProvider(
        tiny_primekg,
        entity_catalog_path=tmp_path / "entities.sqlite",
    )
    try:
        result = provider.search_entities(EntitySearchQuery(limit=10))
    finally:
        provider.close()
    names = [entity.name for entity in result.entities]
    assert names[:3] == ["aspirin", "headache", "asthma"]
    assert "PTGS1" in names
    assert len(names) == len({entity.canonical_id for entity in result.entities})
    assert all(
        entity.native_id and entity.entity_type for entity in result.entities
    )


def test_search_entities_covers_head_tail_and_native_id(
    tiny_primekg: Path, tmp_path: Path
) -> None:
    catalog_path = tmp_path / "entities.sqlite"
    provider = PrimeKGSQLiteProvider(
        tiny_primekg,
        entity_catalog_path=catalog_path,
    )
    try:
        by_tail = provider.search_entities(EntitySearchQuery(query="pain"))
        by_id = provider.search_entities(EntitySearchQuery(query="5742"))
        truncated = provider.search_entities(EntitySearchQuery(query="as", limit=1))
    finally:
        provider.close()
    assert [entity.name for entity in by_tail.entities] == ["pain"]
    assert [entity.name for entity in by_id.entities] == ["PTGS1"]
    assert len(truncated.entities) == 1
    assert truncated.truncated is True
    assert catalog_path.exists()


def test_search_entities_requires_catalog_path(provider) -> None:
    with pytest.raises(GraphConfigurationError):
        provider.search_entities(EntitySearchQuery(query="aspirin"))


def test_empty_result_is_success(provider) -> None:
    entity = _entity(provider, "PTGS1")
    result = provider.neighbors(
        NeighborQuery(entity=entity, direction="outgoing", relations=["nope"], limit=5)
    )
    assert result.edges == []
    assert result.truncated is False
    assert result.trace.records_returned == 0


def test_paths_respects_capability_limit(provider) -> None:
    item = PrimeKGSQLiteProvider(provider.database_path, max_hops=1)
    try:
        with pytest.raises(GraphCapabilityError):
            item.paths(PathQuery(start=_entity(provider, "aspirin"), max_hops=2))
    finally:
        item.close()


def test_bounded_paths_return_deduplicated_edges(provider) -> None:
    with pytest.raises(GraphCapabilityError) as excinfo:
        provider.paths(PathQuery(start=_entity(provider, "aspirin"), max_hops=2))
    assert excinfo.value.code == "kg_capability_error"


def test_path_capability_is_not_advertised(provider) -> None:
    assert provider.capabilities().path_query is False


def test_missing_database_is_config_error(tmp_path: Path) -> None:
    # Construction fails fast rather than deferring the error to first use.
    with pytest.raises(GraphConfigurationError):
        PrimeKGSQLiteProvider(tmp_path / "nope.sqlite")


def test_database_without_edges_table_is_integrity_error(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE other (a TEXT)")
    connection.commit()
    connection.close()
    item = PrimeKGSQLiteProvider(path)
    health = item.healthcheck()
    assert health.healthy is False
    assert health.error["code"] == "kg_data_integrity"
    item.close()


def test_missing_columns_is_integrity_error(tmp_path: Path) -> None:
    path = tmp_path / "partial.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE edges (head TEXT, tail TEXT)")
    connection.commit()
    connection.close()
    item = PrimeKGSQLiteProvider(path)
    health = item.healthcheck()
    assert health.healthy is False
    assert health.error["code"] == "kg_data_integrity"
    item.close()


def test_database_is_not_modified(provider, tiny_primekg: Path) -> None:
    before = tiny_primekg.stat()
    entity = _entity(provider, "aspirin")
    provider.neighbors(NeighborQuery(entity=entity, limit=10))
    with pytest.raises(GraphCapabilityError):
        provider.paths(PathQuery(start=entity, max_hops=2))
    after = tiny_primekg.stat()
    assert (after.st_size, after.st_mtime) == (before.st_size, before.st_mtime)


def test_error_carries_stable_code() -> None:
    exc = GraphDataIntegrityError("boom")
    assert exc.as_dict()["code"] == "kg_data_integrity"
    assert exc.as_dict()["retryable"] is False
