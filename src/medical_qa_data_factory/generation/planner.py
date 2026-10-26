"""The planner: what the model is allowed to see, and what it may propose.

Step 5 of the real-generation-agent plan. The planner context is deliberately
compressed and structured. It contains no SQL, no database path, no API key, no
full high-fan-out neighbourhood and no stored chain of thought. The model sees
what it needs to choose one action, and nothing more.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from pydantic import Field, field_validator

from ..domain.common import ContractModel, UtcDateTime
from .actions import AgentActionProposal, ModelActionProposal, RunPolicy
from .contracts import ExplorationActionType, ToolObservation


class PlannerContext(ContractModel):
    """What the planner sees. Everything here is structured and bounded."""

    schema_version: str = "planner-context-v2"
    run_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    turn: int = Field(ge=1)
    #: The seed's identity, resolved by the server.
    seed_entity_id: str = Field(min_length=1)
    seed_entity_name: str = Field(min_length=1)
    #: The entities discovered so far, with a short summary each.
    known_entities: list[dict[str, Any]] = Field(default_factory=list)
    #: The observations already produced, with their outcomes.
    observations: list[dict[str, Any]] = Field(default_factory=list)
    #: The evidence gathered so far, summarised rather than quoted in full.
    evidence_summary: dict[str, Any] = Field(default_factory=dict)
    #: The literature needs that are still open.
    open_needs: list[dict[str, Any]] = Field(default_factory=list)
    #: The gaps the run still has.
    evidence_gaps: list[str] = Field(default_factory=list)
    #: The action hashes already executed, so the model does not repeat one.
    executed_action_hashes: list[str] = Field(default_factory=list)
    #: The provider's capabilities, so the model does not ask for what is absent.
    available_tools: list[str] = Field(default_factory=list)
    #: The finite action types currently meaningful from this state.
    available_actions: list[str] = Field(default_factory=list)
    can_pin_snapshot: bool = False
    remaining_tool_calls: int = 0
    max_hops: int = 3
    #: The hard server-side ceiling for one neighbour or path request.
    max_results_per_query: int = Field(default=50, ge=1)
    #: The edge budget still available to this run. ``None`` only appears in
    #: legacy callers that do not account for edges.
    remaining_edge_budget: int | None = Field(default=None, ge=0)
    #: Recent server refusals. These are feedback from the policy gate, not
    #: model-generated reasoning, and let the next turn correct its request.
    recent_policy_rejections: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("recent_policy_rejections")
    @classmethod
    def bound_policy_rejections(
        cls, values: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Expose only the fields the next planner turn can act on."""
        return [
            {
                "turn": item.get("turn"),
                "action_type": item.get("action_type"),
                "code": item.get("code"),
                "message": item.get("message"),
            }
            for item in values[-5:]
        ]


class PlannerPort:
    """The interface the planner uses to reach a model.

    The implementation calls the structured model with the ``explorer`` role
    and the planner's own prompt version. The planner never passes the model a
    free-text instruction of its own.
    """

    def propose(
        self, context: PlannerContext, response_model: type[Any]
    ) -> ModelActionProposal:  # pragma: no cover - interface
        raise NotImplementedError


class StructuredModelPlanner(PlannerPort):
    """A planner that asks the structured model for one action."""

    def __init__(self, model: Any, *, prompt_version: str = "explorer-v2") -> None:
        self.model = model
        self.prompt_version = prompt_version

    def propose(
        self, context: PlannerContext, response_model: type[Any]
    ) -> ModelActionProposal:
        from ..llm.contracts import StructuredGenerationRequest

        request = StructuredGenerationRequest(
            invocation_id=f"planner.{context.episode_id}.{context.turn}",
            role="exploration_planner",
            system_prompt_version=self.prompt_version,
            payload=context.model_dump(mode="json"),
            idempotency_key=(
                f"planner:{context.episode_id}:{context.turn}:"
                f"{len(context.executed_action_hashes)}"
            ),
        )
        # ``response_model`` remains part of the port signature for scripted
        # planners, but the real model boundary always uses the strict
        # discriminated action schema. Passing the legacy permissive proposal
        # exposed ``action`` as an arbitrary object to the provider.
        result = self.model.generate_structured(request, AgentActionProposal)
        candidate = AgentActionProposal.model_validate(result.parsed)
        return candidate.as_model_proposal()


@dataclass
class TurnResult:
    """One planner turn: the proposal and what the gate said about it."""

    turn: int
    proposal: ModelActionProposal | None = None
    sealed: Any = None
    rejection: Any = None
    observation: ToolObservation | None = None

    @property
    def was_refused(self) -> bool:
        return self.sealed is None


def build_planner_context(
    *,
    run_id: str,
    episode_id: str,
    turn: int,
    seed_entity_id: str,
    seed_entity_name: str,
    known_entities: Sequence[dict[str, Any]],
    observations: Sequence[ToolObservation],
    open_needs: Sequence[dict[str, Any]],
    evidence_gaps: Sequence[str],
    executed_action_hashes: Sequence[str],
    policy: RunPolicy,
    available_tools: Sequence[str],
    available_actions: Sequence[str] = (),
    evidence_summary: dict[str, Any] | None = None,
    remaining_edge_budget: int | None = None,
    recent_policy_rejections: Sequence[dict[str, Any]] = (),
) -> PlannerContext:
    """Assemble the planner's view of the run.

    The context is capped. A planner that cannot see everything is a planner
    that has to choose, and a planner that has to choose is the point.
    """
    return PlannerContext(
        run_id=run_id,
        episode_id=episode_id,
        turn=turn,
        seed_entity_id=seed_entity_id,
        seed_entity_name=seed_entity_name,
        known_entities=[dict(item) for item in known_entities[:50]],
        observations=[
            {
                "observation_id": item.observation_id,
                "action_type": item.action_type,
                "status": item.status,
                "rows_examined": item.rows_examined,
                "truncated": item.truncated,
                "new_evidence_ids": item.new_evidence_ids,
                "discovered_entity_ids": item.discovered_entity_ids,
                "error_code": item.error_code,
            }
            for item in observations[-20:]
        ],
        evidence_summary=dict(evidence_summary or {}),
        open_needs=[dict(item) for item in open_needs[:20]],
        evidence_gaps=list(evidence_gaps)[:20],
        executed_action_hashes=list(executed_action_hashes)[-20:],
        available_tools=list(available_tools),
        available_actions=list(available_actions),
        can_pin_snapshot=policy.can_pin_snapshot,
        remaining_tool_calls=policy.remaining_tool_calls,
        max_hops=policy.max_hops,
        max_results_per_query=policy.max_results_per_query,
        remaining_edge_budget=(
            remaining_edge_budget
            if remaining_edge_budget is not None
            else policy.remaining_edge_budget
        ),
        recent_policy_rejections=[
            {
                "turn": item.get("turn"),
                "action_type": item.get("action_type"),
                "code": item.get("code"),
                "message": item.get("message"),
            }
            for item in recent_policy_rejections[-5:]
        ],
    )


#: The action types the planner may propose, in the order they are offered.
PLANNER_ACTION_TYPES: tuple[str, ...] = tuple(
    item.value for item in ExplorationActionType
)
