"""Bounded two-hop path assembly.

PrimeKG's ``paths()`` ignores relation and type filters and cannot return a
full ordered walk, so the project assembles paths itself from bounded neighbor
queries: one first hop from the verified seed, then a filtered second hop from
each intermediate entity. The result is an ``OrderedPath`` whose every step
carries the edge id and query trace that produced it.

Nothing here chooses a next action. It is a pure, deterministic executor
primitive; the Planner that decides what to expand arrives in a later step.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from .contracts import (
    GraphEdge,
    GraphEntity,
    NeighborQuery,
    OrderedPath,
    OrderedPathStep,
)
from .ports import KnowledgeGraphProvider


DEFAULT_MAX_INTERMEDIATES = 5
DEFAULT_PER_RELATION_CAP = 2


def path_digest(*, snapshot_id: str, steps: list[tuple[str, str, str]]) -> str:
    """Stable digest over the snapshot and the directed step list.

    Direction is part of the identity: walking an edge backwards is a different
    traversal from walking it forwards, so the same pair of edges in opposite
    orders must not collapse into one path.
    """
    blob = "\n".join(
        "|".join((snapshot_id, edge_id, direction, relation)) for
        edge_id, direction, relation in steps
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def traversal_direction(
    edge: GraphEdge, *, from_entity_id: str, to_entity_id: str
) -> str | None:
    """Direction the edge is walked in for this from/to pair, or ``None``."""
    if edge.head.canonical_id == from_entity_id and edge.tail.canonical_id == to_entity_id:
        return "forward"
    if edge.tail.canonical_id == from_entity_id and edge.head.canonical_id == to_entity_id:
        return "reverse"
    return None


@dataclass
class PathBuildResult:
    """Every path assembled, plus what the build cost and what it skipped."""

    paths: list[OrderedPath] = field(default_factory=list)
    first_hop_edges: list[GraphEdge] = field(default_factory=list)
    second_hop_edges: list[GraphEdge] = field(default_factory=list)
    rows_examined: int = 0
    tool_calls: int = 0
    skipped_intermediates: int = 0
    truncated: bool = False
    error_codes: list[str] = field(default_factory=list)


def bounded_intermediates(
    edges: list[GraphEdge], *, cap: int, per_relation_cap: int = DEFAULT_PER_RELATION_CAP
) -> list[GraphEntity]:
    """Pick a stable, bounded set of middle entities to expand.

    High-degree fan-out is the main cost and noise source, so the cap applies
    before any second-hop query runs. Ordering is deterministic.
    """
    by_id: dict[str, GraphEntity] = {}
    per_relation: dict[str, int] = {}
    ordered = sorted(
        edges, key=lambda item: (item.relation, item.tail.canonical_id, item.edge_id)
    )
    for edge in ordered:
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


def assemble_paths(
    first_hop: Iterable[GraphEdge],
    second_hop_by_intermediate: dict[str, list[tuple[GraphEdge, str]]],
    *,
    provider_id: str,
    snapshot_id: str,
    max_paths: int,
    first_trace_ids: dict[str, str] | None = None,
) -> list[OrderedPath]:
    """Join bounded hops into ordered paths, then deduplicate stably.

    ``second_hop_by_intermediate`` maps an intermediate canonical id to
    ``(edge, query_trace_id)`` pairs. Only edges that actually connect to the
    intermediate are joined; a disconnected pair is dropped, never stitched.
    """
    first_trace_ids = first_trace_ids or {}
    seen: set[str] = set()
    paths: list[OrderedPath] = []
    for first in first_hop:
        start_id = first.head.canonical_id
        intermediate_id = first.tail.canonical_id
        first_direction = traversal_direction(
            first, from_entity_id=start_id, to_entity_id=intermediate_id
        )
        if first_direction is None:
            continue
        for edge, trace_id in second_hop_by_intermediate.get(intermediate_id, []):
            # Continuity is checked against the real edge endpoints, never
            # against anything the caller asserted.
            if edge.head.canonical_id == intermediate_id:
                second_direction = "forward"
                end_id = edge.tail.canonical_id
            elif edge.tail.canonical_id == intermediate_id:
                second_direction = "reverse"
                end_id = edge.head.canonical_id
            else:
                continue
            if edge.edge_id == first.edge_id:
                # The same edge twice is a loop, not two hops.
                continue
            if end_id == start_id or end_id == intermediate_id:
                # Walking back to the seed or to the middle is not progress.
                continue
            steps = [
                OrderedPathStep(
                    position=0,
                    edge_id=first.edge_id,
                    traversal_direction=first_direction,  # type: ignore[arg-type]
                    from_entity_id=start_id,
                    relation=first.relation,
                    to_entity_id=intermediate_id,
                    query_trace_id=first_trace_ids.get(first.edge_id, ""),
                ),
                OrderedPathStep(
                    position=1,
                    edge_id=edge.edge_id,
                    traversal_direction=second_direction,  # type: ignore[arg-type]
                    from_entity_id=intermediate_id,
                    to_entity_id=end_id,
                    relation=edge.relation,
                    query_trace_id=trace_id,
                ),
            ]
            digest = path_digest(
                snapshot_id=snapshot_id,
                steps=[
                    (item.edge_id, item.traversal_direction, item.relation) for item in steps
                ],
            )
            if digest in seen:
                continue
            seen.add(digest)
            paths.append(
                OrderedPath(
                    path_id=f"opath.{digest[:24]}",
                    provider_id=provider_id,
                    snapshot_id=snapshot_id,
                    start_entity_id=start_id,
                    end_entity_id=end_id,
                    steps=steps,
                    intermediate_entity_ids=[intermediate_id],
                    source_edge_ids=[first.edge_id, edge.edge_id],
                    path_hash=digest,
                    query_trace_ids=[item.query_trace_id for item in steps],
                )
            )
            if len(paths) >= max_paths:
                return sorted(paths, key=lambda item: item.path_hash)
    return sorted(paths, key=lambda item: item.path_hash)


class BoundedPathBuilder:
    """Executes bounded hops through a provider and assembles ordered paths.

    The provider is the only thing that touches data. This class never builds
    SQL, never reads a file path, and never accepts a tool name from a caller.
    """

    def __init__(
        self,
        provider: KnowledgeGraphProvider,
        *,
        max_intermediates: int = DEFAULT_MAX_INTERMEDIATES,
        per_relation_cap: int = DEFAULT_PER_RELATION_CAP,
    ) -> None:
        self.provider = provider
        self.max_intermediates = max(1, int(max_intermediates))
        self.per_relation_cap = max(1, int(per_relation_cap))

    def build(
        self,
        seed: GraphEntity,
        *,
        first_relations: list[str] | None = None,
        first_target_types: list[str] | None = None,
        second_relations: list[str] | None = None,
        second_target_types: list[str] | None = None,
        first_limit: int = 50,
        second_limit: int = 50,
        max_paths: int = 20,
        intermediates: list[GraphEntity] | None = None,
    ) -> PathBuildResult:
        """First hop from the seed, then one filtered hop per intermediate.

        ``intermediates`` lets a caller (or a later Planner) supply the exact
        middle entities to expand. When omitted the builder derives them from
        the first hop, capped by ``max_intermediates``.
        """
        result = PathBuildResult()
        first = self._neighbors(
            seed,
            direction="outgoing",
            relations=list(first_relations or []),
            target_types=list(first_target_types or []),
            limit=first_limit,
        )
        result.first_hop_edges = list(first.edges)
        first_trace_id = _query_trace_id(first)
        result.rows_examined += int(first.trace.rows_examined or 0)
        result.tool_calls += 1
        result.truncated = result.truncated or bool(first.truncated)
        if not first.edges:
            return result

        # One trace id covers the whole first-hop query; each edge inherits it
        # so a replay can find the query that produced it.
        first_trace_ids = {edge.edge_id: first_trace_id for edge in first.edges}
        if intermediates is None:
            chosen = bounded_intermediates(
                first.edges,
                cap=self.max_intermediates,
                per_relation_cap=self.per_relation_cap,
            )
        else:
            chosen = list(intermediates)
            result.skipped_intermediates = max(
                0,
                len(bounded_intermediates(first.edges, cap=10**9, per_relation_cap=10**9))
                - len(chosen),
            )

        second_by_intermediate: dict[str, list[tuple[GraphEdge, str]]] = {}
        for entity in chosen:
            expanded = self._neighbors(
                entity,
                direction="both",
                relations=list(second_relations or []),
                target_types=list(second_target_types or []),
                limit=second_limit,
            )
            result.tool_calls += 1
            result.rows_examined += int(expanded.trace.rows_examined or 0)
            result.truncated = result.truncated or bool(expanded.truncated)
            result.second_hop_edges.extend(expanded.edges)
            trace_id = _query_trace_id(expanded)
            second_by_intermediate[entity.canonical_id] = [
                (edge, trace_id) for edge in expanded.edges
            ]

        snapshot_id = str(getattr(first.snapshot, "data_snapshot_id", "") or "")
        result.paths = assemble_paths(
            first.edges,
            second_by_intermediate,
            provider_id=self.provider.provider_id,
            snapshot_id=snapshot_id,
            max_paths=max_paths,
            first_trace_ids=first_trace_ids,
        )
        return result

    def _neighbors(
        self,
        entity: GraphEntity,
        *,
        direction: str,
        relations: list[str],
        target_types: list[str],
        limit: int,
    ) -> Any:
        return self.provider.neighbors(
            NeighborQuery(
                entity=entity,
                direction=direction,  # type: ignore[arg-type]
                relations=relations,
                target_types=target_types,
                limit=limit,
            )
        )


def _query_trace_id(result: Any) -> str:
    return str(getattr(getattr(result, "trace", None), "query_id", "") or "")
