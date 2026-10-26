"""Bounded N-hop frontier expansion.

The provider's ``paths()`` cannot return a complete ordered walk, so the
project assembles walks itself. This module does that for an arbitrary hop
count, under an explicit budget.

The rules that make the result trustworthy:

* A hop is only ever extended from an entity that a previous hop actually
  reached. Continuity is checked against real edge endpoints, never against
  anything the caller asserted.
* The same edge is never walked twice, the walk never returns to where it
  started, and a step never ends where it began.
* Every hop comes from the same provider and the same snapshot. A path that
  would stitch two snapshots is rejected, not silently repaired.
* The budget is checked *before* a query goes out, so a high-degree entity
  cannot blow past the row cap. Truncation is reported, never hidden.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .contracts import (
    DEFAULT_MAX_HOPS,
    MAX_HOPS_CEILING,
    GraphEdge,
    GraphEntity,
    NeighborQuery,
    OrderedPath,
    OrderedPathStep,
)
from .ports import KnowledgeGraphProvider


#: Default number of entities expanded at one level of the frontier.
DEFAULT_MAX_FRONTIER = 6
#: Default cap on how many entities one relation may contribute.
DEFAULT_PER_RELATION_CAP = 2
#: Default cap on the rows a single hop may return.
DEFAULT_HOP_LIMIT = 50


def path_digest(*, snapshot_id: str, steps: Sequence[tuple[str, str, str]]) -> str:
    """Stable digest over the snapshot and the directed step list.

    Direction is part of the identity: walking an edge backwards is a
    different traversal from walking it forwards.
    """
    blob = "\n".join(
        "|".join((snapshot_id, edge_id, direction, relation))
        for edge_id, direction, relation in steps
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def traversal_direction(
    edge: GraphEdge, *, from_entity_id: str, to_entity_id: str
) -> str | None:
    """The direction the edge is walked in for this from/to pair."""
    if (
        edge.head.canonical_id == from_entity_id
        and edge.tail.canonical_id == to_entity_id
    ):
        return "forward"
    if (
        edge.tail.canonical_id == from_entity_id
        and edge.head.canonical_id == to_entity_id
    ):
        return "reverse"
    return None


@dataclass(frozen=True)
class FrontierBudget:
    """The limits one expansion may not exceed."""

    max_hops: int = DEFAULT_MAX_HOPS
    max_frontier: int = DEFAULT_MAX_FRONTIER
    per_relation_cap: int = DEFAULT_PER_RELATION_CAP
    hop_limit: int = DEFAULT_HOP_LIMIT
    max_paths: int = 20
    max_rows: int = 500

    def __post_init__(self) -> None:
        if not 1 <= self.max_hops <= MAX_HOPS_CEILING:
            raise ValueError(
                f"max_hops must be between 1 and {MAX_HOPS_CEILING}, "
                f"got {self.max_hops}"
            )
        if self.max_frontier < 1:
            raise ValueError("max_frontier must be at least 1")
        if self.per_relation_cap < 1:
            raise ValueError("per_relation_cap must be at least 1")
        if self.hop_limit < 1:
            raise ValueError("hop_limit must be at least 1")
        if self.max_paths < 1:
            raise ValueError("max_paths must be at least 1")
        if self.max_rows < 1:
            raise ValueError("max_rows must be at least 1")


@dataclass
class FrontierLevel:
    """One hop's worth of expansion."""

    depth: int
    entity: GraphEntity
    edges: list[GraphEdge] = field(default_factory=list)
    trace_id: str = ""
    truncated: bool = False
    rows_examined: int = 0
    snapshot_id: str = ""


@dataclass
class ExpansionResult:
    """Everything the expansion produced and what it cost."""

    levels: list[FrontierLevel] = field(default_factory=list)
    paths: list[OrderedPath] = field(default_factory=list)
    all_edges: list[GraphEdge] = field(default_factory=list)
    tool_calls: int = 0
    rows_examined: int = 0
    truncated: bool = False
    skipped_entities: int = 0
    error_codes: list[str] = field(default_factory=list)


@dataclass
class _Walk:
    """A partial walk under construction."""

    steps: list[OrderedPathStep] = field(default_factory=list)
    #: Entities the walk has already been through, including the start.
    visited: set[str] = field(default_factory=set)
    #: Edge ids the walk has already used.
    used_edges: set[str] = field(default_factory=set)


def select_frontier(
    edges: Iterable[GraphEdge],
    *,
    cap: int,
    per_relation_cap: int = DEFAULT_PER_RELATION_CAP,
) -> list[GraphEntity]:
    """Pick a stable, bounded set of entities to expand next.

    High-degree fan-out is the main cost and noise source, so the cap applies
    before any further query runs. Ordering is deterministic: by relation,
    then by canonical id, then by edge id.
    """
    by_id: dict[str, GraphEntity] = {}
    per_relation: dict[str, int] = {}
    for edge in sorted(
        edges, key=lambda item: (item.relation, item.tail.canonical_id, item.edge_id)
    ):
        middle = edge.tail
        key = middle.canonical_id
        if key in by_id:
            continue
        used = per_relation.get(edge.relation, 0)
        if used >= per_relation_cap:
            continue
        per_relation[edge.relation] = used + 1
        by_id[key] = middle
        if len(by_id) >= cap:
            break
    return list(by_id.values())


class NHopPathBuilder:
    """Assembles ordered walks of any length from bounded neighbor queries.

    The provider is the only thing that touches data. This class never builds
    SQL, never reads a file path, and never accepts a tool name from a caller.
    """

    def __init__(
        self,
        provider: KnowledgeGraphProvider,
        *,
        budget: FrontierBudget | None = None,
    ) -> None:
        self.provider = provider
        self.budget = budget or FrontierBudget()

    def build(
        self,
        seed: GraphEntity,
        *,
        relations: list[str] | None = None,
        target_types: list[str] | None = None,
        max_hops: int | None = None,
        expand: list[list[GraphEntity]] | None = None,
    ) -> ExpansionResult:
        """Expand from the seed for up to ``max_hops`` hops.

        ``expand`` lets a caller (or a later Planner) supply the exact
        entities to expand at each level, as one list per level. When omitted
        the builder selects them from the previous level under the budget.
        """
        hops = self.budget.max_hops if max_hops is None else int(max_hops)
        if hops < 1 or hops > MAX_HOPS_CEILING:
            raise ValueError(
                f"max_hops must be between 1 and {MAX_HOPS_CEILING}, got {hops}"
            )
        result = ExpansionResult()
        snapshot_id = ""
        # Walks under construction. The seed is a walk with no steps yet, so
        # the first hop extends it exactly like every later hop.
        walks: list[_Walk] = [_Walk(visited={seed.canonical_id})]
        frontier: list[GraphEntity] = [seed]
        # Entities that have already served as a frontier. This is separate
        # from "discovered": an entity is discovered as soon as an edge reaches
        # it, but it is only expanded once.
        expanded: set[str] = {seed.canonical_id}
        for depth in range(hops):
            next_entities: list[GraphEntity] = []
            for entity in frontier:
                if result.rows_examined >= self.budget.max_rows:
                    result.truncated = True
                    break
                if result.tool_calls >= self.budget.max_frontier * hops:
                    result.truncated = True
                    break
                level = self._expand_one(
                    entity,
                    depth=depth,
                    relations=list(relations or []),
                    target_types=list(target_types or []),
                )
                result.levels.append(level)
                result.tool_calls += 1
                result.rows_examined += level.rows_examined
                result.truncated = result.truncated or level.truncated
                result.all_edges.extend(level.edges)
                if not snapshot_id:
                    snapshot_id = level.snapshot_id
                elif level.snapshot_id and level.snapshot_id != snapshot_id:
                    # A snapshot change mid-walk means the walk is not
                    # reproducible. It is recorded and the level is dropped.
                    result.error_codes.append("snapshot_drift")
                    continue
                for edge in level.edges:
                    direction, end_id, end_entity = _direction_and_end(edge, entity)
                    if direction is None:
                        # The edge does not touch this entity at all.
                        continue
                    walks.extend(
                        self._extend(
                            walks, edge, entity, direction, end_id, level, depth
                        )
                    )
                    if end_id not in expanded:
                        next_entities.append(end_entity)
            if not next_entities:
                break
            # The next level expands the entities the walks actually ended at.
            # Picking them from the raw edges instead would expand entities no
            # walk reached, and the resulting steps would not connect.
            reachable: dict[str, GraphEntity] = {}
            for walk in walks:
                if not walk.steps:
                    continue
                end_id = walk.steps[-1].to_entity_id
                if end_id in expanded:
                    continue
                for entity in next_entities:
                    if entity.canonical_id == end_id:
                        reachable[end_id] = entity
                        break
            if expand is not None and depth + 1 < len(expand):
                chosen = [
                    item
                    for item in expand[depth + 1]
                    if item.canonical_id in reachable
                ]
            else:
                chosen = [
                    reachable[key]
                    for key in sorted(reachable)
                ][: self.budget.max_frontier]
            result.skipped_entities += max(0, len(next_entities) - len(chosen))
            frontier = chosen[: self.budget.max_frontier]
            expanded.update(item.canonical_id for item in frontier)
            if not frontier:
                break
        result.paths = self._finalise(walks, snapshot_id=snapshot_id)
        return result

    def _extend(
        self,
        walks: list[_Walk],
        edge: GraphEdge,
        entity: GraphEntity,
        direction: str,
        end_id: str,
        level: FrontierLevel,
        depth: int,
    ) -> list[_Walk]:
        """Return the walks extended by one more step.

        A walk is never modified in place. Each (walk, edge) pair produces a
        new walk, and the original is kept, so a branching graph yields every
        distinct walk instead of one spliced mixture of them.
        """
        if end_id == entity.canonical_id:
            # A self loop is not progress.
            return []
        extended: list[_Walk] = []
        for walk in walks:
            if walk.steps:
                if walk.steps[-1].to_entity_id != entity.canonical_id:
                    # Continuity is checked against the real edge endpoints.
                    continue
                if edge.edge_id in walk.used_edges:
                    # The same edge twice is a loop, not two hops.
                    continue
                if end_id in walk.visited:
                    # Returning to an entity already on the walk closes a
                    # cycle, and walking back to the start closes a loop.
                    continue
            else:
                # The seed walk: it has no steps yet, so any edge may start
                # it. It may only ever do so at the first hop, otherwise a
                # walk would appear to start somewhere it never visited.
                if depth != 0:
                    continue
                if end_id in walk.visited:
                    continue
            step = OrderedPathStep(
                position=len(walk.steps),
                edge_id=edge.edge_id,
                traversal_direction=direction,  # type: ignore[arg-type]
                from_entity_id=entity.canonical_id,
                relation=edge.relation,
                to_entity_id=end_id,
                query_trace_id=level.trace_id,
            )
            extended.append(
                _Walk(
                    steps=[*walk.steps, step],
                    visited={*walk.visited, end_id},
                    used_edges={*walk.used_edges, edge.edge_id},
                )
            )
        return extended

    def _expand_one(
        self,
        entity: GraphEntity,
        *,
        depth: int,
        relations: list[str],
        target_types: list[str],
    ) -> FrontierLevel:
        result = self.provider.neighbors(
            NeighborQuery(
                entity=entity,
                direction="both",
                relations=relations,
                target_types=target_types,
                limit=self.budget.hop_limit,
            )
        )
        trace = getattr(result, "trace", None)
        snapshot = getattr(result, "snapshot", None)
        return FrontierLevel(
            depth=depth,
            entity=entity,
            edges=list(result.edges),
            trace_id=str(getattr(trace, "query_id", "") or ""),
            truncated=bool(getattr(result, "truncated", False)) or bool(getattr(trace, "truncated", False)),
            rows_examined=int(getattr(trace, "rows_examined", 0) or 0),
            snapshot_id=str(getattr(snapshot, "data_snapshot_id", "") or ""),
        )

    def _finalise(
        self, walks: list[_Walk], *, snapshot_id: str
    ) -> list[OrderedPath]:
        """Turn the collected walks into deduplicated, ordered paths."""
        seen: set[str] = set()
        paths: list[OrderedPath] = []
        for walk in walks:
            if len(walk.steps) < 2:
                # A single step is an edge, not a path.
                continue
            digest = path_digest(
                snapshot_id=snapshot_id,
                steps=[
                    (item.edge_id, item.traversal_direction, item.relation)
                    for item in walk.steps
                ],
            )
            if digest in seen:
                continue
            seen.add(digest)
            paths.append(
                OrderedPath(
                    path_id=f"opath.{digest[:24]}",
                    provider_id=self.provider.provider_id,
                    snapshot_id=snapshot_id,
                    start_entity_id=walk.steps[0].from_entity_id,
                    end_entity_id=walk.steps[-1].to_entity_id,
                    steps=list(walk.steps),
                    intermediate_entity_ids=[
                        item.to_entity_id for item in walk.steps[:-1]
                    ],
                    source_edge_ids=[item.edge_id for item in walk.steps],
                    path_hash=digest,
                    query_trace_ids=[item.query_trace_id for item in walk.steps],
                )
            )
        return sorted(paths, key=lambda item: item.path_hash)[: self.budget.max_paths]


def _direction_and_end(
    edge: GraphEdge, entity: GraphEntity
) -> tuple[str | None, str, GraphEntity]:
    """The direction the edge leaves ``entity``, where it ends, and the end."""
    if edge.head.canonical_id == entity.canonical_id:
        return "forward", edge.tail.canonical_id, edge.tail
    if edge.tail.canonical_id == entity.canonical_id:
        return "reverse", edge.head.canonical_id, edge.head
    return None, "", entity