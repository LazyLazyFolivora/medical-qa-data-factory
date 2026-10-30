"""Structured planner for the entity-first question-production loop."""
from __future__ import annotations

from typing import Any, Protocol

from ..llm.contracts import StructuredGenerationRequest
from .exploration import AgentDecision, ExplorationState
from .planner import PlannerContext


class EntityQuestionPlannerPort(Protocol):
    """Choose search, candidate proposal, abandonment, or an explicit stop."""

    def propose(
        self,
        state: ExplorationState,
        context: PlannerContext,
    ) -> AgentDecision:  # pragma: no cover - interface
        ...


class StructuredEntityQuestionPlanner:
    """Ask the structured model for one decision over the current state."""

    def __init__(
        self,
        model: Any,
        *,
        prompt_version: str = "entity-question-explorer-v4",
    ) -> None:
        self.model = model
        self.prompt_version = prompt_version

    def propose(
        self,
        state: ExplorationState,
        context: PlannerContext,
    ) -> AgentDecision:
        state_digest = state.state_digest()
        request = StructuredGenerationRequest(
            invocation_id=(
                f"entity-question-planner.{state.episode_id}.{context.turn}"
            ),
            role="entity_question_exploration_planner",
            system_prompt_version=self.prompt_version,
            payload={
                "exploration_state": state.model_dump(mode="json"),
                "planner_context": context.model_dump(mode="json"),
                "instruction": (
                    "Choose one bounded search action, propose a structured "
                    "TaskCandidate, abandon one unproductive direction, or "
                    "stop with a structured reason. A candidate is not a "
                    "validated or published question. The current production "
                    "planner context may expose available_skills. When a "
                    "suitable skill is available, select it by skill_id and "
                    "supply only its declared arguments instead of inventing "
                    "a raw tool action. "
                    "validator accepts constrained_entity candidates: one "
                    "primary relation step plus at least one independently "
                    "checkable graph constraint."
                ),
            },
            idempotency_key=(
                f"entity-question-planner:{state.episode_id}:"
                f"{context.turn}:{state_digest}"
            ),
        )
        result = self.model.generate_structured(request, AgentDecision)
        if state.state_digest() != state_digest:
            raise ValueError("the model decision was made from a stale state")
        decision = AgentDecision.model_validate(result.parsed).model_copy(
            update={"state_digest": state_digest}
        )
        return decision
