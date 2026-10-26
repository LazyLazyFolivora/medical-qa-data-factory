"""The strongly typed logical query.

Step 7 of the real-generation-agent plan. A multi-hop question is not a bag of
edges and it is not a free-form dict. It is an ordered, typed query with a
named answer variable.

The type exists to make two specific mistakes impossible:

* A query that silently means something different from the path it came from.
  The query carries the probe's dependency digest, and a mismatch with the
  probe is an error, not a warning.
* A query whose answer is decided by the evidence that was used to build it.
  The evaluator runs against the complete fixed snapshot, so a question whose
  answer is only reachable through the generation input fails.
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Protocol, Sequence

from pydantic import Field, model_validator

from ..domain.common import ContractModel, UtcDateTime
from .probe import AnswerabilityProbe, ProbeExecution, ProbeStatus


#: The logical query contract version.
LOGICAL_QUERY_SCHEMA_VERSION = "logical-query-v1"


class LogicalDirection(str, Enum):
    """How a step is walked."""

    FORWARD = "forward"
    REVERSE = "reverse"


class Aggregation(str, Enum):
    """How the answer set is aggregated."""

    NONE = "none"
    COUNT = "count"
    EXISTS = "exists"
    SET = "set"


class QueryStep(ContractModel):
    """One typed hop of a logical query."""

    position: int = Field(ge=0)
    relation: str = Field(min_length=1)
    direction: LogicalDirection
    #: The entity type the hop arrives at, when the question constrains it.
    target_entity_type: str | None = None
    #: The entity the hop starts at. Filled in by the evaluator.
    from_entity_id: str | None = None
    #: The entity the hop ends at, when the path pinned it.
    to_entity_id: str | None = None


class QueryConstraint(ContractModel):
    """A path predicate that an answer candidate must satisfy.

    Constraints start from the answer variable and end at a fixed entity. They
    express intersections such as "targeted by Drug A" AND "associated with
    Disease D" without pinning the candidate answer itself.
    """

    constraint_id: str = Field(min_length=1)
    steps: list[QueryStep] = Field(min_length=1)
    terminal_entity_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def positions_are_contiguous(self) -> "QueryConstraint":
        if [step.position for step in self.steps] != list(range(len(self.steps))):
            raise ValueError("constraint step positions must be contiguous from zero")
        return self


class LogicalQuery(ContractModel):
    """An ordered, typed multi-hop query with a named answer variable."""

    schema_version: str = LOGICAL_QUERY_SCHEMA_VERSION
    query_id: str = Field(min_length=1)
    #: The path the query came from.
    path_id: str = Field(min_length=1)
    path_version: str = Field(min_length=1)
    #: The probe the query was promoted from. The digests must agree.
    probe_id: str = Field(min_length=1)
    probe_digest: str = Field(min_length=1)
    #: The seed entity the query starts at.
    start_entity_id: str = Field(min_length=1)
    steps: list[QueryStep] = Field(min_length=1)
    #: Additional path predicates evaluated from each answer candidate.
    constraints: list[QueryConstraint] = Field(default_factory=list)
    #: The answer variable.
    answer_variable: str = Field(default="answer", min_length=1)
    answer_entity_type: str | None = None
    #: The expected number of answers.
    expected_cardinality: str = Field(default="any", pattern="^(one|many|any)$")
    aggregation: Aggregation = Aggregation.NONE
    #: The filters the question carries.
    filters: dict[str, Any] = Field(default_factory=dict)
    negation: bool = False
    temporal_scope: str | None = None
    snapshot_id: str = Field(min_length=1)
    #: The digest of the query's own content. It changes when the query's
    #: meaning changes.
    dependency_digest: str = Field(min_length=1)
    created_at: UtcDateTime

    @model_validator(mode="after")
    def positions_are_contiguous(self) -> "LogicalQuery":
        if [step.position for step in self.steps] != list(range(len(self.steps))):
            raise ValueError("query step positions must be contiguous from zero")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        """The part of the query that defines its meaning."""
        return {
            "start_entity_id": self.start_entity_id,
            "steps": [
                {
                    "position": step.position,
                    "relation": step.relation,
                    "direction": step.direction.value,
                    "target_entity_type": step.target_entity_type,
                }
                for step in self.steps
            ],
            "constraints": [
                {
                    "constraint_id": constraint.constraint_id,
                    "steps": [
                        {
                            "position": step.position,
                            "relation": step.relation,
                            "direction": step.direction.value,
                            "target_entity_type": step.target_entity_type,
                        }
                        for step in constraint.steps
                    ],
                    "terminal_entity_id": constraint.terminal_entity_id,
                }
                for constraint in self.constraints
            ],
            "answer_variable": self.answer_variable,
            "answer_entity_type": self.answer_entity_type,
            "expected_cardinality": self.expected_cardinality,
            "aggregation": self.aggregation.value,
            "filters": self.filters,
            "negation": self.negation,
            "temporal_scope": self.temporal_scope,
        }

    def digest(self) -> str:
        payload = {
            "path_id": self.path_id,
            "path_version": self.path_version,
            "snapshot_id": self.snapshot_id,
            **self.canonical_payload(),
        }
        text = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LogicalQueryError(RuntimeError):
    """The query could not be built or evaluated."""

    code = "logical_query_error"


def promote_probe_to_query(
    probe: AnswerabilityProbe,
    *,
    query_id: str,
    answer_entity_type: str | None = None,
    expected_cardinality: str | None = None,
    filters: dict[str, Any] | None = None,
    aggregation: Aggregation = Aggregation.NONE,
) -> LogicalQuery:
    """Promote a probe into a full logical query.

    The probe's digest is carried over unchanged, so the query can always be
    traced back to the probe that justified it. The query's own digest is
    computed from its content, because a query may constrain the answer
    further than the probe did.

    What is checked is the *continuity* of the chain: the query's steps must
    be the probe's steps. A query cannot silently answer a different
    question than the one that was judged sufficient.
    """
    if probe.dependency_digest != probe.digest():
        raise LogicalQueryError(
            "the probe's dependency digest does not match its own content"
        )
    steps = [
        QueryStep(
            position=step.position,
            relation=step.relation,
            direction=LogicalDirection(step.direction),
            target_entity_type=step.target_entity_type,
            from_entity_id=step.from_entity_id,
            to_entity_id=step.to_entity_id,
        )
        for step in probe.steps
    ]
    query = LogicalQuery(
        query_id=query_id,
        path_id=probe.path_id,
        path_version=probe.path_version,
        probe_id=probe.probe_id,
        probe_digest=probe.dependency_digest,
        start_entity_id=probe.start_entity_id,
        steps=steps,
        answer_entity_type=(
            answer_entity_type
            if answer_entity_type is not None
            else probe.answer_entity_type
        ),
        expected_cardinality=(
            expected_cardinality
            if expected_cardinality is not None
            else probe.expected_cardinality
        ),
        aggregation=aggregation,
        filters=dict(filters or {}),
        snapshot_id=probe.snapshot_id,
        dependency_digest="pending",
        created_at=probe.created_at,
    )
    if query.probe_digest != probe.dependency_digest:
        raise LogicalQueryError(
            "the promoted query does not preserve the probe's digest"
        )
    if _probe_shape(probe) != _query_shape(query):
        raise LogicalQueryError(
            "the promoted query's steps differ from the probe's steps"
        )
    return query.model_copy(
        update={"dependency_digest": query.digest()}
    )


def _probe_shape(probe: AnswerabilityProbe) -> list[tuple[Any, ...]]:
    """The ordered relation and direction sequence of a probe."""
    return [
        (step.position, step.relation, str(step.direction))
        for step in probe.steps
    ]


def _query_shape(query: LogicalQuery) -> list[tuple[Any, ...]]:
    """The ordered relation and direction sequence of a query."""
    return [
        (step.position, step.relation, step.direction.value)
        for step in query.steps
    ]


class QueryEvaluator(Protocol):
    """The deterministic evaluator that answers a logical query."""

    def answer(self, query: LogicalQuery) -> QueryAnswer: ...


class QueryAnswer(ContractModel):
    """The authoritative answer of a logical query."""

    query_id: str = Field(min_length=1)
    #: The canonical answer set, sorted and deduplicated.
    answer_ids: list[str] = Field(default_factory=list)
    #: The aggregate value, when the query aggregates.
    aggregate_value: int | None = None
    status: str = Field(default="ok")
    truncated: bool = False
    failure_reason: str | None = None
    rows_examined: int = Field(default=0, ge=0)
    evaluated_at: UtcDateTime


class SnapshotQueryEvaluator:
    """Answers a logical query against a complete, fixed snapshot.

    The search domain is the whole snapshot, never the subset that produced
    the path. Restricting it would let the generation input prove the
    generation output.
    """

    def __init__(
        self,
        edges: Sequence[Any],
        *,
        snapshot_id: str,
        max_answers: int = 50,
    ) -> None:
        self.edges = list(edges)
        self.snapshot_id = snapshot_id
        self.max_answers = max_answers

    def answer(self, query: LogicalQuery) -> QueryAnswer:
        from ..domain.common import utc_now

        if query.snapshot_id != self.snapshot_id:
            return QueryAnswer(
                query_id=query.query_id,
                status="incomplete",
                failure_reason=(
                    f"the query targets snapshot {query.snapshot_id!r} but the "
                    f"evaluator serves {self.snapshot_id!r}"
                ),
                evaluated_at=utc_now(),
            )
        current: set[str] = {query.start_entity_id}
        rows = 0
        for step in query.steps:
            nxt: set[str] = set()
            for edge in self.edges:
                rows += 1
                head = _canonical_id(getattr(edge, "head", None))
                tail = _canonical_id(getattr(edge, "tail", None))
                if not head or not tail:
                    continue
                if getattr(edge, "relation", None) != step.relation:
                    continue
                if step.direction is LogicalDirection.FORWARD:
                    if head in current:
                        nxt.add(tail)
                else:
                    if tail in current:
                        nxt.add(head)
            if step.target_entity_type:
                nxt = {
                    item
                    for item in nxt
                    if _entity_type(self.edges, item) == step.target_entity_type
                }
            current = nxt
            if not current:
                break
        truncated = len(current) > self.max_answers
        answer_ids = sorted(current)
        if truncated:
            answer_ids = answer_ids[: self.max_answers]
        aggregate: int | None = None
        if query.aggregation is Aggregation.COUNT:
            aggregate = len(answer_ids)
        if query.aggregation is Aggregation.EXISTS:
            aggregate = 1 if answer_ids else 0
        if query.negation:
            answer_ids = []
        status = "ok"
        failure: str | None = None
        if truncated:
            status = "incomplete"
            failure = (
                f"the answer set was truncated at {self.max_answers} entities"
            )
        elif (
            query.expected_cardinality == "one" and len(answer_ids) > 1
        ):
            status = "ambiguous"
            failure = (
                f"the query expected one answer but the graph offers "
                f"{len(answer_ids)}"
            )
        elif not answer_ids and query.aggregation is not Aggregation.EXISTS:
            status = "no_answer"
            failure = "the query found no answer on the fixed snapshot"
        return QueryAnswer(
            query_id=query.query_id,
            answer_ids=answer_ids,
            aggregate_value=aggregate,
            status=status,
            truncated=truncated,
            failure_reason=failure,
            rows_examined=rows,
            evaluated_at=utc_now(),
        )


def _canonical_id(entity: Any) -> str | None:
    if entity is None:
        return None
    value = getattr(entity, "canonical_id", None)
    return str(value) if value else None


def _entity_type(edges: Sequence[Any], canonical_id: str) -> str | None:
    for edge in edges:
        for side in ("head", "tail"):
            entity = getattr(edge, side, None)
            if _canonical_id(entity) == canonical_id:
                return getattr(entity, "entity_type", None)
    return None
