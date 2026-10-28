"""The entity planner binds decisions to server-owned exploration state."""
from __future__ import annotations

from typing import Any

from medical_qa_data_factory.generation.entity_planner import (
    StructuredEntityQuestionPlanner,
)
from medical_qa_data_factory.generation.exploration import ExplorationState
from medical_qa_data_factory.generation.planner import PlannerContext
from medical_qa_data_factory.llm.contracts import StructuredGenerationResult


class _RecordingModel:
    def __init__(self, parsed: dict[str, Any]) -> None:
        self.parsed = parsed
        self.requests: list[Any] = []

    def generate_structured(self, request: Any, response_model: type[Any]):
        self.requests.append(request)
        return StructuredGenerationResult(
            invocation_id=request.invocation_id,
            role=request.role,
            parsed=self.parsed,
            usage={},
            model_config_id="test-model",
        )


def _state() -> ExplorationState:
    return ExplorationState(
        episode_id="episode-1",
        task_id="task-1",
        seed_entity_id="primekg:gene/protein:9240",
        seed_entity_name="PNMA1",
    )


def _context() -> PlannerContext:
    return PlannerContext(
        run_id="episode-1",
        episode_id="episode-1",
        turn=1,
        seed_entity_id="primekg:gene/protein:9240",
        seed_entity_name="PNMA1",
    )


def test_planner_binds_the_server_state_digest() -> None:
    state = _state()
    model = _RecordingModel(
        {
            "decision": "stop",
            "state_digest": "model-invented-digest",
            "stop_reason": "search_saturated",
        }
    )

    decision = StructuredEntityQuestionPlanner(model).propose(state, _context())

    assert decision.state_digest == state.state_digest()
    assert model.requests[0].payload["exploration_state"] == state.model_dump(
        mode="json"
    )
