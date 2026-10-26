"""Ordered two-hop path assembly.

These tests use fixture graphs, not the production database. The point is to
prove that a disconnected pair of edges never becomes a path, and that the
same two edges walked in different directions are not collapsed together.
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from medical_qa_data_factory.knowledge_graph import (
    EntityLookup,
    EntityResolution,
    GraphCapabilities,
    GraphEdge,
    GraphEntity,
    GraphHealth,
    GraphQueryResult,
    GraphQueryTrace,
    GraphSnapshot,
    NeighborQuery,
    OrderedPath,
    OrderedPathStep,
    assemble_paths,
    bounded_intermediates,
    path_digest,
    traversal_direction,
)
from medical_qa_data_factory.knowledge_graph.errors import GraphQueryError
from medical_qa_data_factory.knowledge_graph.path_builder import BoundedPathBuilder


def _entity(name: str, native: str, kind: str) -> GraphEntity:
    return GraphEntity(
        provider_id="fake",
        native_id=native,
        canonical_id=f"fake:{kind}:{native}",
        name=name,
        entity_type=kind,
    )


ASPIRIN = _entity("aspirin", "1", "exposure")
PTGS1 = _entity("PTGS1", "5742", "protein")
MIGRAINE = _entity("migraine", "B156", "disease")
WARFARIN = _entity("warfarin", "D002", "exposure")
ORPHAN = _entity("orphan", "9", "pathway")
_ALL_ENTITIES = [ASPIRIN, PTGS1, MIGRAINE, WARFARIN, ORPHAN]


def _snapshot() -> GraphSnapshot:
    return GraphSnapshot(
        provider_id="fake",
        adapter_version="fake-v1",
        data_snapshot_id="snapshot-1",
        config_hash="config-1",
    )


def _trace(query_id: str, rows: int = 0) -> GraphQueryTrace:
    return GraphQueryTrace(
        query_id=query_id,
        provider_id="fake",
        operation="neighbors",
        normalized_request={},
        started_at="2026-01-01T00:00:00Z",
        elapsed_ms=1,
        rows_examined=rows,
        records_returned=0,
        truncated=False,
    )


def _edge(head: GraphEntity, relation: str, tail: GraphEntity, edge_id: str) -> GraphEdge:
    return GraphEdge(
        provider_id="fake",
        edge_id=edge_id,
        head=head,
        relation=relation,
        tail=tail,
        source="fixture",
    )


def _step(
    position: int, edge_id: str, direction: str, start: str, end: str, relation: str = "r"
) -> OrderedPathStep:
    return OrderedPathStep(
        position=position,
        edge_id=edge_id,
        traversal_direction=direction,
        from_entity_id=start,
        relation=relation,
        to_entity_id=end,
        query_trace_id=f"trace-{position}",
    )


def _path() -> OrderedPath:
    return OrderedPath(
        path_id="opath.1",
        provider_id="fake",
        snapshot_id="snapshot-1",
        start_entity_id=ASPIRIN.canonical_id,
        end_entity_id=MIGRAINE.canonical_id,
        steps=[
            _step(0, "e1", "forward", ASPIRIN.canonical_id, PTGS1.canonical_id),
            _step(1, "e2", "reverse", PTGS1.canonical_id, MIGRAINE.canonical_id),
        ],
        intermediate_entity_ids=[PTGS1.canonical_id],
        source_edge_ids=["e1", "e2"],
        path_hash="h" * 64,
        query_trace_ids=["trace-0", "trace-1"],
    )


class FixtureProvider:
    """Answers neighbor queries from an in-memory adjacency map."""

    provider_id = "fake"

    def __init__(self, adjacency: dict[str, list[GraphEdge]]) -> None:
        self.adjacency = adjacency
        self.calls: list[NeighborQuery] = []

    def capabilities(self) -> GraphCapabilities:
        return GraphCapabilities(
            entity_lookup_by_name=True,
            entity_lookup_by_id=True,
            incoming_edges=True,
            outgoing_edges=True,
            bounded_expansion=True,
            path_query=False,
        )

    def healthcheck(self) -> GraphHealth:
        return GraphHealth(provider_id="fake", healthy=True)

    def snapshot(self) -> GraphSnapshot:
        return _snapshot()

    def resolve_entity(self, query: EntityLookup) -> EntityResolution:
        selected = next(
            (item for item in _ALL_ENTITIES if item.name == query.mention), None
        )
        return EntityResolution(
            candidates=[], selected=selected, ambiguous=False, trace=_trace("resolve")
        )

    def neighbors(self, query: NeighborQuery) -> GraphQueryResult:
        self.calls.append(query)
        key = query.entity.canonical_id
        edges = [
            edge
            for edge in self.adjacency.get(key, [])
            if not query.relations or edge.relation in query.relations
        ]
        edges = edges[: query.limit]
        return GraphQueryResult(
            edges=edges,
            trace=_trace(f"trace-{len(self.calls)}", rows=len(edges)),
            snapshot=_snapshot(),
            truncated=False,
        )

    def paths(self, query: Any) -> GraphQueryResult:
        raise GraphQueryError("paths is disabled")

    def close(self) -> None:
        return None

# ---------------------------------------------------------------- contract


def test_ordered_path_round_trips() -> None:
    path = _path()
    restored = OrderedPath.model_validate(path.model_dump(mode="json"))
    assert restored.steps[0].to_entity_id == restored.steps[1].from_entity_id
    assert restored.end_entity_id == MIGRAINE.canonical_id


def test_broken_path_is_rejected() -> None:
    payload = _path().model_dump(mode="json")
    payload["steps"][1]["from_entity_id"] = "fake:protein:9999"
    with pytest.raises(ValidationError) as excinfo:
               OrderedPath.model_validate(payload)
    assert "broken" in str(excinfo.value)


def test_self_loop_step_is_rejected() -> None:
    payload = _path().model_dump(mode="json")
    payload["steps"][0]["to_entity_id"] = payload["steps"][0]["from_entity_id"]
    with pytest.raises(ValidationError):
        OrderedPath.model_validate(payload)


def test_reused_edge_is_rejected() -> None:
    payload = _path().model_dump(mode="json")
    payload["steps"][1]["edge_id"] = payload["steps"][0]["edge_id"]
    payload["source_edge_ids"] = ["e1", "e1"]
    with pytest.raises(ValidationError):
        OrderedPath.model_validate(payload)


def test_return_to_start_is_rejected() -> None:
    payload = _path().model_dump(mode="json")
    payload["steps"][1]["to_entity_id"] = payload["steps"][0]["from_entity_id"]
    payload["end_entity_id"] = payload["steps"][0]["from_entity_id"]
    with pytest.raises(ValidationError):
        OrderedPath.model_validate(payload)


def test_single_step_is_rejected() -> None:
    payload = _path().model_dump(mode="json")
    payload["steps"] = payload["steps"][:1]
    payload["intermediate_entity_ids"] = []
    payload["source_edge_ids"] = ["e1"]
    payload["query_trace_ids"] = ["trace-0"]
    with pytest.raises(ValidationError):
        OrderedPath.model_validate(payload)


def test_traversal_direction_recognises_both_ways() -> None:
    edge = _edge(ASPIRIN, "treats", MIGRAINE, "e1")
    assert (
        traversal_direction(
            edge, from_entity_id=ASPIRIN.canonical_id, to_entity_id=MIGRAINE.canonical_id
        )
        == "forward"
    )
    assert (
        traversal_direction(
            edge, from_entity_id=MIGRAINE.canonical_id, to_entity_id=ASPIRIN.canonical_id
        )
        == "reverse"
    )
    assert (
        traversal_direction(
            edge, from_entity_id=PTGS1.canonical_id, to_entity_id=ASPIRIN.canonical_id
        )
        is None
    )


def test_path_digest_is_direction_sensitive() -> None:
    forward = path_digest(
        snapshot_id="s1", steps=[("e1", "forward", "r"), ("e2", "forward", "r")]
    )
    backward = path_digest(
        snapshot_id="s1", steps=[("e1", "reverse", "r"), ("e2", "forward", "r")]
    )
    assert forward != backward
    assert forward == path_digest(
        snapshot_id="s1", steps=[("e1", "forward", "r"), ("e2", "forward", "r")]
    )
    assert forward != path_digest(
        snapshot_id="s2", steps=[("e1", "forward", "r"), ("e2", "forward", "r")]
    )

# ---------------------------------------------------------------- assembly


def test_forward_forward_path_is_assembled() -> None:
    # aspirin -> PTGS1 -> migraine
    adjacency = {
        ASPIRIN.canonical_id: [_edge(ASPIRIN, "drug_protein", PTGS1, "e1")],
        PTGS1.canonical_id: [_edge(PTGS1, "disease_protein", MIGRAINE, "e2")],
    }
    builder = BoundedPathBuilder(FixtureProvider(adjacency))
    result = builder.build(ASPIRIN)
    assert len(result.paths) == 1
    path = result.paths[0]
    assert path.start_entity_id == ASPIRIN.canonical_id
    assert path.end_entity_id == MIGRAINE.canonical_id
    assert [s.traversal_direction for s in path.steps] == ["forward", "forward"]
    assert result.tool_calls == 2


def test_forward_reverse_path_is_assembled() -> None:
    # aspirin -> PTGS1, migraine -> PTGS1  (so step two is walked backwards)
    adjacency = {
        ASPIRIN.canonical_id: [_edge(ASPIRIN, "drug_protein", PTGS1, "e1")],
        PTGS1.canonical_id: [_edge(MIGRAINE, "disease_protein", PTGS1, "e2")],
    }
    builder = BoundedPathBuilder(FixtureProvider(adjacency))
    result = builder.build(ASPIRIN)
    assert len(result.paths) == 1
    assert [s.traversal_direction for s in result.paths[0].steps] == [
        "forward",
        "reverse",
    ]
    assert result.paths[0].end_entity_id == MIGRAINE.canonical_id


def test_disconnected_second_hop_is_dropped() -> None:
    """Two edges that do not share the intermediate must not be joined."""
    adjacency = {
        ASPIRIN.canonical_id: [_edge(ASPIRIN, "drug_protein", PTGS1, "e1")],
        PTGS1.canonical_id: [],
        ORPHAN.canonical_id: [_edge(ORPHAN, "pathway_pathway", MIGRAINE, "e9")],
    }
    builder = BoundedPathBuilder(FixtureProvider(adjacency))
    result = builder.build(ASPIRIN)
    assert result.paths == []
    assert result.first_hop_edges


def test_second_hop_back_to_seed_is_not_a_path() -> None:
    adjacency = {
        ASPIRIN.canonical_id: [_edge(ASPIRIN, "drug_protein", PTGS1, "e1")],
        # migraine -> aspirin, walked backwards from PTGS1 lands on aspirin.
        PTGS1.canonical_id: [
            _edge(MIGRAINE, "disease_protein", ASPIRIN, "e2"),
            _edge(MIGRAINE, "disease_protein", PTGS1, "e3"),
        ],
    }
    builder = BoundedPathBuilder(FixtureProvider(adjacency))
    result = builder.build(ASPIRIN)
    assert [p.end_entity_id for p in result.paths] == [MIGRAINE.canonical_id]


def test_same_edges_different_directions_are_distinct_paths() -> None:
    adjacency = {
        ASPIRIN.canonical_id: [
            _edge(ASPIRIN, "drug_protein", PTGS1, "e1"),
            _edge(ASPIRIN, "drug_protein", MIGRAINE, "e2"),
        ],
        PTGS1.canonical_id: [_edge(PTGS1, "disease_protein", MIGRAINE, "e3")],
        MIGRAINE.canonical_id: [_edge(MIGRAINE, "disease_protein", PTGS1, "e4")],
    }
    builder = BoundedPathBuilder(FixtureProvider(adjacency))
    result = builder.build(ASPIRIN)
    digests = {p.path_hash for p in result.paths}
    assert len(digests) == len(result.paths)
    assert len(result.paths) >= 2


def test_duplicate_edges_collapse_to_one_path() -> None:
    adjacency = {
        ASPIRIN.canonical_id: [
            _edge(ASPIRIN, "drug_protein", PTGS1, "e1"),
            _edge(ASPIRIN, "drug_protein", PTGS1, "e1"),
        ],
        PTGS1.canonical_id: [
            _edge(PTGS1, "disease_protein", MIGRAINE, "e2"),
            _edge(PTGS1, "disease_protein", MIGRAINE, "e2"),
        ],
    }
    builder = BoundedPathBuilder(FixtureProvider(adjacency))
    result = builder.build(ASPIRIN)
    assert len(result.paths) == 1


def test_empty_first_hop_makes_no_second_call() -> None:
    builder = BoundedPathBuilder(FixtureProvider({ASPIRIN.canonical_id: []}))
    result = builder.build(ASPIRIN)
    assert result.paths == []
    assert result.tool_calls == 1


def test_intermediate_cap_bounds_fan_out() -> None:
    # Distinct relations so the per-relation cap does not bind first.
    edges = [
        _edge(ASPIRIN, f"rel_{i}", _entity(f"p{i}", str(i), "protein"), f"e{i}")
        for i in range(12)
    ]
    adjacency: dict[str, list[GraphEdge]] = {ASPIRIN.canonical_id: edges}
    for i in range(12):
        adjacency[f"fake:protein:{i}"] = [
            _edge(_entity(f"p{i}", str(i), "protein"), "disease_protein", MIGRAINE, f"x{i}")
        ]
    builder = BoundedPathBuilder(FixtureProvider(adjacency), max_intermediates=3)
    result = builder.build(ASPIRIN)
    # One first-hop call plus at most three second-hop calls.
    assert result.tool_calls == 4
    assert len(result.paths) == 3


def test_per_relation_cap_limits_a_single_relation_fan_out() -> None:
    """Twelve proteins on one relation expand at most per_relation_cap of them."""
    edges = [
        _edge(ASPIRIN, "drug_protein", _entity(f"p{i}", str(i), "protein"), f"e{i}")
        for i in range(12)
    ]
    adjacency: dict[str, list[GraphEdge]] = {ASPIRIN.canonical_id: edges}
    for i in range(12):
        adjacency[f"fake:protein:{i}"] = [
            _edge(_entity(f"p{i}", str(i), "protein"), "disease_protein", MIGRAINE, f"x{i}")
        ]
    builder = BoundedPathBuilder(FixtureProvider(adjacency), max_intermediates=9)
    result = builder.build(ASPIRIN)
    assert result.tool_calls == 3
    assert len(result.paths) == 2


def test_per_relation_cap_spreads_intermediates() -> None:
    edges = [
        _edge(ASPIRIN, "drug_protein", _entity(f"p{i}", str(i), "protein"), f"e{i}")
        for i in range(8)
    ]
    chosen = bounded_intermediates(edges, cap=10, per_relation_cap=2)
    assert len(chosen) == 2


def test_supplied_intermediates_are_honoured() -> None:
    adjacency = {
        ASPIRIN.canonical_id: [
            _edge(ASPIRIN, "drug_protein", PTGS1, "e1"),
            _edge(ASPIRIN, "drug_protein", _entity("p2", "2", "protein"), "e2"),
        ],
        PTGS1.canonical_id: [_edge(PTGS1, "disease_protein", MIGRAINE, "e3")],
        "fake:protein:2": [_edge(_entity("p2", "2", "protein"), "disease_protein", WARFARIN, "e4")],
    }
    provider = FixtureProvider(adjacency)
    builder = BoundedPathBuilder(provider)
    result = builder.build(ASPIRIN, intermediates=[PTGS1])
    assert [p.end_entity_id for p in result.paths] == [MIGRAINE.canonical_id]
    assert result.skipped_intermediates == 1


def test_relation_filter_reaches_the_second_hop() -> None:
    adjacency = {
        ASPIRIN.canonical_id: [_edge(ASPIRIN, "drug_protein", PTGS1, "e1")],
        PTGS1.canonical_id: [
            _edge(PTGS1, "disease_protein", MIGRAINE, "e2"),
            _edge(PTGS1, "other_relation", ORPHAN, "e3"),
        ],
    }
    builder = BoundedPathBuilder(FixtureProvider(adjacency))
    result = builder.build(ASPIRIN, second_relations=["disease_protein"])
    assert [p.end_entity_id for p in result.paths] == [MIGRAINE.canonical_id]


def test_max_paths_caps_output_stably() -> None:
    edges = [
        _edge(ASPIRIN, "drug_protein", _entity(f"p{i}", str(i), "protein"), f"e{i}")
        for i in range(6)
    ]
    adjacency: dict[str, list[GraphEdge]] = {ASPIRIN.canonical_id: edges}
    for i in range(6):
        adjacency[f"fake:protein:{i}"] = [
            _edge(
                _entity(f"p{i}", str(i), "protein"),
                "disease_protein",
                _entity(f"d{i}", f"D{i}", "disease"),
                f"x{i}",
            )
        ]
    builder = BoundedPathBuilder(FixtureProvider(adjacency), max_intermediates=6)
    first = [p.path_hash for p in builder.build(ASPIRIN, max_paths=2).paths]
    second = [p.path_hash for p in builder.build(ASPIRIN, max_paths=2).paths]
    assert len(first) == 2
    assert first == second


def test_assemble_paths_ignores_a_second_hop_for_an_unknown_intermediate() -> None:
    paths = assemble_paths(
        [_edge(ASPIRIN, "drug_protein", PTGS1, "e1")],
        {ORPHAN.canonical_id: [(_edge(ORPHAN, "r", MIGRAINE, "e2"), "t")]},
        provider_id="fake",
        snapshot_id="snapshot-1",
        max_paths=5,
    )
    assert paths == []
