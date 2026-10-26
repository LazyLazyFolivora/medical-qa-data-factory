"""Shortcut detection: is there a shorter path that answers the same question?

Step 7 of the real-generation-agent plan. A three-hop question whose answer
is already reachable in two hops is not a three-hop question. Publishing it
as one would teach a selector that a long chain is needed when it is not.

Two checks:

* **Leave-one-hop-out.** Drop each hop and see whether the remaining chain
  still reaches the same answer set. A chain that survives losing a hop is
  carrying a redundant step.
* **Shorter path.** Search the snapshot for a chain of fewer hops with the
  same relation sequence that reaches the same answer.

Both are computed against the complete, fixed snapshot, never against the
edges that happened to be collected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, Sequence

from pydantic import Field

from ..domain.common import ContractModel, UtcDateTime
from .logical_query import LogicalDirection, LogicalQuery, QueryAnswer


class ShortcutKind(str, Enum):
    """Why a query was considered to have a shortcut."""

    LEAVE_ONE_HOP_OUT = "leave_one_hop_out"
    SHORTER_PATH = "shorter_path"


class ShortcutFinding(ContractModel):
    """One shortcut that a query has."""

    kind: ShortcutKind
    #: The hop positions that can be dropped.
    droppable_hop_positions: list[int] = Field(default_factory=list)
    #: The shorter chain that reaches the same answer.
    shorter_steps: list[str] = Field(default_factory=list)
    #: The answer set both chains reach.
    answer_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


class ShortcutReport(ContractModel):
    """Every shortcut found for one query."""

    query_id: str = Field(min_length=1)
    has_shortcut: bool = False
    findings: list[ShortcutFinding] = Field(default_factory=list)
    #: The shortest chain, in hops, that reaches the answer.
    shortest_hop_count: int | None = None
    checked_at: UtcDateTime


class ShortcutEvaluator(Protocol):
    """The evaluator that can answer a shortened chain."""

    def answer(self, query: LogicalQuery) -> QueryAnswer: ...


@dataclass
class _Edge:
    """A minimal edge view for the shortcut search."""

    head: str
    relation: str
    tail: str


def _relations(edges: Sequence[Any]) -> list[_Edge]:
    result: list[_Edge] = []
    for edge in edges:
        head = getattr(getattr(edge, "head", None), "canonical_id", None)
        tail = getattr(getattr(edge, "tail", None), "canonical_id", None)
        relation = getattr(edge, "relation", None)
        if head and tail and relation:
            result.append(_Edge(str(head), str(relation), str(tail)))
    return result


def check_shortcuts(
    query: LogicalQuery,
    *,
    edges: Sequence[Any],
    evaluator: ShortcutEvaluator,
    answer: QueryAnswer | None = None,
) -> ShortcutReport:
    """Look for a shorter chain that reaches the same answer."""
    from ..domain.common import utc_now

    report = ShortcutReport(
        query_id=query.query_id,
        shortest_hop_count=len(query.steps),
        checked_at=utc_now(),
    )
    if answer is None:
        answer = evaluator.answer(query)
    if answer.status not in ("ok", "ambiguous") or not answer.answer_ids:
        # A query that does not resolve has no answer to shortcut.
        return report
    target = set(answer.answer_ids)
    edge_list = _relations(edges)

    # ---- leave-one-hop-out ---------------------------------------------
    for position in range(len(query.steps)):
        remaining = [
            step for step in query.steps if step.position != position
        ]
        if not remaining:
            continue
        reached = _reach(
            edge_list,
            start=query.start_entity_id,
            steps=[
                (step.relation, step.direction.value) for step in remaining
            ],
        )
        if target and target <= reached:
            report.findings.append(
                ShortcutFinding(
                    kind=ShortcutKind.LEAVE_ONE_HOP_OUT,
                    droppable_hop_positions=[position],
                    answer_ids=sorted(target),
                    rationale=(
                        f"dropping hop {position} still reaches the same answer"
                    ),
                )
            )

    # ---- shorter path --------------------------------------------------
    shortest = _shortest_chain(
        edge_list,
        start=query.start_entity_id,
        relations=[step.relation for step in query.steps],
        target=target,
        max_hops=len(query.steps) - 1,
    )
    if shortest is not None:
        report.findings.append(
            ShortcutFinding(
                kind=ShortcutKind.SHORTER_PATH,
                shorter_steps=shortest,
                answer_ids=sorted(target),
                rationale=(
                    f"a chain of {len(shortest)} hops reaches the same answer"
                ),
            )
        )
        report.shortest_hop_count = min(
            report.shortest_hop_count or len(shortest), len(shortest)
        )
    report.has_shortcut = bool(report.findings)
    return report


def _reach(
    edges: Sequence[_Edge],
    *,
    start: str,
    steps: Sequence[tuple[str, str]],
) -> set[str]:
    """The entities reachable by following the given relation sequence."""
    current: set[str] = {start}
    for relation, direction in steps:
        nxt: set[str] = set()
        for edge in edges:
            if edge.relation != relation:
                continue
            if direction == LogicalDirection.FORWARD.value:
                if edge.head in current:
                    nxt.add(edge.tail)
            else:
                if edge.tail in current:
                    nxt.add(edge.head)
        current = nxt
        if not current:
            break
    return current


def _shortest_chain(
    edges: Sequence[_Edge],
    *,
    start: str,
    relations: Sequence[str],
    target: set[str],
    max_hops: int,
) -> list[str] | None:
    """The shortest chain of relations that reaches the target.

    The chain keeps the relation order where it can, but the point of the
    check is that a shorter chain need not follow the same relations.
    """
    if max_hops < 1 or not target:
        return None
    # Breadth-first over the reachable set, one hop at a time.
    frontier: dict[str, list[str]] = {start: []}
    visited: set[str] = {start}
    for _ in range(max_hops):
        nxt: dict[str, list[str]] = {}
        for entity, chain in frontier.items():
            for edge in edges:
                # Only an edge that is actually incident to the current
                # entity may extend the chain. Otherwise an edge between two
                # unrelated entities would look like a one-hop shortcut.
                for neighbour, incident in (
                    (edge.tail, edge.head),
                    (edge.head, edge.tail),
                ):
                    if incident != entity:
                        continue
                    if neighbour in visited:
                        continue
                    new_chain = [*chain, edge.relation]
                    if neighbour in target:
                        return new_chain
                    nxt[neighbour] = new_chain
        visited.update(nxt)
        frontier = nxt
        if not frontier:
            break
    return None