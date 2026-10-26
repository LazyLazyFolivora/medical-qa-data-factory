"""Budget accounting with explicit, inspectable counters.

Counting rules follow the implementation spec: seed identity resolution costs
one tool call, incoming and outgoing each cost one, ``edges_observed`` counts
raw rows before deduplication, and every node execution or retry costs a step.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .contracts import EntityGenerationRequest, StopReason


@dataclass
class GenerationBudget:
    max_tool_calls: int
    max_edges_observed: int
    max_agent_steps: int
    max_wall_time_seconds: int
    tool_calls: int = 0
    edges_observed: int = 0
    steps: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @classmethod
    def from_request(cls, request: EntityGenerationRequest) -> "GenerationBudget":
        safety = request.resolved_safety_budget()
        return cls(
            max_tool_calls=safety.max_tool_calls,
            max_edges_observed=safety.max_edges_observed,
            max_agent_steps=safety.max_model_attempts,
            max_wall_time_seconds=safety.max_wall_time_seconds,
        )

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining_edge_budget(self) -> int:
        return max(self.max_edges_observed - self.edges_observed, 0)

    @property
    def remaining_tool_calls(self) -> int:
        return max(self.max_tool_calls - self.tool_calls, 0)

    def split_edge_budget(self) -> tuple[int, int]:
        """Split the remaining edge budget; odd remainder goes to outgoing."""
        remaining = self.remaining_edge_budget
        return (remaining // 2 + remaining % 2, remaining // 2)

    def spend_tool_call(self, count: int = 1) -> None:
        self.tool_calls += count

    def spend_edges(self, count: int) -> None:
        self.edges_observed += count

    def spend_step(self) -> None:
        self.steps += 1

    def exhausted_reason(self) -> StopReason | None:
        """Return the first exhausted budget, or ``None`` when still usable."""
        if self.steps >= self.max_agent_steps:
            return StopReason.BUDGET_EXHAUSTED
        if self.tool_calls >= self.max_tool_calls:
            return StopReason.BUDGET_EXHAUSTED
        if self.elapsed_seconds >= self.max_wall_time_seconds:
            return StopReason.BUDGET_EXHAUSTED
        if self.remaining_edge_budget <= 0:
            return StopReason.BUDGET_EXHAUSTED
        return None

    def snapshot(self) -> dict[str, int | float]:
        return {
            "tool_calls": self.tool_calls,
            "edges_observed": self.edges_observed,
            "steps": self.steps,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }
