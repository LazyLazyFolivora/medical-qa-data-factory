"""Bounded semantic decisions for the objective-question main agent."""
from __future__ import annotations

from enum import Enum

from .objective_question_contracts import (
    HypothesisState,
    MainAgentDecision,
    RunState,
    canonical_digest,
)
from .objective_question_runtime import ObjectiveRuntimeSnapshot


class MainAgentAction(str, Enum):
    RETRIEVE_MATERIALS = "retrieve_materials"
    UNDERSTAND_FACTS = "understand_facts"
    DESIGN_QUESTIONS = "design_questions"
    VERIFY_ANSWER = "verify_answer"
    REALIZE_QUESTION = "realize_question"
    REVIEW_RELEASE = "review_release"
    REQUEST_MORE_EVIDENCE = "request_more_evidence"
    STOP = "stop"


class IllegalMainAgentAction(ValueError):
    code = "illegal_main_agent_action"


_ROLE_BY_ACTION = {
    MainAgentAction.RETRIEVE_MATERIALS: "material_retrieval",
    MainAgentAction.UNDERSTAND_FACTS: "fact_understanding",
    MainAgentAction.DESIGN_QUESTIONS: "objective_question_design",
    MainAgentAction.VERIFY_ANSWER: "objective_answer_verification",
    MainAgentAction.REALIZE_QUESTION: "objective_question_realization",
    MainAgentAction.REVIEW_RELEASE: "objective_release_review",
    MainAgentAction.REQUEST_MORE_EVIDENCE: "material_retrieval",
}


def validate_main_agent_decision(
    snapshot: ObjectiveRuntimeSnapshot,
    decision: MainAgentDecision,
) -> None:
    current_digest = canonical_digest(snapshot.model_dump(mode="json"))
    if decision.state_digest != current_digest:
        raise IllegalMainAgentAction("main agent decision uses stale state")
    if _is_terminal(snapshot):
        raise IllegalMainAgentAction("run is terminal and accepts no new action")
    try:
        action = MainAgentAction(decision.action)
    except ValueError as exc:
        raise IllegalMainAgentAction(
            f"unknown main agent action: {decision.action}"
        ) from exc
    allowed = _allowed_actions(snapshot)
    if action not in allowed:
        raise IllegalMainAgentAction(
            f"action {action.value.upper()} is illegal in {snapshot.run_state.value}"
        )
    if action is MainAgentAction.STOP:
        if decision.target_subagent_role is not None:
            raise IllegalMainAgentAction("STOP cannot target a subagent")
        return
    expected_role = _ROLE_BY_ACTION[action]
    if decision.target_subagent_role != expected_role:
        raise IllegalMainAgentAction(
            f"target_subagent_role must be {expected_role!r}"
        )


def _allowed_actions(
    snapshot: ObjectiveRuntimeSnapshot,
) -> set[MainAgentAction]:
    hypothesis_states = set(snapshot.hypotheses.values())
    if HypothesisState.QUESTION_REALIZED in hypothesis_states:
        return {MainAgentAction.REVIEW_RELEASE, MainAgentAction.STOP}
    if HypothesisState.ANSWER_VERIFIED in hypothesis_states:
        return {MainAgentAction.REALIZE_QUESTION, MainAgentAction.STOP}
    if snapshot.run_state is RunState.CREATED:
        return {MainAgentAction.RETRIEVE_MATERIALS, MainAgentAction.STOP}
    if snapshot.run_state is RunState.MATERIALS_COLLECTED:
        return {
            MainAgentAction.UNDERSTAND_FACTS,
            MainAgentAction.REQUEST_MORE_EVIDENCE,
            MainAgentAction.STOP,
        }
    if snapshot.run_state is RunState.CLAIMS_PROPOSED:
        return {MainAgentAction.DESIGN_QUESTIONS, MainAgentAction.STOP}
    if snapshot.run_state in {
        RunState.HYPOTHESES_PROPOSED,
        RunState.CANDIDATES_PROCESSING,
    }:
        return {
            MainAgentAction.VERIFY_ANSWER,
            MainAgentAction.REQUEST_MORE_EVIDENCE,
            MainAgentAction.STOP,
        }
    return {MainAgentAction.STOP}


def _is_terminal(snapshot: ObjectiveRuntimeSnapshot) -> bool:
    return snapshot.run_state in {
        RunState.COMPLETED,
        RunState.COMPLETED_WITH_ZERO_QUESTIONS,
        RunState.PROVIDER_FAILED,
        RunState.RECOVERY_REQUIRED,
        RunState.VERSION_INCOMPATIBLE,
        RunState.CANCELLED,
    }
