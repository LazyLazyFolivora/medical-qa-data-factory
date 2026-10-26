"""The bounded KG/RAG ReAct graph.

Step 6 of the real-generation-agent plan. This is the LangGraph wiring for the
exploration loop:

    observe -> decide -> gate -> act -> persist -> observe

The graph is a real agent loop, not a renamed DAG. Its nodes are driven by
what the previous node observed, and the loop ends when the model asks to
finish, when the policy refuses every proposal, when two consecutive turns
produce nothing new, or when the budget runs out.

The fixed ``rule-based`` DAG in ``graph.py`` stays as the regression baseline.
This graph is the agent path.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Sequence

from langgraph.graph import END, StateGraph

from .actions import ModelActionProposal, PolicyGate, RunPolicy
from .contracts import (
    ExplorationActionType,
    GenerationSpec,
    StopReason,
    ToolObservation,
)
from .executor import Executor
from .planner import PlannerContext, PlannerPort, build_planner_context
from .state import EntityGenerationState
from .training import (
    PlannerExecution,
    PlannerKnownEntity,
    PlannerOutcome,
    PlannerSelectedAction,
    PlannerState,
    PlannerStep,
    stable_training_id,
)


class Route:
    """The node names of the agent graph."""

    OBSERVE = "observe"
    DECIDE = "decide"
    GATE = "gate"
    ACT = "act"
    PERSIST = "persist"
    ASSESS = "assess"
    DONE = "done"
    PARTIAL = "done_partial"


@dataclass
class ReactContext:
    """The injected dependencies of the agent graph."""

    planner: PlannerPort
    executor: Executor
    policy: RunPolicy
    run_id: str
    episode_id: str
    seed_entity_id: str
    seed_entity_name: str
    available_tools: Sequence[str] = ()
    open_needs: Sequence[dict[str, Any]] = ()
    #: A live provider lets newly discovered KG facts become RAG needs in a
    #: later turn without rebuilding the graph.
    open_needs_provider: Callable[[], Sequence[dict[str, Any]]] | None = None
    #: A bounded semantic view of collected evidence for the next decision.
    evidence_summary_provider: Callable[[], dict[str, Any]] | None = None
    evidence_summary: dict[str, Any] = field(default_factory=dict)
    #: Reads the live generation budget after each tool call.
    remaining_edge_budget_provider: Callable[[], int] | None = None
    generation_spec: GenerationSpec | None = None
    available_action_types: Sequence[str] = ()
    #: The hook that judges whether the evidence is sufficient.
    sufficiency: Any = None
    #: Called with each observation, so the caller can persist it.
    on_observation: Callable[[ToolObservation], None] | None = None
    #: Called with each sealed action, for the audit trail.
    on_sealed_action: Callable[[Any], None] | None = None
    #: Called with each policy rejection, for the audit trail.
    on_rejection: Callable[[Any], None] | None = None
    #: Called exactly once per persisted proposal.
    on_planner_step: Callable[[PlannerStep], None] | None = None
    model_config_id: str = "unknown-model"
    max_turns: int = 12
    #: The number of consecutive turns without new evidence that stops the run.
    stagnation_limit: int = 2
    #: Consecutive policy refusals that stop a model that cannot correct itself.
    rejection_limit: int = 3


def build_react_graph(
    context: ReactContext,
    *,
    checkpointer: Any = None,
) -> Any:
    """Build the agent graph.

    Every node reads typed state and returns typed state. No node parses free
    text, and no node decides anything the model is allowed to decide.
    """
    def dump_policy(policy: RunPolicy) -> dict[str, Any]:
        payload = asdict(policy)
        payload["executed_action_hashes"] = sorted(policy.executed_action_hashes)
        return payload

    def with_live_edge_budget(policy: RunPolicy) -> RunPolicy:
        if context.remaining_edge_budget_provider is None:
            return policy
        return replace(
            policy,
            remaining_edge_budget=max(
                int(context.remaining_edge_budget_provider()), 0
            ),
        )

    def load_policy(state: EntityGenerationState) -> RunPolicy:
        stored = state.get("react_policy")
        payload = dict(stored or dump_policy(context.policy))
        for key in (
            "allowed_relations",
            "allowed_target_types",
            "allowed_directions",
            "allowed_action_types",
            "open_need_ids",
            "available_tool_names",
        ):
            payload[key] = tuple(payload.get(key, ()))
        executed = set(payload.get("executed_action_hashes", ()))
        executed.update(state.get("react_action_hashes", ()))
        payload["executed_action_hashes"] = frozenset(executed)
        if not stored:
            payload["remaining_tool_calls"] = max(
                context.policy.remaining_tool_calls - len(executed), 0
            )
        if any(
            item.get("new_evidence_ids") or item.get("new_path_ids")
            for item in state.get("react_observations", [])
        ):
            payload["has_evidence"] = True
        return with_live_edge_budget(RunPolicy(**payload))

    def load_observations(state: EntityGenerationState) -> list[ToolObservation]:
        return [
            ToolObservation.model_validate(item)
            for item in state.get("react_observations", [])
        ]

    def initial_state(request: dict[str, Any]) -> EntityGenerationState:
        return EntityGenerationState(
            request=request,
            episode_id=context.episode_id,
            task_id=str(request.get("task_id", "")),
            current_node=Route.OBSERVE,
            react_turn=0,
            react_policy=dump_policy(with_live_edge_budget(context.policy)),
            react_observations=[],
            react_known_entity_ids=[context.seed_entity_id],
            react_action_hashes=[],
            react_open_need_ids=[],
            react_path_ids=[],
            react_sufficiency=None,
            react_sealed_actions=[],
            react_rejections=[],
            react_planner_steps=[],
            react_stagnation_count=0,
            react_rejection_streak=0,
            stop_reason=None,
            errors=[],
        )

    def observe(state: EntityGenerationState) -> EntityGenerationState:
        """Read the current state into the planner's view."""
        payload = dict(state)
        payload["current_node"] = Route.OBSERVE
        payload["react_known_entity_ids"] = sorted(
            set(state.get("react_known_entity_ids", []))
            | {context.seed_entity_id}
        )
        return payload  # type: ignore[return-value]

    def decide(state: EntityGenerationState) -> EntityGenerationState:
        """Ask the planner for one proposal."""
        turn = int(state.get("react_turn", 0)) + 1
        observations = load_observations(state)
        action_hashes = list(state.get("react_action_hashes", []))
        policy = load_policy(state)
        if context.open_needs_provider is not None:
            policy = replace(
                policy,
                open_need_ids=tuple(
                    str(item.get("need_id"))
                    for item in context.open_needs_provider()
                    if item.get("need_id")
                ),
            )
        live_needs = (
            list(context.open_needs_provider())
            if context.open_needs_provider is not None
            else list(context.open_needs)
        )
        gap_names = [
            str(item.get("information_need") or item.get("need_id"))
            for item in live_needs
            if item
        ]
        available_actions = list(context.available_action_types)
        available_tools = list(context.available_tools)
        if not live_needs:
            available_actions = [
                item for item in available_actions if item != "retrieve_literature"
            ]
            available_tools = [
                item for item in available_tools if item != "literature_search"
            ]
        planner_context = build_planner_context(
            run_id=context.run_id,
            episode_id=context.episode_id,
            turn=turn,
            seed_entity_id=context.seed_entity_id,
            seed_entity_name=context.seed_entity_name,
            known_entities=[
                {"canonical_id": item}
                for item in sorted(state.get("react_known_entity_ids", []))
            ],
            observations=observations,
            open_needs=live_needs,
            evidence_gaps=gap_names,
            evidence_summary=(
                context.evidence_summary_provider()
                if context.evidence_summary_provider is not None
                else context.evidence_summary
            ),
            executed_action_hashes=action_hashes,
            policy=policy,
            available_tools=available_tools,
            available_actions=available_actions,
            remaining_edge_budget=policy.remaining_edge_budget,
            recent_policy_rejections=list(state.get("react_rejections", [])),
        )
        try:
            proposal = context.planner.propose(planner_context, ModelActionProposal)
            error: str | None = None
        except Exception as exc:
            proposal = None
            error = f"{type(exc).__name__}: {exc}"
        payload = dict(state)
        payload["current_node"] = Route.DECIDE
        payload["react_turn"] = turn
        payload["_proposal"] = (
            proposal.model_dump(mode="json") if proposal is not None else None
        )
        payload["_proposal_error"] = error
        payload["_planner_state"] = (
            PlannerState(
                known_entities=[
                    PlannerKnownEntity(
                        canonical_id=str(item.get("canonical_id")),
                        name=item.get("name"),
                    )
                    for item in planner_context.known_entities
                    if item.get("canonical_id")
                ],
                observation_summaries=list(planner_context.observations),
                evidence_summary=dict(planner_context.evidence_summary),
                evidence_gaps=list(planner_context.evidence_gaps),
                target_difficulty=(
                    list(context.generation_spec.difficulty_targets)
                    if context.generation_spec is not None
                    else []
                ),
                unsatisfied_constraints=list(planner_context.evidence_gaps),
                available_actions=list(
                    planner_context.available_actions
                    or (
                        "query_neighbors",
                        "expand_entity",
                        "finish",
                    )
                ),
            ).model_dump(mode="json")
            if proposal is not None
            else None
        )
        payload["_sealed"] = None
        payload["_rejection"] = None
        payload["_outcome"] = None
        return payload  # type: ignore[return-value]

    def gate(state: EntityGenerationState) -> EntityGenerationState:
        """Validate the proposal and seal it, or refuse it."""
        payload = dict(state)
        payload["current_node"] = Route.GATE
        raw = payload.get("_proposal")
        if raw is None:
            payload["_sealed"] = None
            payload["_rejection"] = None
            return payload  # type: ignore[return-value]
        proposal = ModelActionProposal.model_validate(raw)
        observations = load_observations(state)
        gate_policy = load_policy(state)
        if context.open_needs_provider is not None:
            gate_policy = replace(
                gate_policy,
                open_need_ids=tuple(
                    str(item.get("need_id"))
                    for item in context.open_needs_provider()
                    if item.get("need_id")
                ),
            )
        gate_ = PolicyGate(gate_policy)
        sealed, rejection = gate_.evaluate(
            proposal,
            run_id=context.run_id,
            episode_id=context.episode_id,
            turn=int(state.get("react_turn", 1)),
            known_entity_ids=set(state.get("react_known_entity_ids", [])),
            known_observation_ids={item.observation_id for item in observations},
        )
        payload["_sealed"] = sealed.model_dump(mode="json") if sealed else None
        payload["_rejection"] = (
            rejection.model_dump(mode="json") if rejection else None
        )
        return payload  # type: ignore[return-value]

    def act(state: EntityGenerationState) -> EntityGenerationState:
        """Execute the sealed action, or record the refusal."""
        payload = dict(state)
        payload["current_node"] = Route.ACT
        payload["_policy_before"] = dump_policy(load_policy(state))
        sealed_payload = payload.get("_sealed")
        if sealed_payload is None:
            rejection = payload.get("_rejection")
            if rejection is not None:
                if context.on_rejection is not None:
                    context.on_rejection(rejection)
                rejections = list(state.get("react_rejections", []))
                rejections.append(rejection)
                payload["react_rejections"] = rejections
                payload["react_rejection_streak"] = int(
                    state.get("react_rejection_streak", 0)
                ) + 1
            return payload  # type: ignore[return-value]
        from .actions import SealedAction

        sealed = SealedAction.model_validate(sealed_payload)
        payload["react_rejection_streak"] = 0
        if context.on_sealed_action is not None:
            context.on_sealed_action(sealed)
        if sealed.action_type is ExplorationActionType.FINISH:
            # A finish is a stop signal, not a tool call. It produces no
            # observation and consumes no tool budget.
            payload["_outcome"] = None
            return payload  # type: ignore[return-value]
        outcome = context.executor.execute(
            sealed, episode_id=context.episode_id, run_id=context.run_id
        )
        observation = outcome.observation
        if observation is None:  # defensive: every executed action must be auditable
            raise RuntimeError("executor returned no observation")
        observations = load_observations(state)
        observations.append(observation)
        payload["react_observations"] = [
            item.model_dump(mode="json") for item in observations
        ]
        known_entity_ids = set(state.get("react_known_entity_ids", []))
        known_entity_ids.update(outcome.discovered_entity_ids)
        payload["react_known_entity_ids"] = sorted(known_entity_ids)
        action_hashes = list(state.get("react_action_hashes", []))
        action_hashes.append(sealed.canonical_action_hash)
        payload["react_action_hashes"] = action_hashes
        policy = load_policy(state).with_execution(sealed.canonical_action_hash)
        if outcome.new_evidence_ids or outcome.new_path_ids:
            policy = policy.with_evidence(
                kg_evidence_count=(
                    len(outcome.new_evidence_ids)
                    if sealed.action_type
                    in (
                        ExplorationActionType.QUERY_NEIGHBORS,
                        ExplorationActionType.EXPAND_ENTITY,
                    )
                    else 0
                ),
                literature_evidence_count=(
                    len(outcome.new_evidence_ids)
                    if sealed.action_type
                    is ExplorationActionType.RETRIEVE_LITERATURE
                    else 0
                ),
            )
            payload["react_stagnation_count"] = 0
        else:
            payload["react_stagnation_count"] = int(
                state.get("react_stagnation_count", 0)
            ) + 1
        payload["react_policy"] = dump_policy(policy)
        payload["_outcome"] = {
            "new_evidence_ids": outcome.new_evidence_ids,
            "new_path_ids": outcome.new_path_ids,
            "error_code": outcome.error_code,
            "observation": observation.model_dump(mode="json"),
        }
        return payload  # type: ignore[return-value]

    def persist(state: EntityGenerationState) -> EntityGenerationState:
        """Record the turn's sealed action and observation."""
        payload = dict(state)
        payload["current_node"] = Route.PERSIST
        sealed_payload = payload.get("_sealed")
        if sealed_payload is not None:
            sealed_actions = list(state.get("react_sealed_actions", []))
            sealed_actions.append(sealed_payload)
            payload["react_sealed_actions"] = sealed_actions
        outcome_payload = payload.get("_outcome")
        if outcome_payload is not None:
            # The hook fires once per executed action, not once per turn.
            observation = ToolObservation.model_validate(
                outcome_payload["observation"]
            )
            if context.on_observation is not None:
                context.on_observation(observation)
        proposal_payload = payload.get("_proposal")
        planner_state_payload = payload.get("_planner_state")
        if proposal_payload is not None and planner_state_payload is not None:
            proposal = ModelActionProposal.model_validate(proposal_payload)
            selected = proposal.validated_action()
            sealed_payload = payload.get("_sealed")
            rejection_payload = payload.get("_rejection")
            outcome = payload.get("_outcome") or {}
            observation_payload = outcome.get("observation")
            if rejection_payload:
                execution = PlannerExecution(
                    policy_result="rejected",
                    status="rejected",
                    rejection_code=rejection_payload.get("code"),
                )
                outcome_value = PlannerOutcome(useful=False)
            elif selected.action_type is ExplorationActionType.FINISH:
                execution = PlannerExecution(
                    policy_result="accepted",
                    sealed_action_id=sealed_payload.get("sealed_action_id"),
                    status="finish",
                )
                outcome_value = PlannerOutcome(
                    useful=True,
                    satisfied_constraints=["finish_requested"],
                )
            else:
                observation = (
                    ToolObservation.model_validate(observation_payload)
                    if observation_payload is not None
                    else None
                )
                execution = PlannerExecution(
                    policy_result="accepted",
                    sealed_action_id=sealed_payload.get("sealed_action_id"),
                    observation_id=observation.observation_id if observation else None,
                    status=(
                        observation.status
                        if observation is not None and observation.status
                        else "error"
                    ),
                )
                new_ids = list(outcome.get("new_evidence_ids", []))
                new_paths = list(outcome.get("new_path_ids", []))
                outcome_value = PlannerOutcome(
                    discovered_entity_ids=list(
                        observation.discovered_entity_ids if observation else []
                    ),
                    new_evidence_ids=new_ids,
                    evidence_gain=len(new_ids) + len(new_paths),
                    useful=bool(new_ids or new_paths),
                )
                before = state.get("_policy_before", {})
                before_kg = int(before.get("kg_evidence_count", 0))
                before_lit = int(before.get("literature_evidence_count", 0))
                after_kg = before_kg + (
                    len(new_ids)
                    if selected.action_type
                    in (
                        ExplorationActionType.QUERY_NEIGHBORS,
                        ExplorationActionType.EXPAND_ENTITY,
                    )
                    else 0
                )
                after_lit = before_lit + (
                    len(new_ids)
                    if selected.action_type
                    is ExplorationActionType.RETRIEVE_LITERATURE
                    else 0
                )
                satisfied: list[str] = []
                if (
                    before_kg < int(before.get("min_kg_evidence", 0))
                    <= after_kg
                ):
                    satisfied.append("min_kg_evidence")
                if (
                    before_lit
                    < int(before.get("min_literature_evidence", 0))
                    <= after_lit
                ):
                    satisfied.append("min_literature_evidence")
                if (
                    before_kg + before_lit
                    < int(before.get("min_total_evidence", 1))
                    <= after_kg + after_lit
                ):
                    satisfied.append("min_total_evidence")
                outcome_value = outcome_value.model_copy(
                    update={"satisfied_constraints": satisfied}
                )
            trajectory_id = stable_training_id(
                "planner-trajectory",
                context.episode_id,
                prefix="trajectory",
            )
            step = PlannerStep(
                step_id=stable_training_id(
                    "planner-step",
                    context.episode_id,
                    int(state.get("react_turn", 1)),
                    proposal.model_dump(mode="json"),
                    prefix="planner-step",
                ),
                trajectory_id=trajectory_id,
                episode_id=context.episode_id,
                turn=int(state.get("react_turn", 1)),
                state=PlannerState.model_validate(planner_state_payload),
                selected_action=PlannerSelectedAction(
                    action_type=selected.action_type.value,
                    parameters=dict(proposal.action),
                    expected_information=proposal.expected_information,
                    evidence_gap=proposal.evidence_gap,
                ),
                execution=execution,
                outcome=outcome_value,
            )
            planner_steps = list(state.get("react_planner_steps", []))
            if all(item.get("step_id") != step.step_id for item in planner_steps):
                planner_steps.append(step.model_dump(mode="json"))
            payload["react_planner_steps"] = planner_steps
            if context.on_planner_step is not None:
                context.on_planner_step(step)
        return payload  # type: ignore[return-value]

    def assess(state: EntityGenerationState) -> EntityGenerationState:
        """Decide whether the loop should continue or stop."""
        payload = dict(state)
        payload["current_node"] = Route.ASSESS
        turn = int(state.get("react_turn", 0))
        observations = load_observations(state)
        policy = load_policy(state)
        # The model asked to finish.
        sealed_payload = payload.get("_sealed")
        if sealed_payload:
            from .actions import SealedAction

            sealed = SealedAction.model_validate(sealed_payload)
            if sealed.action_type is ExplorationActionType.FINISH:
                payload["stop_reason"] = StopReason.EVIDENCE_SUFFICIENT.value
                return payload  # type: ignore[return-value]
        if context.sufficiency is not None:
            try:
                enough = bool(context.sufficiency(observations))
            except Exception:
                enough = False
            if enough:
                payload["stop_reason"] = StopReason.EVIDENCE_SUFFICIENT.value
                return payload  # type: ignore[return-value]
        if turn >= context.max_turns:
            payload["stop_reason"] = StopReason.BUDGET_EXHAUSTED.value
            return payload  # type: ignore[return-value]
        if int(state.get("react_stagnation_count", 0)) >= context.stagnation_limit:
            payload["stop_reason"] = StopReason.NO_NEW_EVIDENCE.value
            return payload  # type: ignore[return-value]
        if policy.remaining_tool_calls <= 0:
            payload["stop_reason"] = StopReason.BUDGET_EXHAUSTED.value
            return payload  # type: ignore[return-value]
        if (
            policy.remaining_edge_budget is not None
            and policy.remaining_edge_budget <= 0
        ):
            payload["stop_reason"] = StopReason.BUDGET_EXHAUSTED.value
            return payload  # type: ignore[return-value]
        if (
            int(state.get("react_rejection_streak", 0))
            >= context.rejection_limit
        ):
            payload["stop_reason"] = StopReason.NO_VALID_ACTION.value
            return payload  # type: ignore[return-value]
        return payload  # type: ignore[return-value]

    def done(state: EntityGenerationState) -> EntityGenerationState:
        payload = dict(state)
        payload["current_node"] = Route.DONE
        if not payload.get("stop_reason") and payload.get("_proposal_error"):
            # The model did not return a usable proposal.
            payload["stop_reason"] = StopReason.MODEL_ERROR.value
        return payload  # type: ignore[return-value]

    def done_partial(state: EntityGenerationState) -> EntityGenerationState:
        payload = dict(state)
        payload["current_node"] = Route.PARTIAL
        if not payload.get("stop_reason"):
            payload["stop_reason"] = StopReason.EVIDENCE_EXHAUSTED.value
        return payload  # type: ignore[return-value]

    def after_decide(state: EntityGenerationState) -> str:
        if state.get("_proposal") is None:
            return Route.DONE
        return Route.GATE

    def after_gate(state: EntityGenerationState) -> str:
        if state.get("_sealed") is None:
            return Route.ACT
        return Route.ACT

    def after_assess(state: EntityGenerationState) -> str:
        if state.get("stop_reason"):
            return Route.DONE
        return Route.OBSERVE

    graph = StateGraph(EntityGenerationState)
    graph.add_node(Route.OBSERVE, observe)
    graph.add_node(Route.DECIDE, decide)
    graph.add_node(Route.GATE, gate)
    graph.add_node(Route.ACT, act)
    graph.add_node(Route.PERSIST, persist)
    graph.add_node(Route.ASSESS, assess)
    graph.add_node(Route.DONE, done)
    graph.add_node(Route.PARTIAL, done_partial)
    graph.set_entry_point(Route.OBSERVE)
    graph.add_edge(Route.OBSERVE, Route.DECIDE)
    graph.add_conditional_edges(
        Route.DECIDE, after_decide, {Route.DONE: Route.DONE, Route.GATE: Route.GATE}
    )
    graph.add_conditional_edges(
        Route.GATE, after_gate, {Route.ACT: Route.ACT}
    )
    graph.add_edge(Route.ACT, Route.PERSIST)
    graph.add_edge(Route.PERSIST, Route.ASSESS)
    graph.add_conditional_edges(
        Route.ASSESS,
        after_assess,
        {Route.DONE: Route.DONE, Route.OBSERVE: Route.OBSERVE},
    )
    graph.add_edge(Route.DONE, END)
    graph.add_edge(Route.PARTIAL, END)
    compiled = graph.compile(checkpointer=checkpointer)
    compiled.initial_state = initial_state  # type: ignore[attr-defined]
    return compiled
