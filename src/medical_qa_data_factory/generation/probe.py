"""The answerability probe: a deterministic question of the graph.

Step 6 of the real-generation-agent plan. The probe exists to break a
circular dependency.

The naive order would be: build a LogicalQuery, then decide whether the
evidence can support it. But a LogicalQuery needs an answer, and the answer
comes from the query, so "is the evidence sufficient?" would depend on the
very thing the evidence is supposed to determine.

The probe breaks that. It is a query skeleton built from the current path:
the start entity, the ordered relation and direction sequence, the target
entity type and the expected cardinality. It contains no natural-language
question and no answer. The sufficiency judge runs the probe against the
complete, fixed snapshot and gets an answer set it can check. Only then does
the question planner promote the probe into a full LogicalQuery.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, Sequence

from pydantic import Field, model_validator

from ..domain.common import ContractModel, UtcDateTime, utc_now
from .contracts import EvidencePath, EvidencePathStep


#: The probe contract version.
PROBE_SCHEMA_VERSION = "answerability-probe-v1"


class ProbeStatus(str, Enum):
    """What the probe's execution produced."""

    OK = "ok"
    AMBIGUOUS = "ambiguous"
    INCOMPLETE = "incomplete"
    NO_ANSWER = "no_answer"
    NOT_RUN = "not_run"


class ProbeStep(ContractModel):
    """One ordered hop of the probe."""

    position: int = Field(ge=0)
    relation: str = Field(min_length=1)
    direction: str = Field(pattern="^(forward|reverse)$")
    #: The entity type the hop must arrive at, when one is known.
    target_entity_type: str | None = None
    from_entity_id: str = Field(min_length=1)
    to_entity_id: str | None = None


class AnswerabilityProbe(ContractModel):
    """A question of the graph with no answer attached.

    The probe is built from a path, never from a natural-language question.
    It carries the digest of the inputs it was built from, so a change to the
    path invalidates the probe and everything derived from it.
    """

    schema_version: str = PROBE_SCHEMA_VERSION
    probe_id: str = Field(min_length=1)
    path_id: str = Field(min_length=1)
    path_version: str = Field(min_length=1)
    start_entity_id: str = Field(min_length=1)
    steps: list[ProbeStep] = Field(min_length=1)
    #: The entity type the answer must have.
    answer_entity_type: str | None = None
    #: The expected number of answers: "one", "many" or "any".
    expected_cardinality: str = Field(default="any", pattern="^(one|many|any)$")
    snapshot_id: str = Field(min_length=1)
    #: The digest of everything the probe depends on.
    dependency_digest: str = Field(min_length=1)
    created_at: UtcDateTime

    @model_validator(mode="after")
    def positions_are_contiguous(self) -> "AnswerabilityProbe":
        if [step.position for step in self.steps] != list(range(len(self.steps))):
            raise ValueError("probe step positions must be contiguous from zero")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        """The part of the probe that defines its meaning."""
        return {
            "start_entity_id": self.start_entity_id,
            "steps": [
                {
                    "position": step.position,
                    "relation": step.relation,
                    "direction": step.direction,
                    "target_entity_type": step.target_entity_type,
                }
                for step in self.steps
            ],
            "answer_entity_type": self.answer_entity_type,
            "expected_cardinality": self.expected_cardinality,
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


class ProbeExecution(ContractModel):
    """The result of running a probe."""

    probe_id: str = Field(min_length=1)
    status: ProbeStatus = ProbeStatus.NOT_RUN
    #: The canonical answer set, sorted and deduplicated.
    answer_ids: list[str] = Field(default_factory=list)
    #: Whether the result was truncated by a safety limit.
    truncated: bool = False
    #: Why the probe did not produce a clean answer.
    failure_reason: str | None = None
    rows_examined: int = Field(default=0, ge=0)
    executed_at: UtcDateTime | None = None


class GraphEvaluator(Protocol):
    """The deterministic evaluator that runs a probe.

    It must run against the complete, fixed snapshot. It must not restrict
    its search to the evidence that was used to build the probe: that would
    let the generation input prove the generation output.
    """

    def evaluate(self, probe: AnswerabilityProbe) -> ProbeExecution: ...


@dataclass
class _StepView:
    """A step resolved to concrete entities."""

    relation: str
    direction: str
    target_entity_type: str | None
    to_entity_id: str | None


def build_probe(
    path: EvidencePath,
    *,
    answer_entity_type: str | None = None,
    expected_cardinality: str = "any",
) -> AnswerabilityProbe:
    """Build the probe for a path.

    The probe mirrors the path's ordered relation and direction sequence. It
    carries no answer and no question text.
    """
    steps: list[ProbeStep] = []
    for step in path.steps:
        steps.append(
            ProbeStep(
                position=step.position,
                relation=step.relation,
                direction=step.traversal_direction,
                target_entity_type=None,
                from_entity_id=step.from_entity_id,
                to_entity_id=step.to_entity_id,
            )
        )
    probe_id = f"probe.{path.path_id}"
    payload = {
        "path_id": path.path_id,
        "path_version": path.path_version,
        "snapshot_id": path.snapshot_id,
        "start_entity_id": path.start_entity_id,
        "steps": [
            {
                "position": step.position,
                "relation": step.relation,
                "direction": step.direction,
                "target_entity_type": step.target_entity_type,
            }
            for step in steps
        ],
        "answer_entity_type": answer_entity_type,
        "expected_cardinality": expected_cardinality,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return AnswerabilityProbe(
        probe_id=probe_id,
        path_id=path.path_id,
        path_version=path.path_version,
        start_entity_id=path.start_entity_id,
        steps=steps,
        answer_entity_type=answer_entity_type,
        expected_cardinality=expected_cardinality,
        snapshot_id=path.snapshot_id,
        dependency_digest=digest,
        created_at=utc_now(),
    )


def probe_from_evidence_path(path: Any) -> AnswerabilityProbe:
    """Build a probe from any object shaped like an evidence path."""
    return build_probe(path)


class SnapshotGraphEvaluator:
    """A deterministic probe evaluator over an in-memory edge set.

    The edge set is the complete snapshot, not the subset that produced the
    path. A probe whose answer exists only in the generation input therefore
    fails, which is the point.
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

    def evaluate(self, probe: AnswerabilityProbe) -> ProbeExecution:
        from ..domain.common import utc_now

        if probe.snapshot_id != self.snapshot_id:
            return ProbeExecution(
                probe_id=probe.probe_id,
                status=ProbeStatus.INCOMPLETE,
                failure_reason=(
                    f"the probe targets snapshot {probe.snapshot_id!r} but the "
                    f"evaluator serves {self.snapshot_id!r}"
                ),
                executed_at=utc_now(),
            )
        # The reachable entity set after each hop.
        current: set[str] = {probe.start_entity_id}
        rows = 0
        for step in probe.steps:
            nxt: set[str] = set()
            for edge in self.edges:
                rows += 1
                head = _canonical_id(getattr(edge, "head", None))
                tail = _canonical_id(getattr(edge, "tail", None))
                if not head or not tail:
                    continue
                if getattr(edge, "relation", None) != step.relation:
                    continue
                if step.direction == "forward":
                    if head in current:
                        nxt.add(tail)
                else:
                    if tail in current:
                        nxt.add(head)
            current = nxt
            if not current:
                break
        truncated = len(current) > self.max_answers
        answer_ids = sorted(current)
        if truncated:
            answer_ids = answer_ids[: self.max_answers]
        if not answer_ids:
            return ProbeExecution(
                probe_id=probe.probe_id,
                status=ProbeStatus.NO_ANSWER,
                rows_examined=rows,
                executed_at=utc_now(),
            )
        status = ProbeStatus.OK
        failure: str | None = None
        if truncated:
            status = ProbeStatus.INCOMPLETE
            failure = (
                f"the answer set was truncated at {self.max_answers} entities"
            )
        elif (
            probe.expected_cardinality == "one" and len(answer_ids) > 1
        ):
            status = ProbeStatus.AMBIGUOUS
            failure = (
                f"the probe expected one answer but the graph offers "
                f"{len(answer_ids)}"
            )
        return ProbeExecution(
            probe_id=probe.probe_id,
            status=status,
            answer_ids=answer_ids,
            truncated=truncated,
            failure_reason=failure,
            rows_examined=rows,
            executed_at=utc_now(),
        )


def _canonical_id(entity: Any) -> str | None:
    if entity is None:
        return None
    value = getattr(entity, "canonical_id", None)
    return str(value) if value else None