"""The executor: the only component that runs a tool.

Step 5 of the real-generation-agent plan. The executor accepts a
``SealedAction`` and nothing else. It cannot widen a request, cannot invent a
tool, and cannot decide what to do next. Its output is a tool observation that
feeds back into the planner's context.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..domain.common import new_id, utc_now
from .actions import SealedAction
from .contracts import ExplorationActionType, ToolObservation


class ToolCallError(RuntimeError):
    """A tool call failed in a way the executor can report."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class KGExecutionPort(Protocol):
    """The bounded graph queries the executor may run."""

    def neighbors(
        self, request: dict[str, Any]
    ) -> tuple[list[Any], str]: ...


class LiteratureExecutionPort(Protocol):
    """The literature search the executor may run."""

    def search(
        self, request: dict[str, Any]
    ) -> tuple[list[Any], str, str | None]: ...


@dataclass
class ExecutionOutcome:
    """What one execution produced, and the observation that records it."""

    #: Filled in once the call has finished and the observation is recorded.
    observation: ToolObservation | None = None
    new_evidence_ids: list[str] = field(default_factory=list)
    new_path_ids: list[str] = field(default_factory=list)
    discovered_entity_ids: list[str] = field(default_factory=list)
    rows_examined: int = 0
    truncated: bool = False
    error_code: str | None = None
    retryable: bool = False

    @property
    def succeeded(self) -> bool:
        return self.error_code is None


class Executor:
    """Runs sealed actions and records what happened.

    The executor never sees a model proposal. It cannot act on anything that
    did not come through the policy gate.
    """

    def __init__(
        self,
        *,
        kg: KGExecutionPort | None = None,
        literature: LiteratureExecutionPort | None = None,
        tool_name: str = "kg_neighbors",
    ) -> None:
        self.kg = kg
        self.literature = literature
        self.tool_name = tool_name
        self.executed_action_hashes: list[str] = []

    def execute(
        self,
        sealed: SealedAction,
        *,
        episode_id: str,
        run_id: str,
    ) -> ExecutionOutcome:
        """Execute one sealed action and record the observation."""
        started = time.monotonic()
        request = dict(sealed.request)
        try:
            if sealed.action_type is ExplorationActionType.RETRIEVE_LITERATURE:
                outcome = self._run_literature(
                    {**request, "sealed_action_id": sealed.sealed_action_id}
                )
            elif sealed.action_type is ExplorationActionType.FINISH:
                outcome = self._run_finish(request)
            else:
                outcome = self._run_kg(request)
        except ToolCallError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            observation = ToolObservation(
                observation_id=new_id("observation"),
                episode_id=episode_id,
                tool_name=sealed.tool_name,
                operation_key=sealed.sealed_action_id,
                request_digest=sealed.request_digest,
                row_count=0,
                elapsed_ms=elapsed,
                error_code=exc.code,
                decision_id=sealed.decision_id,
                action_type=sealed.action_type.value,
                status="error",
                retryable=exc.retryable,
                message=exc.message,
            )
            return ExecutionOutcome(
                observation=observation,
                error_code=exc.code,
                retryable=exc.retryable,
            )
        elapsed = int((time.monotonic() - started) * 1000)
        observation = ToolObservation(
            observation_id=new_id("observation"),
            episode_id=episode_id,
            tool_name=sealed.tool_name,
            operation_key=sealed.sealed_action_id,
            request_digest=sealed.request_digest,
            row_count=outcome.rows_examined,
            elapsed_ms=elapsed,
            decision_id=sealed.decision_id,
            action_type=sealed.action_type.value,
            status="success" if not outcome.error_code else "error",
            discovered_entity_ids=outcome.discovered_entity_ids,
            new_evidence_ids=outcome.new_evidence_ids,
            new_path_ids=outcome.new_path_ids,
            rows_examined=outcome.rows_examined,
            truncated=outcome.truncated,
            error_code=outcome.error_code,
            retryable=outcome.retryable,
        )
        self.executed_action_hashes.append(sealed.canonical_action_hash)
        outcome.observation = observation
        return outcome

    # ---- tool implementations -------------------------------------------

    def _run_kg(self, request: dict[str, Any]) -> ExecutionOutcome:
        if self.kg is None:
            raise ToolCallError(
                "kg_unavailable", "no knowledge graph provider is wired up"
            )
        try:
            edges, trace_id = self.kg.neighbors(request)
        except Exception as exc:  # provider boundary
            code = str(getattr(exc, "code", "kg_query_error"))
            raise ToolCallError(
                code,
                f"the knowledge graph query failed: {exc}",
                retryable=code in {"kg_unavailable", "kg_query_timeout"},
            ) from exc
        discovered: list[str] = []
        new_evidence: list[str] = []
        for edge in edges:
            for side in ("head", "tail"):
                entity = getattr(edge, side, None)
                canonical_id = getattr(entity, "canonical_id", None)
                if canonical_id and canonical_id not in discovered:
                    discovered.append(canonical_id)
            edge_id = getattr(edge, "edge_id", None)
            if edge_id:
                new_evidence.append(str(edge_id))
        return ExecutionOutcome(
            new_evidence_ids=new_evidence,
            discovered_entity_ids=discovered,
            rows_examined=len(edges),
        )

    def _run_literature(self, request: dict[str, Any]) -> ExecutionOutcome:
        if self.literature is None:
            raise ToolCallError(
                "literature_unavailable", "no literature provider is wired up"
            )
        try:
            chunks, trace_id, snapshot_id = self.literature.search(request)
        except Exception as exc:  # provider boundary
            code = str(getattr(exc, "code", "literature_error"))
            raise ToolCallError(
                code,
                f"the literature search failed: {exc}",
                retryable=code in {"literature_timeout", "literature_rate_limited"},
            ) from exc
        return ExecutionOutcome(
            new_evidence_ids=[
                str(getattr(item, "evidence_id", item)) for item in chunks
            ],
            rows_examined=len(chunks),
        )

    def _run_finish(self, request: dict[str, Any]) -> ExecutionOutcome:
        return ExecutionOutcome()
