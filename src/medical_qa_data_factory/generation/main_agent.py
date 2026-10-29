"""Bounded semantic decisions for the objective-question main agent."""
from __future__ import annotations

from enum import Enum
from typing import Any
from dataclasses import asdict

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
from .fact_source_policy import (
    objective_fact_source_route,
    objective_fact_source_routes,
)


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
            "fact_source_routes": {
                fact_type: asdict(route)
                for fact_type, route in objective_fact_source_routes(
                    snapshot.goal.allowed_fact_types
                ).items()
            },
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
                "Use the fact_source_routes to choose the primary source and a different structured verifier for each fact type.",
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
            fact_types=snapshot.goal.allowed_fact_types,
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
    fact_types: list[str],
) -> dict[str, Any]:
    normalized = dict(parameters)
    if action not in {
        MainAgentAction.RETRIEVE_MATERIALS.value,
        MainAgentAction.REQUEST_MORE_EVIDENCE.value,
    }:
        return normalized
    purpose = str(normalized.get("purpose", "discovery"))
    if not normalized.get("provider_id"):
        route_lists = [
            (
                objective_fact_source_route(fact_type).verification_providers
                if purpose == "verification"
                else objective_fact_source_route(fact_type).primary_providers
            )
            for fact_type in fact_types
        ]
        preferred = [
            provider_id
            for providers in route_lists
            for provider_id in providers
            if provider_id in provider_ids
        ]
        normalized["provider_id"] = (
            preferred[0]
            if preferred
            else "pubmed"
            if purpose == "verification" and "pubmed" in provider_ids
            else provider_ids[0]
        )
    provider_id = normalized.get("provider_id")
    query = normalized.get("query")
    if isinstance(query, dict) and isinstance(query.get(provider_id), dict):
        query = dict(query[provider_id])
    elif isinstance(query, dict) and not _query_has_provider_fields(
        provider_id, query
    ):
        nested_values = [
            value
            for key, value in query.items()
            if key in provider_ids and isinstance(value, dict)
        ]
        query = dict(nested_values[0]) if nested_values else {}
    if not isinstance(query, dict):
        query = {}
    if provider_id == "primekg":
        normalized["query"] = {
            "mention": str(
                query.get("mention")
                or query.get("symbol")
                or target_entity
            ),
            "native_id": query.get("native_id") or target_entity_id,
        }
    elif provider_id in {"hgnc", "ncbi_gene", "ensembl"}:
        normalized["query"] = {
            "symbol": str(
                query.get("symbol")
                or query.get("mention")
                or query.get("entity_name")
                or target_entity
            ),
            "native_id": query.get("native_id") or target_entity_id,
        }
    elif provider_id == "pubmed":
        normalized["query"] = {
            # Keep fallback queries short. Explanatory instructions belong
            # in the agent context, not in a provider search expression.
            "term": str(
                query.get("term")
                if query.get("term")
                and len(str(query["term"])) <= 160
                else target_entity
            ),
        }
    else:
        normalized["query"] = query
    return normalized


def _query_has_provider_fields(provider_id: str, query: dict[str, Any]) -> bool:
    fields = {
        "primekg": {"mention", "native_id"},
        "hgnc": {"symbol", "mention", "entity_name", "native_id"},
        "ncbi_gene": {"symbol", "mention", "entity_name", "native_id"},
        "ensembl": {"symbol", "mention", "entity_name", "native_id"},
        "pubmed": {"term", "mention"},
        "ctd": {"chemical_id", "native_id"},
    }.get(provider_id, set())
    return bool(set(query) & fields)


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
