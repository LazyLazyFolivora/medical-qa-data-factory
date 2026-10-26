"""Bounded N-hop expansion: 2, 3 and 4 hops, and every way a walk can be wrong.

Step 3 of the real-generation-agent plan. The point of these tests is that a
walk which is not a path - a broken chain, a cycle, a repeated edge, a walk
that returns to its start, a walk stitched from two snapshots - never becomes
an ``OrderedPath``.
"""
from __future__ import annotations

from typing import Any

import pytest

from medical_qa_data_factory.domain.common import utc_now
from medical_qa_data_factory.knowledge_graph.contracts import (
    GraphEdge,
    GraphEntity,
    GraphQueryResult,
    GraphQueryTrace,
    GraphSnapshot,
    NeighborQuery,
    OrderedPath,
)
from medical_qa_data_factory.knowledge_graph.frontier import (
    FrontierBudget,
    NHopPathBuilder,
    select_frontier,
)


def _entity(canonical_id: str, name: str, entity_type: str = "disease") -> GraphEntity:
    return GraphEntity(
        provider_id="fake",
        canonical_id=canonical_id,
        name=name,
        entity_type=entity_type,
    )


def _edge(
    head: GraphEntity,
    relation: str,
    tail: GraphEntity,
    edge_id: str | None = None,
) -> GraphEdge:
    return GraphEdge(
        provider_id="fake",
        edge_id=edge_id or f"e{head.canonical_id}-{relation}-{tail.canonical_id}",
        head=head,
        relation=relation,
        tail=tail,
    )


def _snapshot(snapshot_id: str = "snap-1") -> GraphSnapshot:
    return GraphSnapshot(
        provider_id="fake",
        adapter_version="1.0.0",
        data_snapshot_id=snapshot_id,
        config_hash="cfg",
    )


class _FakeProvider:
    """A provider whose neighbor table is given per entity."""

    def __init__(
        self,
        table: dict[str, list[GraphEdge]],
        snapshot_id: str = "snap-1",
        switch_to_snapshot: str | None = None,
    ):
        self.table = table
        self.snapshot_id = snapshot_id
        #: When set, the snapshot changes after the first query, so a walk
        #: that spans two snapshots can be produced.
        self.switch_to_snapshot = switch_to_snapshot
        self.queries: list[NeighborQuery] = []

    @property
    def provider_id(self) -> str:
        return "fake"

    def neighbors(self, query: NeighborQuery) -> GraphQueryResult:
        self.queries.append(query)
        if self.switch_to_snapshot is not None and len(self.queries) > 1:
            self.snapshot_id = self.switch_to_snapshot
        edges = self.table.get(query.entity.canonical_id, [])
        truncated = len(edges) > query.limit
        return GraphQueryResult(
            edges=edges[: query.limit],
            trace=GraphQueryTrace(
                query_id=f"q{len(self.queries)}",
                provider_id="fake",
                operation="neighbors",
                normalized_request={"direction": query.direction},
                started_at=utc_now(),
                rows_examined=len(edges),
                records_returned=min(len(edges), query.limit),
                truncated=truncated,
            ),
            snapshot=_snapshot(self.snapshot_id),
        )


ASPIRIN = _entity("drug:aspirin", "aspirin", "drug")
HEADACHE = _entity("disease:headache", "headache")
MIGRAINE = _entity("disease:migraine", "migraine")
PAIN = _entity("symptom:pain", "pain")
NASAL = _entity("symptom:runny_nose", "runny nose")


def _chain_provider() -> _FakeProvider:
    """A clean four-hop chain: aspirin -> headache -> migraine -> pain."""
    return _FakeProvider(
        {
            ASPIRIN.canonical_id: [_edge(ASPIRIN, "treats", HEADACHE)],
            HEADACHE.canonical_id: [_edge(HEADACHE, "indication", MIGRAINE)],
            MIGRAINE.canonical_id: [_edge(MIGRAINE, "symptom", PAIN)],
            PAIN.canonical_id: [],
        }
    )


# ---- hop counts ----------------------------------------------------------


def test_a_two_hop_walk_is_produced() -> None:
    builder = NHopPathBuilder(_chain_provider())
    result = builder.build(ASPIRIN, max_hops=2)
    assert result.paths
    # Paths are ordered by hash, not by length.
    path = max(result.paths, key=lambda item: len(item.steps))
    assert len(path.steps) == 2
    assert path.start_entity_id == ASPIRIN.canonical_id
    assert path.end_entity_id == MIGRAINE.canonical_id
    assert path.intermediate_entity_ids == [HEADACHE.canonical_id]


def test_a_three_hop_walk_is_produced() -> None:
    builder = NHopPathBuilder(_chain_provider())
    result = builder.build(ASPIRIN, max_hops=3)
    assert result.paths
    path = max(result.paths, key=lambda item: len(item.steps))
    assert len(path.steps) == 3
    assert path.end_entity_id == PAIN.canonical_id
    assert path.intermediate_entity_ids == [
        HEADACHE.canonical_id,
        MIGRAINE.canonical_id,
    ]


def test_a_four_hop_walk_is_produced() -> None:
    provider = _chain_provider()
    provider.table[PAIN.canonical_id] = [_edge(PAIN, "relates", NASAL)]
    builder = NHopPathBuilder(provider)
    result = builder.build(ASPIRIN, max_hops=4)
    assert result.paths
    path = max(result.paths, key=lambda item: len(item.steps))
    assert len(path.steps) == 4
    assert path.end_entity_id == NASAL.canonical_id


def test_the_hop_count_respects_the_requested_limit() -> None:
    builder = NHopPathBuilder(_chain_provider())
    result = builder.build(ASPIRIN, max_hops=2)
    assert all(len(path.steps) <= 2 for path in result.paths)


def test_a_hop_count_above_the_ceiling_is_rejected() -> None:
    builder = NHopPathBuilder(_chain_provider())
    with pytest.raises(ValueError):
        builder.build(ASPIRIN, max_hops=5)


def test_a_budget_above_the_ceiling_is_rejected() -> None:
    with pytest.raises(ValueError):
        FrontierBudget(max_hops=5)


def test_the_default_budget_is_three_hops() -> None:
    assert FrontierBudget().max_hops == 3


# ---- path validity -------------------------------------------------------


def test_every_step_is_continuous() -> None:
    builder = NHopPathBuilder(_chain_provider())
    for path in builder.build(ASPIRIN, max_hops=4).paths:
        for left, right in zip(path.steps, path.steps[1:]):
            assert left.to_entity_id == right.from_entity_id


def test_a_walk_that_returns_to_the_start_is_not_a_path() -> None:
    provider = _FakeProvider(
        {
            ASPIRIN.canonical_id: [_edge(ASPIRIN, "treats", HEADACHE)],
            HEADACHE.canonical_id: [_edge(HEADACHE, "contra", ASPIRIN)],
        }
    )
    builder = NHopPathBuilder(provider)
    result = builder.build(ASPIRIN, max_hops=2)
    assert result.paths == []


def test_a_repeated_edge_is_not_a_path() -> None:
    provider = _FakeProvider(
        {
            ASPIRIN.canonical_id: [
                _edge(ASPIRIN, "treats", HEADACHE),
                _edge(ASPIRIN, "treats", HEADACHE, edge_id="other"),
            ],
            HEADACHE.canonical_id: [_edge(HEADACHE, "treats", ASPIRIN)],
        }
    )
    builder = NHopPathBuilder(provider)
    result = builder.build(ASPIRIN, max_hops=2)
    for path in result.paths:
        assert len(set(path.source_edge_ids)) == len(path.source_edge_ids)


def test_a_cycle_is_not_a_path() -> None:
    """Returning to an entity already on the walk closes a cycle."""
    provider = _FakeProvider(
        {
            ASPIRIN.canonical_id: [_edge(ASPIRIN, "treats", HEADACHE)],
            HEADACHE.canonical_id: [_edge(HEADACHE, "related", MIGRAINE)],
            MIGRAINE.canonical_id: [_edge(MIGRAINE, "related", HEADACHE)],
        }
    )
    builder = NHopPathBuilder(provider)
    result = builder.build(ASPIRIN, max_hops=3)
    for path in result.paths:
        visited = [path.start_entity_id, *path.intermediate_entity_ids]
        assert len(set(visited)) == len(visited)


def test_a_disconnected_edge_is_never_stitched() -> None:
    """An edge that does not touch the current entity cannot extend a walk."""
    provider = _FakeProvider(
        {
            ASPIRIN.canonical_id: [_edge(ASPIRIN, "treats", HEADACHE)],
            HEADACHE.canonical_id: [_edge(MIGRAINE, "unrelated", PAIN)],
        }
    )
    builder = NHopPathBuilder(provider)
    result = builder.build(ASPIRIN, max_hops=2)
    for path in result.paths:
        assert path.steps[-1].edge_id == "edrug:aspirin-treats-disease:headache"


def test_a_self_loop_is_not_progress() -> None:
    provider = _FakeProvider(
        {
            ASPIRIN.canonical_id: [_edge(ASPIRIN, "self", ASPIRIN)],
        }
    )
    builder = NHopPathBuilder(provider)
    result = builder.build(ASPIRIN, max_hops=2)
    assert result.paths == []


# ---- snapshots -----------------------------------------------------------


def test_a_snapshot_change_mid_walk_is_reported() -> None:
    provider = _chain_provider()
    builder = NHopPathBuilder(provider)
    result = builder.build(ASPIRIN, max_hops=3)
    assert result.paths
    assert "snapshot_drift" not in result.error_codes
    # A walk that spans two snapshots must be reported, not stitched.
    drifting = _FakeProvider(
        _chain_provider().table, snapshot_id="snap-1", switch_to_snapshot="snap-2"
    )
    drifted = NHopPathBuilder(drifting).build(ASPIRIN, max_hops=3)
    assert "snapshot_drift" in drifted.error_codes


def test_a_path_records_its_snapshot() -> None:
    builder = NHopPathBuilder(_chain_provider())
    paths = builder.build(ASPIRIN, max_hops=2).paths
    assert paths
    assert all(item.snapshot_id == "snap-1" for item in paths)


# ---- budget and truncation ----------------------------------------------


def test_a_high_degree_entity_is_truncated_and_reported() -> None:
    fan = [_edge(ASPIRIN, "treats", _entity(f"d{i}", f"d{i}")) for i in range(20)]
    provider = _FakeProvider({ASPIRIN.canonical_id: fan})
    builder = NHopPathBuilder(
        provider, budget=FrontierBudget(max_hops=2, hop_limit=5, max_frontier=2)
    )
    result = builder.build(ASPIRIN, max_hops=2)
    assert result.truncated
    assert provider.queries[0].limit == 5


def test_the_frontier_is_capped() -> None:
    fan = [_edge(ASPIRIN, "treats", _entity(f"d{i}", f"d{i}")) for i in range(20)]
    provider = _FakeProvider({ASPIRIN.canonical_id: fan})
    builder = NHopPathBuilder(
        provider, budget=FrontierBudget(max_hops=2, max_frontier=3, hop_limit=50)
    )
    result = builder.build(ASPIRIN, max_hops=2)
    # Only the capped frontier is expanded.
    assert len(provider.queries) <= 1 + 3


def test_the_per_relation_cap_limits_one_relation() -> None:
    edges = [_edge(ASPIRIN, "treats", _entity(f"d{i}", f"d{i}")) for i in range(10)]
    chosen = select_frontier(edges, cap=10, per_relation_cap=2)
    assert len(chosen) == 2


def test_the_frontier_selection_is_deterministic() -> None:
    edges = [
        _edge(ASPIRIN, "b", _entity("d2", "d2")),
        _edge(ASPIRIN, "a", _entity("d1", "d1")),
        _edge(ASPIRIN, "a", _entity("d3", "d3")),
    ]
    first = select_frontier(edges, cap=2, per_relation_cap=2)
    second = select_frontier(list(reversed(edges)), cap=2, per_relation_cap=2)
    assert [item.canonical_id for item in first] == [
        item.canonical_id for item in second
    ]


def test_the_row_budget_stops_the_expansion() -> None:
    provider = _chain_provider()
    builder = NHopPathBuilder(
        provider, budget=FrontierBudget(max_hops=4, max_rows=1)
    )
    result = builder.build(ASPIRIN, max_hops=4)
    assert result.truncated
    assert result.rows_examined <= 2


# ---- the ordered path contract -------------------------------------------


def test_the_ordered_path_contract_accepts_an_n_hop_walk() -> None:
    """The schema itself must not cap a walk at two hops."""
    steps = []
    entities = [ASPIRIN, HEADACHE, MIGRAINE, PAIN]
    for index in range(3):
        steps.append(
            {
                "position": index,
                "edge_id": f"e{index}",
                "traversal_direction": "forward",
                "from_entity_id": entities[index].canonical_id,
                "relation": "rel",
                "to_entity_id": entities[index + 1].canonical_id,
                "query_trace_id": f"q{index}",
            }
        )
    path = OrderedPath(
        path_id="opath.1",
        provider_id="fake",
        snapshot_id="snap-1",
        start_entity_id=ASPIRIN.canonical_id,
        end_entity_id=PAIN.canonical_id,
        steps=steps,
        intermediate_entity_ids=[HEADACHE.canonical_id, MIGRAINE.canonical_id],
        source_edge_ids=["e0", "e1", "e2"],
        path_hash="h",
        query_trace_ids=["q0", "q1", "q2"],
    )
    assert len(path.steps) == 3