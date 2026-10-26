"""What the planner sees, and what it may propose.

Step 5 of the real-generation-agent plan. The context is compressed and
structured; the model proposes and never executes.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from medical_qa_data_factory.generation.actions import (
    ModelActionProposal,
    RunPolicy,
)
from medical_qa_data_factory.generation.contracts import ToolObservation
from medical_qa_data_factory.generation.planner import (
    PlannerContext,
    StructuredModelPlanner,
    build_planner_context,
)


def _observation(
    observation_id: str = "obs-1", **overrides: Any
) -> ToolObservation:
    payload: dict[str, Any] = {
        "observation_id": observation_id,
        "episode_id": "ep1",
        "tool_name": "kg_neighbors",
        "operation_key": "op-1",
        "request_digest": "digest",
        "row_count": 5,
        "elapsed_ms": 10,
        "action_type": "query_neighbors",
        "status": "success",
        "rows_examined": 5,
    }
    payload.update(overrides)
    return ToolObservation(**payload)


def _context(**overrides: Any) -> PlannerContext:
    payload: dict[str, Any] = {
        "run_id": "run1",
        "episode_id": "ep1",
        "turn": 1,
        "seed_entity_id": "drug:aspirin",
        "seed_entity_name": "aspirin",
        "known_entities": [
            {"canonical_id": "drug:aspirin", "name": "aspirin"},
            {"canonical_id": "disease:headache", "name": "headache"},
        ],
        "observations": [
            {
                "observation_id": "obs-1",
                "action_type": "query_neighbors",
                "status": "success",
                "rows_examined": 5,
                "truncated": False,
                "new_evidence_ids": [],
                "discovered_entity_ids": [],
                "error_code": None,
            }
        ],
        "open_needs": [
            {
                "need_id": "need.1",
                "hop_position": 0,
                "information_need": "aspirin treatment headache",
            }
        ],
        "evidence_gaps": ["the indication set is unknown"],
        "executed_action_hashes": ["hash-1"],
        "available_tools": ["kg_neighbors", "literature_search"],
        "remaining_tool_calls": 8,
        "max_hops": 3,
    }
    payload.update(overrides)
    return PlannerContext(**payload)


# ---- the context ---------------------------------------------------------


def test_the_context_carries_the_seed_identity() -> None:
    context = _context()
    assert context.seed_entity_id == "drug:aspirin"
    assert context.seed_entity_name == "aspirin"


def test_the_context_carries_the_observations() -> None:
    context = _context()
    assert len(context.observations) == 1
    assert context.observations[0]["status"] == "success"
    assert context.observations[0]["rows_examined"] == 5


def test_the_context_carries_the_open_needs() -> None:
    context = _context()
    assert context.open_needs[0]["need_id"] == "need.1"


def test_the_context_carries_the_remaining_budget() -> None:
    context = _context()
    assert context.remaining_tool_calls == 8
    assert context.max_hops == 3
    assert context.max_results_per_query == 50
    assert context.remaining_edge_budget is None


def test_the_context_carries_query_limits_and_policy_feedback() -> None:
    context = _context(
        max_results_per_query=12,
        remaining_edge_budget=7,
        recent_policy_rejections=[
            {
                "turn": 1,
                "action_type": "query_neighbors",
                "code": "limit_too_large",
                "message": "max_results 100 exceeds the policy limit 12",
                "irrelevant_detail": "must not be forwarded",
            }
        ],
    )
    assert context.max_results_per_query == 12
    assert context.remaining_edge_budget == 7
    assert context.recent_policy_rejections == [
        {
            "turn": 1,
            "action_type": "query_neighbors",
            "code": "limit_too_large",
            "message": "max_results 100 exceeds the policy limit 12",
        }
    ]


def test_the_context_carries_no_secret() -> None:
    """Nothing in the context may be a credential or a connection string."""
    text = json.dumps(_context().model_dump(mode="json"))
    for forbidden in ("api_key", "password", "secret", "sqlite", "X-API-Key"):
        assert forbidden.lower() not in text.lower()


def test_the_context_is_capped() -> None:
    many = [
        {"canonical_id": f"e{i}", "name": f"e{i}"} for i in range(200)
    ]
    observations = [_observation(f"obs-{i}") for i in range(100)]
    context = build_planner_context(
        run_id="run1",
        episode_id="ep1",
        turn=1,
        seed_entity_id="drug:aspirin",
        seed_entity_name="aspirin",
        known_entities=many,
        observations=observations,
        open_needs=[],
        evidence_gaps=[],
        executed_action_hashes=[],
        policy=RunPolicy(remaining_tool_calls=3),
        available_tools=["kg_neighbors"],
    )
    assert len(context.known_entities) <= 50
    assert len(context.observations) <= 20


def test_the_context_is_json_serialisable() -> None:
    payload = _context().model_dump(mode="json")
    json.dumps(payload)


# ---- the planner ---------------------------------------------------------


class _RecordingModel:
    def __init__(self, parsed: dict[str, Any]):
        self.parsed = parsed
        self.requests: list[Any] = []
        self.response_models: list[type[Any]] = []

    def generate_structured(self, request: Any, response_model: type[Any]):
        self.requests.append(request)
        self.response_models.append(response_model)
        from medical_qa_data_factory.llm.contracts import (
            StructuredGenerationResult,
        )

        return StructuredGenerationResult(
            invocation_id=request.invocation_id,
            role=request.role,
            parsed=self.parsed,
            usage={},
            model_config_id="test-model",
        )


def test_the_planner_asks_the_model_for_one_action() -> None:
    model = _RecordingModel(
        {
            "action": {
                "action_type": "query_neighbors",
                "subject_entity_id": "drug:aspirin",
                "direction": "outgoing",
                "max_results": 10,
            },
            "expected_information": "the diseases aspirin treats",
            "evidence_gap": "the indication set is unknown",
        }
    )
    planner = StructuredModelPlanner(model)
    proposal = planner.propose(_context(), ModelActionProposal)
    assert proposal.action["action_type"] == "query_neighbors"
    assert proposal.expected_information
    schema = model.response_models[0].model_json_schema()
    action_schema = schema["properties"]["action"]
    assert "oneOf" in action_schema
    assert "discriminator" in action_schema


def test_the_planner_uses_the_explorer_role() -> None:
    model = _RecordingModel(
        {
            "action": {
                "action_type": "finish",
                "stop_reason": "evidence_sufficient",
            },
            "expected_information": "x",
            "evidence_gap": "y",
        }
    )
    planner = StructuredModelPlanner(model)
    planner.propose(_context(), ModelActionProposal)
    request = model.requests[0]
    assert request.role == "exploration_planner"
    assert request.system_prompt_version == "explorer-v2"


def test_the_planner_passes_the_context_as_the_payload() -> None:
    model = _RecordingModel(
        {
            "action": {
                "action_type": "finish",
                "stop_reason": "evidence_sufficient",
            },
            "expected_information": "x",
            "evidence_gap": "y",
        }
    )
    planner = StructuredModelPlanner(model)
    planner.propose(_context(), ModelActionProposal)
    payload = model.requests[0].payload
    assert payload["seed_entity_id"] == "drug:aspirin"
    assert "open_needs" in payload
    assert payload["max_results_per_query"] == 50
    assert payload["remaining_edge_budget"] is None


def test_the_planner_never_executes_a_tool() -> None:
    """The planner's only output is a proposal."""
    model = _RecordingModel(
        {
            "action": {
                "action_type": "query_neighbors",
                "subject_entity_id": "drug:aspirin",
                "direction": "outgoing",
                "max_results": 10,
            },
            "expected_information": "x",
            "evidence_gap": "y",
        }
    )
    planner = StructuredModelPlanner(model)
    proposal = planner.propose(_context(), ModelActionProposal)
    # A proposal is inert data. It carries no tool name, no call and no
    # execution handle.
    assert not hasattr(proposal, "execute")
    assert not hasattr(proposal, "tool_name")
    assert "tool_name" not in json.dumps(proposal.model_dump(mode="json"))
