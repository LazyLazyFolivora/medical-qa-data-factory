"""Bounded semantic decisions for the objective-question main agent."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field

from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
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


class MainAgentDecisionProposal(ContractModel):
    action: str = Field(min_length=1)
    target_subagent_role: str | None = None
    input_artifact_ids: list[str] = Field(default_factory=list)
    requested_budget: dict[str, Any] = Field(default_factory=dict)
    expected_information_gain: float = Field(ge=0, le=1)
    action_parameters: dict[str, Any] = Field(default_factory=dict)
    stop_reason: str | None = None


class StructuredMainAgent:
    """Structured model adapter; the host stamps state identity."""

    def __init__(self, model: Any, *, provider_ids: list[str]) -> None:
        self.model = model
        self.provider_ids = list(provider_ids)

    def decide(self, snapshot: ObjectiveRuntimeSnapshot) -> MainAgentDecision:
        if snapshot.goal is None:
            raise ValueError("objective runtime has no generation goal")
        artifact_summaries = [
            {
                "artifact_id": artifact.artifact_id,
                "artifact_type": artifact.artifact_type,
                "producer_role": artifact.producer_role,
                "metadata": artifact.metadata,
            }
            for artifact in sorted(
                snapshot.artifacts.values(),
                key=lambda item: item.artifact_id,
            )
        ]
        state_digest = canonical_digest(snapshot.model_dump(mode="json"))
        payload = {
            "generation_goal": snapshot.goal.model_dump(mode="json"),
            "run_state": snapshot.run_state.value,
            "hypothesis_states": {
                key: value.value for key, value in snapshot.hypotheses.items()
            },
            "artifact_summaries": artifact_summaries,
            "artifact_counts": _artifact_counts(snapshot),
            "available_providers": list(self.provider_ids),
            "retrieval_history": _retrieval_history(snapshot),
            "budget": {
                **snapshot.budget.model_dump(mode="json"),
                "remaining_cost": max(
                    snapshot.budget.max_cost - snapshot.budget.charged_cost,
                    0,
                ),
                "remaining_model_calls": max(
                    snapshot.budget.max_model_calls
                    - snapshot.budget.used_model_calls,
                    0,
                ),
            },
            "available_actions": [
                {
                    "action": action.value,
                    "target_subagent_role": _ROLE_BY_ACTION.get(action),
                }
                for action in sorted(
                    _allowed_actions(snapshot), key=lambda item: item.value
                )
            ],
            "action_protocol": {
                "retrieve_materials": {
                    "provider_id": "exact configured provider id",
                    "purpose": "discovery or verification",
                    "query": {
                        "primekg": {
                            "mention": "public entity name",
                            "native_id": "optional provider id",
                        },
                        "pubmed": {"term": "focused biomedical search query"},
                    },
                }
            },
            "decision_rules": [
                "Choose one action only.",
                "Retrieval may be repeated whenever evidence is insufficient.",
                "After an empty retrieval, broaden or shorten the query or change provider.",
                "After three consecutive retrievals with no new material, stop rather than spin.",
                "Do not verify until independent verification materials exist.",
                "Do not realize a question without a frozen contract.",
                "Do not review release without a question draft and verified contract.",
                "You cannot publish; deterministic code owns release.",
                "Stop when the requested upper bound is reached or no objective candidate remains.",
            ],
        }
        response = self.model.generate_structured(
            StructuredGenerationRequest(
                invocation_id=f"main-agent.{state_digest[:16]}",
                role="objective_main_agent",
                system_prompt_version="objective-main-agent-v1",
                payload=payload,
                idempotency_key=f"{snapshot.run_id}|main|{state_digest}",
            ),
            MainAgentDecisionProposal,
        )
        proposal = MainAgentDecisionProposal.model_validate(response.parsed)
        action_parameters = _normalize_action_parameters(
            proposal.action,
            proposal.action_parameters,
            provider_ids=self.provider_ids,
            target_entity=snapshot.goal.target_entity,
            target_entity_id=snapshot.goal.target_entity_id,
        )
        return MainAgentDecision(
            decision_id=f"decision.{state_digest[:20]}",
            action=proposal.action,
            target_subagent_role=proposal.target_subagent_role,
            input_artifact_ids=proposal.input_artifact_ids,
            state_digest=state_digest,
            requested_budget=proposal.requested_budget,
            expected_information_gain=proposal.expected_information_gain,
            action_parameters=action_parameters,
            stop_reason=proposal.stop_reason,
        )


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
    artifacts = list(snapshot.artifacts.values())
    hypothesis_states = set(snapshot.hypotheses.values())
    common = {
        MainAgentAction.RETRIEVE_MATERIALS,
        MainAgentAction.REQUEST_MORE_EVIDENCE,
        MainAgentAction.STOP,
    }
    types = {artifact.artifact_type for artifact in artifacts}
    if (
        "evidence_material" in types
        and "evidence_anchor" in types
        and "candidate_claim" not in types
    ):
        common.add(MainAgentAction.UNDERSTAND_FACTS)
    if "candidate_claim" in types and "question_hypothesis" not in types:
        common.add(MainAgentAction.DESIGN_QUESTIONS)
    if (
        "question_hypothesis" in types
        and "answer_resolution" not in types
        and any(
            artifact.artifact_type == "provider_result"
            and artifact.metadata.get("purpose") == "verification"
            and artifact.payload.get("terminal_status") == "SUCCEEDED"
            for artifact in artifacts
        )
    ):
        common.add(MainAgentAction.VERIFY_ANSWER)
    if (
        "frozen_question_contract" in types
        and HypothesisState.ANSWER_VERIFIED in hypothesis_states
        and "objective_question_draft" not in types
    ):
        common.add(MainAgentAction.REALIZE_QUESTION)
    if (
        "objective_question_draft" in types
        and "frozen_question_contract" in types
        and "answer_resolution" in types
        and HypothesisState.QUESTION_REALIZED in hypothesis_states
        and "objective_validation_report" not in types
    ):
        common.add(MainAgentAction.REVIEW_RELEASE)
    return common


def _is_terminal(snapshot: ObjectiveRuntimeSnapshot) -> bool:
    return snapshot.run_state in {
        RunState.COMPLETED,
        RunState.COMPLETED_WITH_ZERO_QUESTIONS,
        RunState.PROVIDER_FAILED,
        RunState.RECOVERY_REQUIRED,
        RunState.VERSION_INCOMPATIBLE,
        RunState.CANCELLED,
    }


def _artifact_counts(snapshot: ObjectiveRuntimeSnapshot) -> dict[str, int]:
    counts: dict[str, int] = {}
    for artifact in snapshot.artifacts.values():
        counts[artifact.artifact_type] = (
            counts.get(artifact.artifact_type, 0) + 1
        )
    return dict(sorted(counts.items()))


def _normalize_action_parameters(
    action: str,
    parameters: dict[str, Any],
    *,
    provider_ids: list[str],
    target_entity: str,
    target_entity_id: str | None,
) -> dict[str, Any]:
    normalized = dict(parameters)
    if action not in {
        MainAgentAction.RETRIEVE_MATERIALS.value,
        MainAgentAction.REQUEST_MORE_EVIDENCE.value,
    }:
        return normalized
    query = normalized.get("query")
    if isinstance(query, dict):
        selected_provider = normalized.get("provider_id")
        if (
            selected_provider in provider_ids
            and isinstance(query.get(selected_provider), dict)
        ):
            normalized["query"] = dict(query[selected_provider])
            return normalized
        for provider_id in provider_ids:
            nested = query.get(provider_id)
            if isinstance(nested, dict):
                normalized["provider_id"] = provider_id
                normalized["query"] = dict(nested)
                break
    purpose = str(normalized.get("purpose", "discovery"))
    if not normalized.get("provider_id"):
        if purpose == "verification" and "pubmed" in provider_ids:
            normalized["provider_id"] = "pubmed"
        elif provider_ids:
            normalized["provider_id"] = provider_ids[0]
    provider_id = normalized.get("provider_id")
    if not isinstance(normalized.get("query"), dict):
        if provider_id == "primekg":
            normalized["query"] = {
                "mention": target_entity,
                "native_id": target_entity_id,
            }
        elif provider_id == "pubmed":
            normalized["query"] = {
                # Keep fallback queries short. Explanatory instructions belong
                # in the agent context, not in a provider search expression.
                "term": target_entity,
            }
    return normalized


def _retrieval_history(
    snapshot: ObjectiveRuntimeSnapshot,
) -> list[dict[str, Any]]:
    history = []
    for artifact in snapshot.artifacts.values():
        if artifact.artifact_type != "provider_result":
            continue
        payload = artifact.payload
        request = payload.get("request", {})
        history.append(
            {
                "provider_id": request.get("provider_id"),
                "purpose": artifact.metadata.get("purpose"),
                "query": request.get("query"),
                "terminal_status": payload.get("terminal_status"),
                "material_count": len(payload.get("material_ids", [])),
                "error_category": payload.get("error_category"),
            }
        )
    return history
