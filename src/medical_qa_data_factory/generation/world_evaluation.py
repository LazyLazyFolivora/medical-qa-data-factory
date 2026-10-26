"""Execute logical queries against a complete, pinned graph provider.

Unlike candidate discovery, this evaluator traverses every page and every
bridge reachable under the query.  Unsupported semantics fail closed instead
of being silently ignored.
"""
from __future__ import annotations

from ..domain.common import utc_now
from ..knowledge_graph.contracts import GraphEdge, GraphEntity, NeighborQuery
from ..knowledge_graph.errors import GraphDataIntegrityError, KnowledgeGraphError
from ..knowledge_graph.ports import KnowledgeGraphProvider
from .logical_query import (
    Aggregation,
    LogicalDirection,
    LogicalQuery,
    QueryConstraint,
    QueryAnswer,
)


class PrimeKGWorldEvaluator:
    """Provider-backed evaluator for a frozen PrimeKG snapshot."""

    def __init__(
        self,
        provider: KnowledgeGraphProvider,
        *,
        page_size: int = 500,
        max_answers: int = 10_000,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        if max_answers < 1:
            raise ValueError("max_answers must be positive")
        self.provider = provider
        self.page_size = page_size
        self.max_answers = max_answers

    def answer(self, query: LogicalQuery) -> QueryAnswer:
        snapshot = self.provider.snapshot()
        if query.snapshot_id != snapshot.data_snapshot_id:
            return self._incomplete(
                query,
                "query snapshot does not match the provider validation snapshot",
            )
        unsupported = self._unsupported_semantics(query)
        if unsupported:
            return self._incomplete(
                query, f"unsupported query semantics: {', '.join(unsupported)}"
            )

        try:
            start = self._entity_from_id(query.start_entity_id)
            current = {start.canonical_id: start}
        except ValueError as exc:
            return self._incomplete(query, str(exc))

        rows_examined = 0
        try:
            for step in query.steps:
                next_entities: dict[str, GraphEntity] = {}
                for entity in sorted(
                    current.values(), key=lambda item: item.canonical_id
                ):
                    edges, examined = self._all_neighbors(
                        entity,
                        relation=step.relation,
                        direction=step.direction,
                        target_entity_type=step.target_entity_type,
                    )
                    rows_examined += examined
                    for edge in edges:
                        target = self._walk(edge, entity, step.direction)
                        if target is not None:
                            next_entities[target.canonical_id] = target
                current = next_entities
                if not current:
                    break

            for constraint in query.constraints:
                retained: dict[str, GraphEntity] = {}
                for entity in sorted(
                    current.values(), key=lambda item: item.canonical_id
                ):
                    satisfied, examined = self._satisfies_constraint(
                        entity, constraint
                    )
                    rows_examined += examined
                    if satisfied:
                        retained[entity.canonical_id] = entity
                current = retained
                if not current:
                    break
        except KnowledgeGraphError as exc:
            return QueryAnswer(
                query_id=query.query_id,
                status="incomplete",
                failure_reason=f"validation provider failure: {exc}",
                rows_examined=rows_examined,
                evaluated_at=utc_now(),
            )

        answer_ids = sorted(current)
        if len(answer_ids) > self.max_answers:
            return QueryAnswer(
                query_id=query.query_id,
                answer_ids=answer_ids[: self.max_answers],
                status="incomplete",
                truncated=True,
                failure_reason=(
                    f"world answer set exceeds the limit of {self.max_answers}"
                ),
                rows_examined=rows_examined,
                evaluated_at=utc_now(),
            )

        aggregate: int | None = None
        if query.aggregation is Aggregation.COUNT:
            aggregate = len(answer_ids)
        elif query.aggregation is Aggregation.EXISTS:
            aggregate = int(bool(answer_ids))

        status = "ok"
        failure_reason: str | None = None
        if query.expected_cardinality == "one" and len(answer_ids) > 1:
            status = "ambiguous"
            failure_reason = (
                f"the complete validation world contains {len(answer_ids)} answers"
            )
        elif not answer_ids and query.aggregation is not Aggregation.EXISTS:
            status = "no_answer"
            failure_reason = "the complete validation world contains no answer"

        return QueryAnswer(
            query_id=query.query_id,
            answer_ids=answer_ids,
            aggregate_value=aggregate,
            status=status,
            truncated=False,
            failure_reason=failure_reason,
            rows_examined=rows_examined,
            evaluated_at=utc_now(),
        )

    @staticmethod
    def _unsupported_semantics(query: LogicalQuery) -> list[str]:
        unsupported: list[str] = []
        if query.filters:
            unsupported.append("filters")
        if query.temporal_scope:
            unsupported.append("temporal_scope")
        if query.negation:
            unsupported.append("negation")
        return unsupported

    def _all_neighbors(
        self,
        entity: GraphEntity,
        *,
        relation: str,
        direction: LogicalDirection,
        target_entity_type: str | None,
    ) -> tuple[list[GraphEdge], int]:
        cursor: str | None = None
        edges: dict[str, GraphEdge] = {}
        rows_examined = 0
        while True:
            result = self.provider.neighbors(
                NeighborQuery(
                    entity=entity,
                    direction=(
                        "outgoing"
                        if direction is LogicalDirection.FORWARD
                        else "incoming"
                    ),
                    relations=[relation],
                    target_types=(
                        [target_entity_type] if target_entity_type else []
                    ),
                    limit=self.page_size,
                    cursor=cursor,
                )
            )
            expected_snapshot = self.provider.snapshot().data_snapshot_id
            if result.snapshot.data_snapshot_id != expected_snapshot:
                raise GraphDataIntegrityError(
                    "provider changed snapshot during validation"
                )
            expected_provider = self.provider.snapshot().provider_id
            if result.snapshot.provider_id != expected_provider:
                raise GraphDataIntegrityError(
                    "provider identity changed during validation"
                )
            rows_examined += result.trace.rows_examined or len(result.edges)
            for edge in result.edges:
                if (
                    edge.provider_id != expected_provider
                    or edge.head.provider_id != expected_provider
                    or edge.tail.provider_id != expected_provider
                ):
                    raise GraphDataIntegrityError(
                        "validation result contains an entity from another provider"
                    )
                edges[edge.edge_id] = edge
            if not result.truncated:
                break
            if not result.next_cursor or result.next_cursor == cursor:
                raise ValueError("provider returned an incomplete page without progress")
            cursor = result.next_cursor
        return list(edges.values()), rows_examined

    def _satisfies_constraint(
        self, entity: GraphEntity, constraint: QueryConstraint
    ) -> tuple[bool, int]:
        current = {entity.canonical_id: entity}
        rows_examined = 0
        for step in constraint.steps:
            next_entities: dict[str, GraphEntity] = {}
            for current_entity in sorted(
                current.values(), key=lambda item: item.canonical_id
            ):
                edges, examined = self._all_neighbors(
                    current_entity,
                    relation=step.relation,
                    direction=step.direction,
                    target_entity_type=step.target_entity_type,
                )
                rows_examined += examined
                for edge in edges:
                    target = self._walk(edge, current_entity, step.direction)
                    if target is not None:
                        next_entities[target.canonical_id] = target
            current = next_entities
            if not current:
                break
        return constraint.terminal_entity_id in current, rows_examined

    @staticmethod
    def _walk(
        edge: GraphEdge,
        current: GraphEntity,
        direction: LogicalDirection,
    ) -> GraphEntity | None:
        if direction is LogicalDirection.FORWARD:
            if edge.head.canonical_id == current.canonical_id:
                return edge.tail
            return None
        if edge.tail.canonical_id == current.canonical_id:
            return edge.head
        return None

    def _entity_from_id(self, canonical_id: str) -> GraphEntity:
        parts = canonical_id.split(":", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError(
                "start entity ID must contain provider, type and native ID"
            )
        provider_id, entity_type, native_id = parts
        expected_provider = self.provider.snapshot().provider_id
        if provider_id != expected_provider:
            raise ValueError(
                "start entity provider does not match the validation provider"
            )
        if native_id.startswith("name-sha256:"):
            raise ValueError(
                "name-derived entity IDs require explicit entity resolution"
            )
        return GraphEntity(
            provider_id=provider_id,
            native_id=native_id,
            canonical_id=canonical_id,
            name=canonical_id,
            entity_type=entity_type,
        )

    @staticmethod
    def _incomplete(query: LogicalQuery, reason: str) -> QueryAnswer:
        return QueryAnswer(
            query_id=query.query_id,
            status="incomplete",
            failure_reason=reason,
            evaluated_at=utc_now(),
        )
