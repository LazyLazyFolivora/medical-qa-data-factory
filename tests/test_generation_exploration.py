"""The exploration loop end to end.

Step 5 and Step 6 of the real-generation-agent plan: a model proposes, the
policy gate decides, the executor runs, and the observation comes back to the
planner. A refused proposal produces zero tool calls and does not end the run.
"""
from __future__ import annotations

from typing import Any

import pytest

from medical_qa_data_factory.generation.actions import ModelActionProposal, RunPolicy
from medical_qa_data_factory.generation.contracts import StopReason
from medical_qa_data_factory.generation.exploration import ExplorationLoop
from medical_qa_data_factory.generation.executor import Executor
from medical_qa_data_factory.generation.planner import PlannerContext


class _ScriptedPlanner:
    """Returns one proposal per turn, or raises to simulate a model error."""

    def __init__(self, proposals: list[Any]):
        self.proposals = list(proposals)
        self.contexts: list[PlannerContext] = []

    def propose(self, context: PlannerContext, response_model: type[Any]):
        self.contexts.append(context)
        if not self.proposals:
            raise RuntimeError("no more proposals")
        item = self.proposals.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _neighbor_proposal(entity_id: str = "drug:aspirin", **overrides: Any):
    action: dict[str, Any] = {
        "action_type": "query_neighbors",
        "subject_entity_id": entity_id,
        "direction": "outgoing",
        "max_results": 10,
    }
    action.update(overrides)
    return ModelActionProposal(
        action=action,
        expected_information="the neighbours of the seed",
        evidence_gap="the neighbour set is unknown",
    )


def _finish_proposal():
    return ModelActionProposal(
        action={"action_type": "finish", "stop_reason": "evidence_sufficient"},
        expected_information="the evidence is enough",
        evidence_gap="none",
    )


class _Edge:
    def __init__(self, head: str, tail: str, edge_id: str | None = None):
        self.head = type("E", (), {"canonical_id": head})()
        self.tail = type("E", (), {"canonical_id": tail})()
        self.edge_id = edge_id or f"e-{head}-{tail}"


class _KG:
    def __init__(self, edges: list[_Edge] | None = None):
        self.edges = edges if edges is not None else [_Edge("drug:aspirin", "disease:headache")]
        self.calls: list[dict[str, Any]] = []

    def neighbors(self, request: dict[str, Any]):
        self.calls.append(request)
        return self.edges, "trace-1"


def _loop(
    planner: _ScriptedPlanner,
    kg: _KG | None = None,
    sufficiency: Any = None,
    **policy_overrides: Any,
) -> ExplorationLoop:
    payload: dict[str, Any] = {
        "has_evidence": True,
        "available_tool_names": ("kg_neighbors", "literature_search", "none"),
    }
    payload.update(policy_overrides)
    return ExplorationLoop(
        planner=planner,
        executor=Executor(kg=kg if kg is not None else _KG()),
        policy=RunPolicy(**payload),
        run_id="run1",
        episode_id="ep1",
        seed_entity_id="drug:aspirin",
        seed_entity_name="aspirin",
        available_tools=["kg_neighbors", "literature_search"],
        sufficiency=sufficiency,
    )


# ---- a normal run --------------------------------------------------------


def test_a_proposal_is_executed_and_the_observation_comes_back() -> None:
    kg = _KG()
    planner = _ScriptedPlanner([_neighbor_proposal(), _finish_proposal()])
    result = _loop(planner, kg).run()
    assert len(kg.calls) == 1
    assert result.observations
    assert result.observations[0].action_type == "query_neighbors"
    assert result.stop_reason is StopReason.EVIDENCE_SUFFICIENT


def test_the_observation_feeds_back_into_the_next_context() -> None:
    planner = _ScriptedPlanner([_neighbor_proposal(), _finish_proposal()])
    _loop(planner).run()
    assert len(planner.contexts) == 2
    second = planner.contexts[1]
    assert second.observations
    assert second.observations[0]["status"] == "success"
    assert second.known_entities
    assert "disease:headache" in {
        item["canonical_id"] for item in second.known_entities
    }


def test_the_discovered_entities_become_known_for_the_next_turn() -> None:
    planner = _ScriptedPlanner([_neighbor_proposal(), _finish_proposal()])
    loop = _loop(planner)
    loop.run()
    assert "disease:headache" in loop.known_entity_ids


def test_a_model_error_stops_the_run() -> None:
    planner = _ScriptedPlanner([RuntimeError("the model is down")])
    result = _loop(planner).run()
    assert result.stop_reason is StopReason.MODEL_ERROR
    assert "proposal" in result.stop_message


# ---- refusals ------------------------------------------------------------


def test_a_refused_proposal_produces_no_tool_call() -> None:
    kg = _KG()
    planner = _ScriptedPlanner(
        [_neighbor_proposal(entity_id="drug:not_known"), _finish_proposal()]
    )
    result = _loop(planner, kg).run()
    assert kg.calls == []
    assert result.refusals
    assert result.refusals[0].code.value == "unknown_entity"


def test_a_refusal_does_not_end_the_run() -> None:
    planner = _ScriptedPlanner(
        [_neighbor_proposal(entity_id="drug:not_known"), _neighbor_proposal()]
    )
    kg = _KG()
    result = _loop(planner, kg).run()
    # The first proposal was refused, the second one ran.
    assert len(kg.calls) == 1
    assert result.refusals
    assert result.tool_call_count == 1


def test_the_refusal_reason_reaches_the_planner() -> None:
    planner = _ScriptedPlanner(
        [_neighbor_proposal(entity_id="drug:not_known"), _neighbor_proposal()]
    )
    _loop(planner).run()
    second = planner.contexts[1]
    assert second.evidence_gaps
    assert "not_known" in second.evidence_gaps[0]


def test_two_consecutive_refusals_stop_the_run() -> None:
    planner = _ScriptedPlanner(
        [
            _neighbor_proposal(entity_id="drug:not_known"),
            _neighbor_proposal(entity_id="drug:also_unknown"),
        ]
    )
    kg = _KG()
    result = _loop(planner, kg).run()
    assert result.stop_reason is StopReason.NO_VALID_ACTION
    assert kg.calls == []


# ---- budgets -------------------------------------------------------------


def test_the_turn_budget_stops_the_run() -> None:
    """A run that keeps finding new evidence still stops at the turn budget.

    Each turn must both differ from the last and produce something new, or
    an earlier stop condition would fire first.
    """
    class _GrowingKG(_KG):
        def __init__(self) -> None:
            super().__init__()
            self.round = 0

        def neighbors(self, request: dict[str, Any]):
            self.round += 1
            self.calls.append(request)
            return (
                [_Edge("drug:aspirin", f"disease:disease{self.round}")],
                "trace-1",
            )

    proposals = [
        _neighbor_proposal(
            subject_entity_id="drug:aspirin", max_results=10 + index
        )
        for index in range(10)
    ]
    planner = _ScriptedPlanner(proposals)
    result = _loop(planner, _GrowingKG(), max_turns=3).run()
    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert len(result.turns) == 3


def test_the_tool_budget_stops_the_run() -> None:
    planner = _ScriptedPlanner([_neighbor_proposal() for _ in range(10)])
    kg = _KG()
    result = _loop(planner, kg, remaining_tool_calls=2).run()
    assert result.tool_call_count <= 2


def test_a_repeated_action_is_refused_after_it_ran() -> None:
    planner = _ScriptedPlanner(
        [_neighbor_proposal(), _neighbor_proposal(), _finish_proposal()]
    )
    kg = _KG()
    result = _loop(planner, kg).run()
    assert len(kg.calls) == 1
    assert any(
        item.code.value == "duplicate_action" for item in result.refusals
    )


# ---- sufficiency ---------------------------------------------------------


def test_the_sufficiency_hook_stops_the_run() -> None:
    planner = _ScriptedPlanner([_neighbor_proposal() for _ in range(5)])
    kg = _KG()
    loop = _loop(planner, kg, sufficiency=lambda observations: True)
    result = loop.run()
    assert result.stop_reason is StopReason.EVIDENCE_SUFFICIENT
    assert result.reached_evidence_sufficiency
    assert len(kg.calls) == 1


def test_a_sufficiency_hook_that_throws_does_not_stop_the_run() -> None:
    def _broken(observations: list[Any]) -> bool:
        raise RuntimeError("the judge is broken")

    planner = _ScriptedPlanner([_neighbor_proposal(), _finish_proposal()])
    result = _loop(planner, sufficiency=_broken).run()
    assert result.stop_reason is StopReason.EVIDENCE_SUFFICIENT
    assert len(result.turns) == 2


# ---- the chain of custody ------------------------------------------------


def test_every_execution_traces_back_to_a_sealed_action() -> None:
    planner = _ScriptedPlanner([_neighbor_proposal(), _finish_proposal()])
    result = _loop(planner).run()
    for turn in result.turns:
        if turn.sealed is not None and turn.observation is not None:
            assert turn.observation.decision_id == turn.sealed.decision_id
            assert turn.observation.request_digest == turn.sealed.request_digest