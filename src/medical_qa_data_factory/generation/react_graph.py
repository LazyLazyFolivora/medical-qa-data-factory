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
from .exploration import (
    AgentDecision,
    ExplorationState,
    ValidationFeedback,
    apply_validation_feedback,
)
from .planner import PlannerContext, PlannerPort, build_planner_context
from .state import EntityGenerationState
from .skills import (
    SkillInvocation,
    SkillOutcome,
    SkillRegistry,
    SkillRuntimeContext,
    SkillSelection,
)
from .task_contracts import TaskCandidate
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
    seed_entity_type: str | None = None
    #: Optional v2 planner that may either search or propose TaskCandidate.
    #: When absent, the legacy action-only planner remains supported.
    entity_planner: Any = None
    #: Optional discoverable skill catalog for v2 planner decisions.
    skill_registry: SkillRegistry | None = None
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
    #: On a finish proposal, discover candidate tasks from current evidence
    #: and run the independent validator. A recoverable gap re-enters search.
    candidate_assessor: Callable[
        [Sequence[ToolObservation], EntityGenerationState],
        tuple[Sequence[TaskCandidate], Sequence[ValidationFeedback]],
    ] | None = None
    #: Maximum validated tasks that may end this entity exploration.
    max_questions: int = 1
    #: Frozen KG snapshot candidates must bind to before validation.
    validation_snapshot_id: str | None = None
    #: Called with each observation, so the caller can persist it.
    on_observation: Callable[[ToolObservation], None] | None = None
    #: Called with each sealed action, for the audit trail.
    on_sealed_action: Callable[[Any], None] | None = None
    #: Called with each policy rejection, for the audit trail.
    on_rejection: Callable[[Any], None] | None = None
    #: Called exactly once per persisted proposal.
    on_planner_step: Callable[[PlannerStep], None] | None = None
    #: Persist every v2 decision, including candidate proposals and stops.
    on_agent_decision: Callable[[int, AgentDecision], None] | None = None
    #: Persist every validator-owned candidate transition.
    on_validation_feedback: Callable[
        [int, ValidationFeedback], None
    ] | None = None
    #: Persist server-prepared skill selection and its terminal outcome.
    on_skill_invocation: Callable[[SkillInvocation], None] | None = None
    on_skill_outcome: Callable[[SkillOutcome], None] | None = None
    model_config_id: str = "unknown-model"
    max_turns: int = 12
    #: The number of consecutive turns without new evidence that stops the run.
    stagnation_limit: int = 2
    #: Consecutive policy refusals that stop a model that cannot correct itself.
    rejection_limit: int = 3


def _canonicalize_open_need_argument(
    arguments: dict[str, Any],
    open_needs: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve a model's dependency digest to the exact open need id."""

    resolved = dict(arguments)
    reference = resolved.get("evidence_need_id")
    if not isinstance(reference, str) or not reference:
        return resolved
    open_need_ids = {
        str(item.get("need_id"))
        for item in open_needs
        if item.get("need_id")
    }
    if reference in open_need_ids:
        return resolved
    digest = reference[5:] if reference.startswith("need.") else reference
    matches = {
        str(item.get("need_id"))
        for item in open_needs
        if item.get("need_id")
        and str(item.get("dependency_digest") or "") == digest
    }
    if len(matches) == 1:
        resolved["evidence_need_id"] = next(iter(matches))
    return resolved


def build_react_graph(
    context: ReactContext,
    *,
    checkpointer: Any = None,
) -> Any:
    """Build the agent graph.

    Every node reads typed state and returns typed state. No node parses free
    text, and no node decides anything the model is allowed to decide.
    """
    if context.entity_planner is not None and context.candidate_assessor is None:
        raise ValueError(
            "entity_planner requires an independent candidate_assessor"
        )

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
            (
                item.get("action_type")
                != ExplorationActionType.QUERY_CTD.value
            )
            and (item.get("new_evidence_ids") or item.get("new_path_ids"))
            for item in state.get("react_observations", [])
        ):
            payload["has_evidence"] = True
        return with_live_edge_budget(RunPolicy(**payload))

    def load_observations(state: EntityGenerationState) -> list[ToolObservation]:
        return [
            ToolObservation.model_validate(item)
            for item in state.get("react_observations", [])
        ]

    def load_exploration_state(
        state: EntityGenerationState,
        policy: RunPolicy,
    ) -> ExplorationState:
        observations = load_observations(state)
        return ExplorationState(
            episode_id=context.episode_id,
            task_id=str(state.get("task_id") or context.run_id),
            seed_entity_id=context.seed_entity_id,
            seed_entity_name=context.seed_entity_name,
            max_questions=context.max_questions,
            candidates=[
                TaskCandidate.model_validate(item)
                for item in state.get("react_candidates", [])
            ],
            candidate_statuses=dict(
                state.get("react_candidate_statuses", {})
            ),
            open_evidence_gaps=list(state.get("react_open_gaps", [])),
            gap_candidate_ids=dict(
                state.get("react_gap_candidate_ids", {})
            ),
            evidence_ids=[
                evidence_id
                for observation in observations
                for evidence_id in observation.new_evidence_ids
            ],
            attempted_action_hashes=list(
                state.get("react_action_hashes", [])
            ),
            budget_state={
                "remaining_tool_calls": policy.remaining_tool_calls,
                "remaining_edge_budget": policy.remaining_edge_budget,
            },
            provider_status=(
                {"kg_snapshot_id": context.validation_snapshot_id}
                if context.validation_snapshot_id
                else {}
            ),
            published_task_ids=list(
                state.get("react_published_task_ids", [])
            ),
        )

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
            react_candidates=[],
            react_candidate_statuses={},
            react_validation_feedback=[],
            react_agent_decisions=[],
            react_open_gaps=[],
            react_gap_candidate_ids={},
            react_published_task_ids=[],
            react_entity_loop_active=False,
            react_sealed_actions=[],
            react_rejections=[],
            react_planner_steps=[],
            react_stagnation_count=0,
            react_rejection_streak=0,
            react_skill_invocations=[],
            react_skill_outcomes=[],
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
        planner_visible_needs = [
            {
                key: value
                for key, value in need.items()
                if key != "dependency_digest"
            }
            for need in live_needs
        ]
        gap_names = [
            str(item.get("information_need") or item.get("need_id"))
            for item in live_needs
            if item
        ]
        gap_names.extend(
            str(item.get("description"))
            for item in state.get("react_open_gaps", [])
            if item.get("description")
        )
        available_actions = list(context.available_action_types)
        available_tools = list(context.available_tools)
        if not live_needs:
            available_actions = [
                item for item in available_actions if item != "retrieve_literature"
            ]
            available_tools = [
                item for item in available_tools if item != "literature_search"
            ]
        skill_runtime = SkillRuntimeContext(
            seed_entity_type=context.seed_entity_type,
            gap_types=[
                str(item.get("gap_type"))
                for item in state.get("react_open_gaps", [])
                if item.get("gap_type")
            ],
            artifact_types=sorted(
                {
                    str(artifact_type)
                    for outcome in state.get("react_skill_outcomes", [])
                    if outcome.get("evidence_gain", 0) > 0
                    for artifact_type in outcome.get(
                        "produced_artifact_types", []
                    )
                }
                | {
                    (
                        "literature_evidence"
                        if observation.action_type
                        is ExplorationActionType.RETRIEVE_LITERATURE
                        else (
                            "ctd_evidence"
                            if observation.action_type
                            is ExplorationActionType.QUERY_CTD
                            else "kg_evidence"
                        )
                    )
                    for observation in observations
                    if observation.new_evidence_ids
                }
                | {
                    "kg_path"
                    for observation in observations
                    if observation.new_path_ids
                }
            ),
            available_tool_names=available_tools,
            allowed_action_types=available_actions,
            remaining_tool_calls=policy.remaining_tool_calls,
            remaining_edge_budget=policy.remaining_edge_budget,
        )
        available_skills = (
            context.skill_registry.available_skills(skill_runtime)
            if context.skill_registry is not None
            else []
        )
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
            open_needs=planner_visible_needs,
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
            available_skills=[
                item.model_dump(mode="json") for item in available_skills
            ],
            remaining_edge_budget=policy.remaining_edge_budget,
            recent_policy_rejections=list(state.get("react_rejections", [])),
        )
        agent_decision: AgentDecision | None = None
        skill_invocation: SkillInvocation | None = None
        try:
            if context.entity_planner is not None:
                exploration = load_exploration_state(state, policy)
                try:
                    agent_decision = context.entity_planner.propose(
                        exploration, planner_context
                    )
                except KeyError:
                    # A configured legacy/scripted model may only know the
                    # action schema. Fall back as one coherent legacy run;
                    # the outer graph will not mix its writer with v2 tasks.
                    configured_responses = getattr(
                        getattr(context.entity_planner, "model", None),
                        "responses",
                        None,
                    )
                    if (
                        state.get("react_entity_loop_active", False)
                        or not isinstance(configured_responses, dict)
                        or "entity_question_exploration_planner"
                        in configured_responses
                    ):
                        raise
                    proposal = context.planner.propose(
                        planner_context, ModelActionProposal
                    )
                    agent_decision = None
                if agent_decision is None:
                    error = None
                else:
                    if agent_decision.state_digest != exploration.state_digest():
                        raise ValueError(
                            "the entity planner returned a stale state digest"
                        )
                if agent_decision is not None and agent_decision.decision == "continue_search":
                    if agent_decision.selected_skill_id is not None:
                        if context.skill_registry is None:
                            raise ValueError(
                                "the planner selected a skill but no skill "
                                "registry is configured"
                            )
                        skill_invocation = (
                            context.skill_registry.prepare_invocation(
                                SkillSelection(
                                    skill_id=agent_decision.selected_skill_id,
                                    arguments=_canonicalize_open_need_argument(
                                        agent_decision.skill_arguments,
                                        live_needs,
                                    ),
                                ),
                                runtime=skill_runtime,
                                episode_id=context.episode_id,
                                turn=turn,
                                state_digest=exploration.state_digest(),
                            )
                        )
                        action_payload = skill_invocation.action.model_dump(
                            mode="json"
                        )
                    elif agent_decision.next_action is not None:
                        action_payload = agent_decision.next_action
                    else:  # protected by AgentDecision validation
                        raise ValueError("continue_search has no action")
                    proposal = ModelActionProposal(
                        action=dict(action_payload),
                        expected_information=(
                            "; ".join(agent_decision.expected_information)
                            or "collect information needed by the candidate"
                        ),
                        evidence_gap=(
                            "; ".join(planner_context.evidence_gaps)
                            or "candidate evidence is incomplete"
                        ),
                    )
                elif agent_decision is not None and agent_decision.decision == "propose_candidate":
                    proposal = ModelActionProposal(
                        action={
                            "action_type": "finish",
                            # This is only a policy-gated handoff to the
                            # independent assessor. The assessor, not this
                            # enum value, decides whether evidence is enough.
                            "stop_reason": StopReason.EVIDENCE_SUFFICIENT.value,
                        },
                        expected_information="independently validate the candidate",
                        evidence_gap="candidate validation has not run",
                    )
                elif agent_decision is not None and agent_decision.decision == "stop":
                    proposal = None
                elif agent_decision is not None:
                    if agent_decision.selected_skill_id is not None:
                        if context.skill_registry is None:
                            raise ValueError(
                                "the planner selected a skill but no skill "
                                "registry is configured"
                            )
                        skill_invocation = (
                            context.skill_registry.prepare_invocation(
                                SkillSelection(
                                    skill_id=agent_decision.selected_skill_id,
                                    arguments=_canonicalize_open_need_argument(
                                        agent_decision.skill_arguments,
                                        live_needs,
                                    ),
                                ),
                                runtime=skill_runtime,
                                episode_id=context.episode_id,
                                turn=turn,
                                state_digest=exploration.state_digest(),
                            )
                        )
                        action_payload = skill_invocation.action.model_dump(
                            mode="json"
                        )
                    elif agent_decision.next_action is not None:
                        action_payload = agent_decision.next_action
                    else:  # protected by AgentDecision validation
                        raise ValueError("abandon_direction has no action")
                    proposal = ModelActionProposal(
                        action=dict(action_payload),
                        expected_information=(
                            "; ".join(agent_decision.expected_information)
                            or "try a different evidence direction"
                        ),
                        evidence_gap="the previous direction was abandoned",
                    )
            else:
                proposal = context.planner.propose(
                    planner_context, ModelActionProposal
                )
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
        payload["_agent_decision"] = (
            agent_decision.model_dump(mode="json")
            if agent_decision is not None
            else None
        )
        payload["_skill_invocation"] = (
            skill_invocation.model_dump(mode="json")
            if skill_invocation is not None
            else None
        )
        if agent_decision is not None:
            payload["react_entity_loop_active"] = True
            decision_log = list(state.get("react_agent_decisions", []))
            if not any(
                int(item.get("turn", -1)) == turn for item in decision_log
            ):
                decision_log.append(
                    {
                        "turn": turn,
                        "decision": agent_decision.model_dump(mode="json"),
                    }
                )
            payload["react_agent_decisions"] = decision_log
            if context.on_agent_decision is not None:
                context.on_agent_decision(turn, agent_decision)
        if agent_decision is not None and agent_decision.candidates:
            candidates = {
                item["candidate_id"]: item
                for item in state.get("react_candidates", [])
            }
            statuses = dict(state.get("react_candidate_statuses", {}))
            for candidate in agent_decision.candidates:
                candidates[candidate.candidate_id] = candidate.model_dump(
                    mode="json"
                )
                statuses.setdefault(candidate.candidate_id, "proposed")
            payload["react_candidates"] = list(candidates.values())
            payload["react_candidate_statuses"] = statuses
        if (
            agent_decision is not None
            and agent_decision.decision == "stop"
        ):
            stop_reason = str(agent_decision.stop_reason)
            candidate_statuses = dict(
                payload.get("react_candidate_statuses", {})
            )
            stop_allowed = {
                "question_targets_met": (
                    len(payload.get("react_published_task_ids", []))
                    >= context.max_questions
                ),
                "all_candidates_terminal": (
                    bool(candidate_statuses)
                    and all(
                        item in {"validated", "rejected"}
                        for item in candidate_statuses.values()
                    )
                ),
                "search_saturated": (
                    int(payload.get("react_stagnation_count", 0))
                    >= context.stagnation_limit
                ),
                "budget_exhausted": (
                    policy.remaining_tool_calls <= 0
                    or (
                        policy.remaining_edge_budget is not None
                        and policy.remaining_edge_budget <= 0
                    )
                    or turn >= context.max_turns
                ),
                "provider_unavailable": any(
                    item.error_code
                    for item in observations
                    if item.status != "success"
                ),
            }
            if not stop_allowed[stop_reason]:
                payload["_proposal_error"] = (
                    f"server rejected premature stop: {stop_reason}"
                )
                payload["stop_reason"] = StopReason.MODEL_ERROR.value
                return payload  # type: ignore[return-value]
            stop_mapping = {
                "question_targets_met": StopReason.TARGET_REACHED.value,
                "all_candidates_terminal": StopReason.ALL_CANDIDATES_TERMINAL.value,
                "search_saturated": StopReason.NO_NEW_EVIDENCE.value,
                "budget_exhausted": StopReason.BUDGET_EXHAUSTED.value,
                "provider_unavailable": StopReason.PROVIDER_ERROR.value,
            }
            payload["stop_reason"] = stop_mapping[stop_reason]
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
                available_skills=list(planner_context.available_skills),
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
        if outcome.error_code is None:
            action_hashes.append(sealed.canonical_action_hash)
        payload["react_action_hashes"] = action_hashes
        policy = load_policy(state)
        if outcome.error_code is None:
            policy = policy.with_execution(sealed.canonical_action_hash)
        if outcome.new_evidence_ids or outcome.new_path_ids:
            if sealed.action_type in (
                ExplorationActionType.QUERY_NEIGHBORS,
                ExplorationActionType.EXPAND_ENTITY,
                ExplorationActionType.RETRIEVE_LITERATURE,
            ):
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
        skill_payload = payload.get("_skill_invocation")
        if skill_payload is not None:
            invocation = SkillInvocation.model_validate(skill_payload)
            invocations = list(state.get("react_skill_invocations", []))
            if all(
                item.get("invocation_id") != invocation.invocation_id
                for item in invocations
            ):
                invocations.append(invocation.model_dump(mode="json"))
            payload["react_skill_invocations"] = invocations
            if context.on_skill_invocation is not None:
                context.on_skill_invocation(invocation)
            rejection_payload = payload.get("_rejection")
            observation_payload = (payload.get("_outcome") or {}).get(
                "observation"
            )
            if rejection_payload is not None:
                skill_outcome = SkillOutcome(
                    invocation_id=invocation.invocation_id,
                    episode_id=context.episode_id,
                    status="rejected",
                    summary=str(
                        rejection_payload.get("message")
                        or "the skill invocation was rejected by policy"
                    ),
                    error_code=str(rejection_payload.get("code") or "rejected"),
                )
            elif observation_payload is not None:
                observation = ToolObservation.model_validate(
                    observation_payload
                )
                new_ids = list(observation.new_evidence_ids)
                mapped_status = {
                    "success": "success",
                    "empty": "warning",
                    "warning": "warning",
                    "error": "error",
                    "rejected": "rejected",
                    None: "error",
                }[observation.status]
                new_paths = list(observation.new_path_ids)
                artifact_ids = list(dict.fromkeys([*new_ids, *new_paths]))
                if mapped_status == "success" and not artifact_ids:
                    mapped_status = "warning"
                skill_outcome = SkillOutcome(
                    invocation_id=invocation.invocation_id,
                    episode_id=context.episode_id,
                    status=mapped_status,
                    summary=(
                        f"skill produced {len(new_ids)} evidence artifacts"
                        if mapped_status == "success"
                        else observation.message
                        or observation.error_code
                        or "skill execution did not produce usable evidence"
                    ),
                    artifact_ids=artifact_ids,
                    evidence_artifact_ids=new_ids,
                    path_artifact_ids=new_paths,
                    produced_artifact_types=(
                        list(invocation.produced_artifact_types)
                        if artifact_ids
                        else []
                    ),
                    evidence_gain=len(artifact_ids),
                    error_code=observation.error_code,
                )
            else:
                skill_outcome = SkillOutcome(
                    invocation_id=invocation.invocation_id,
                    episode_id=context.episode_id,
                    status="error",
                    summary="the skill invocation produced no observation",
                    error_code="missing_observation",
                )
            outcomes = list(state.get("react_skill_outcomes", []))
            if all(
                item.get("invocation_id") != invocation.invocation_id
                for item in outcomes
            ):
                outcomes.append(skill_outcome.model_dump(mode="json"))
            payload["react_skill_outcomes"] = outcomes
            if context.on_skill_outcome is not None:
                context.on_skill_outcome(skill_outcome)
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
                    rejection_message=rejection_payload.get("message"),
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
                    skill_id=(
                        skill_payload.get("skill_id")
                        if skill_payload is not None
                        else None
                    ),
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
                if (
                    context.candidate_assessor is not None
                    and (
                        context.entity_planner is None
                        or state.get("react_entity_loop_active", False)
                    )
                ):
                    try:
                        raw_candidates, raw_feedback = context.candidate_assessor(
                            observations, state
                        )
                        candidates = [
                            TaskCandidate.model_validate(item)
                            for item in raw_candidates
                        ]
                        feedback_items = [
                            ValidationFeedback.model_validate(item)
                            for item in raw_feedback
                        ]
                    except Exception as exc:
                        payload["stop_reason"] = StopReason.FATAL_ERROR.value
                        errors = list(state.get("errors", []))
                        errors.append(
                            {
                                "node": Route.ASSESS,
                                "code": "candidate_assessment_failed",
                                "message": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        payload["errors"] = errors
                        return payload  # type: ignore[return-value]

                    by_id = {
                        item.candidate_id: item
                        for item in (
                            TaskCandidate.model_validate(raw)
                            for raw in state.get("react_candidates", [])
                        )
                    }
                    for candidate in candidates:
                        by_id[candidate.candidate_id] = candidate
                    statuses = dict(state.get("react_candidate_statuses", {}))
                    for candidate in candidates:
                        statuses.setdefault(candidate.candidate_id, "proposed")
                    base = load_exploration_state(state, policy)
                    exploration = base.model_copy(
                        update={
                            "candidates": list(by_id.values()),
                            "candidate_statuses": statuses,
                        }
                    )
                    for feedback in feedback_items:
                        exploration = apply_validation_feedback(
                            exploration, feedback
                        )
                        if context.on_validation_feedback is not None:
                            context.on_validation_feedback(turn, feedback)

                    payload["react_candidates"] = [
                        item.model_dump(mode="json")
                        for item in exploration.candidates
                    ]
                    payload["react_candidate_statuses"] = dict(
                        exploration.candidate_statuses
                    )
                    payload["react_open_gaps"] = [
                        item.model_dump(mode="json")
                        for item in exploration.open_evidence_gaps
                    ]
                    payload["react_gap_candidate_ids"] = dict(
                        exploration.gap_candidate_ids
                    )
                    payload["react_published_task_ids"] = list(
                        exploration.published_task_ids
                    )
                    feedback_log = list(
                        state.get("react_validation_feedback", [])
                    )
                    feedback_log.extend(
                        item.model_dump(mode="json") for item in feedback_items
                    )
                    payload["react_validation_feedback"] = feedback_log

                    if (
                        len(exploration.published_task_ids)
                        >= exploration.max_questions
                    ):
                        payload["stop_reason"] = (
                            StopReason.EVIDENCE_SUFFICIENT.value
                        )
                        return payload  # type: ignore[return-value]
                    # A recoverable validation gap is now part of the next
                    # planner observation. A finish proposal alone is never a
                    # certificate, and a rejected candidate does not prove the
                    # entity has no other viable question, so exploration
                    # resumes.
                    return payload  # type: ignore[return-value]
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
