"""The agent graph: a real observe-decide-gate-act loop.

Step 6 of the real-generation-agent plan. The property that distinguishes this
from a renamed DAG is that the decision at turn N depends on what was
observed at turn N-1.
"""
from __future__ import annotations

from typing import Any

import pytest

from medical_qa_data_factory.generation.actions import (
    ModelActionProposal,
    RunPolicy,
    canonical_action_hash,
)
from medical_qa_data_factory.generation.contracts import StopReason
from medical_qa_data_factory.generation.executor import Executor
from medical_qa_data_factory.generation.planner import PlannerContext
from medical_qa_data_factory.generation.react_graph import (
    ReactContext,
    build_react_graph,
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


class _ScriptedPlanner:
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


def _neighbor(entity_id: str = "drug:aspirin", **overrides: Any):
    action: dict[str, Any] = {
        "action_type": "query_neighbors",
        "subject_entity_id": entity_id,
        "direction": "outgoing",
        "max_results": 10,
    }
    action.update(overrides)
    return ModelActionProposal(
        action=action,
        expected_information="the neighbours of the entity",
        evidence_gap="the neighbour set is unknown",
    )


def _finish():
    return ModelActionProposal(
        action={"action_type": "finish", "stop_reason": "evidence_sufficient"},
        expected_information="enough",
        evidence_gap="none",
    )


def _context(
    planner: _ScriptedPlanner,
    kg: _KG | None = None,
    **overrides: Any,
) -> ReactContext:
    payload: dict[str, Any] = {
        "planner": planner,
        "executor": Executor(kg=kg if kg is not None else _KG()),
        "policy": RunPolicy(
            has_evidence=True,
            available_tool_names=("kg_neighbors", "literature_search", "none"),
        ),
        "run_id": "run1",
        "episode_id": "ep1",
        "seed_entity_id": "drug:aspirin",
        "seed_entity_name": "aspirin",
        "available_tools": ["kg_neighbors"],
    }
    payload.update(overrides)
    return ReactContext(**payload)


def _run(context: ReactContext, request: dict[str, Any] | None = None):
    graph = build_react_graph(context)
    state = graph.initial_state(
        request or {"task_id": "t1", "workspace": "ws"}
    )
    return graph.invoke(state)


# ---- the loop ------------------------------------------------------------


def test_the_graph_runs_a_proposal_and_then_finishes() -> None:
    kg = _KG()
    planner = _ScriptedPlanner([_neighbor(), _finish()])
    final = _run(_context(planner, kg))
    assert len(kg.calls) == 1
    assert final["stop_reason"] == StopReason.EVIDENCE_SUFFICIENT.value


def test_the_decision_at_turn_two_sees_turn_one() -> None:
    """This is what makes it an agent loop and not a renamed DAG."""
    planner = _ScriptedPlanner([_neighbor(), _finish()])
    _run(_context(planner))
    assert len(planner.contexts) == 2
    second = planner.contexts[1]
    assert second.turn == 2
    assert second.observations
    assert second.observations[0]["status"] == "success"
    assert second.known_entities
    assert "disease:headache" in {
        item["canonical_id"] for item in second.known_entities
    }


def test_the_decision_at_turn_two_sees_the_turn_one_rejection() -> None:
    planner = _ScriptedPlanner(
        [
            _neighbor(max_results=100),
            _neighbor(max_results=10),
            _finish(),
        ]
    )
    final = _run(
        _context(
            planner,
            policy=RunPolicy(
                has_evidence=True,
                max_results_per_query=10,
                available_tool_names=("kg_neighbors", "none"),
            ),
        )
    )
    second = planner.contexts[1]
    assert second.max_results_per_query == 10
    assert second.recent_policy_rejections[0]["code"] == "limit_too_large"
    assert final["react_rejections"][0]["code"] == "limit_too_large"
    assert final["react_sealed_actions"][0]["request"]["max_results"] == 10


def test_the_planner_sees_the_live_remaining_edge_budget() -> None:
    planner = _ScriptedPlanner(
        [_neighbor(max_results=4), _neighbor(max_results=3), _finish()]
    )
    remaining = {"value": 7}

    class _BudgetedKG(_KG):
        def neighbors(self, request: dict[str, Any]):
            edges, trace = super().neighbors(request)
            remaining["value"] -= int(request.get("max_results", 0))
            return edges, trace

    _run(
        _context(
            planner,
            _BudgetedKG(),
            policy=RunPolicy(
                has_evidence=True,
                max_results_per_query=10,
                available_tool_names=("kg_neighbors", "none"),
            ),
            remaining_edge_budget_provider=lambda: remaining["value"],
        )
    )
    assert planner.contexts[0].remaining_edge_budget == 7
    assert planner.contexts[1].remaining_edge_budget == 3


def test_the_decision_at_turn_two_sees_the_evidence_summary() -> None:
    planner = _ScriptedPlanner([_neighbor(), _finish()])
    _run(
        _context(
            planner,
            evidence_summary_provider=lambda: {
                "total_evidence": 1,
                "seed_linked_edges": [
                    {
                        "head_name": "aspirin",
                        "relation": "treats",
                        "tail_name": "headache",
                    }
                ],
            },
        )
    )
    assert planner.contexts[1].evidence_summary["total_evidence"] == 1
    assert (
        planner.contexts[1].evidence_summary["seed_linked_edges"][0]["relation"]
        == "treats"
    )


def test_the_discovered_entity_is_offered_to_the_next_planner() -> None:
    planner = _ScriptedPlanner([_neighbor(), _finish()])
    _run(_context(planner))
    known = {
        item["canonical_id"] for item in planner.contexts[1].known_entities
    }
    assert "disease:headache" in known


def test_the_graph_records_the_sealed_actions() -> None:
    planner = _ScriptedPlanner([_neighbor(), _finish()])
    final = _run(_context(planner))
    assert final["react_sealed_actions"]
    assert final["react_sealed_actions"][0]["action_type"] == "query_neighbors"


def test_the_graph_records_the_observations() -> None:
    planner = _ScriptedPlanner([_neighbor(), _finish()])
    final = _run(_context(planner))
    assert final["react_observations"]
    assert final["react_observations"][0]["action_type"] == "query_neighbors"


def test_the_graph_restores_known_entities_from_checkpoint_state() -> None:
    """A rebuilt graph must trust persisted state, not empty closure state."""
    kg = _KG([_Edge("disease:headache", "gene:ptgs1")])
    planner = _ScriptedPlanner([_neighbor("disease:headache"), _finish()])
    context = _context(planner, kg)
    graph = build_react_graph(context)
    state = graph.initial_state({"task_id": "t1", "workspace": "ws"})
    state["react_known_entity_ids"] = ["drug:aspirin", "disease:headache"]
    final = graph.invoke(state)

    assert len(kg.calls) == 1
    assert kg.calls[0]["subject_entity_id"] == "disease:headache"
    assert final["stop_reason"] == StopReason.EVIDENCE_SUFFICIENT.value


def test_the_graph_restores_executed_action_hashes_from_checkpoint_state() -> None:
    kg = _KG()
    repeated = _neighbor()
    planner = _ScriptedPlanner([repeated, RuntimeError("done")])
    context = _context(planner, kg)
    graph = build_react_graph(context)
    state = graph.initial_state({"task_id": "t1", "workspace": "ws"})
    state["react_action_hashes"] = [
        canonical_action_hash(repeated.validated_action())
    ]
    # Simulate a legacy checkpoint whose policy payload predates persisted
    # executed_action_hashes.
    state["react_policy"]["executed_action_hashes"] = []
    final = graph.invoke(state)

    assert kg.calls == []
    assert final["react_rejections"][0]["code"] == "duplicate_action"


def test_an_empty_tool_result_does_not_make_finish_eligible() -> None:
    class _EmptyKG(_KG):
        def neighbors(self, request: dict[str, Any]):
            self.calls.append(request)
            return [], "trace-1"

    planner = _ScriptedPlanner([_neighbor(), _finish(), RuntimeError("done")])
    final = _run(
        _context(
            planner,
            _EmptyKG(),
            policy=RunPolicy(
                has_evidence=False,
                available_tool_names=("kg_neighbors", "none"),
            ),
        )
    )

    assert final["stop_reason"] != StopReason.EVIDENCE_SUFFICIENT.value
    assert any(
        item["code"] == "finish_without_evidence"
        for item in final["react_rejections"]
    )


# ---- refusals ------------------------------------------------------------


def test_a_refused_proposal_reaches_no_tool() -> None:
    kg = _KG()
    planner = _ScriptedPlanner(
        [_neighbor(entity_id="drug:not_known"), _neighbor(), _finish()]
    )
    final = _run(_context(planner, kg))
    assert len(kg.calls) == 1
    assert final["react_rejections"]
    assert final["react_rejections"][0]["code"] == "unknown_entity"


def test_a_model_error_stops_the_run() -> None:
    planner = _ScriptedPlanner([RuntimeError("the model is down")])
    final = _run(_context(planner))
    assert final["stop_reason"] == StopReason.MODEL_ERROR.value


# ---- budgets -------------------------------------------------------------


def test_the_turn_budget_stops_the_run() -> None:
    class _GrowingKG(_KG):
        def __init__(self) -> None:
            super().__init__()
            self.round = 0

        def neighbors(self, request: dict[str, Any]):
            self.round += 1
            self.calls.append(request)
            return (
                [_Edge("drug:aspirin", f"disease:d{self.round}", f"e{self.round}")],
                "trace-1",
            )

    proposals = [
        _neighbor(max_results=10 + index) for index in range(10)
    ]
    planner = _ScriptedPlanner(proposals)
    final = _run(_context(planner, _GrowingKG(), max_turns=3))
    assert final["stop_reason"] == StopReason.BUDGET_EXHAUSTED.value
    assert final["react_turn"] == 3


def test_the_tool_budget_stops_the_run() -> None:
    proposals = [_neighbor(max_results=10 + index) for index in range(10)]
    planner = _ScriptedPlanner(proposals)
    final = _run(
        _context(
            planner,
            policy=RunPolicy(
                has_evidence=True,
                remaining_tool_calls=2,
                available_tool_names=("kg_neighbors", "none"),
            ),
        )
    )
    assert final["stop_reason"] == StopReason.BUDGET_EXHAUSTED.value


def test_stagnation_stops_the_run() -> None:
    """Two turns without new evidence end the run."""
    class _EmptyKG(_KG):
        def neighbors(self, request: dict[str, Any]):
            self.calls.append(request)
            return [], "trace-1"

    planner = _ScriptedPlanner(
        [_neighbor(max_results=10 + index) for index in range(10)]
    )
    final = _run(_context(planner, _EmptyKG()))
    assert final["stop_reason"] == StopReason.NO_NEW_EVIDENCE.value


def test_repeated_policy_refusals_stop_the_run() -> None:
    planner = _ScriptedPlanner(
        [_neighbor(max_results=100) for _ in range(5)]
    )
    final = _run(
        _context(
            planner,
            policy=RunPolicy(
                has_evidence=True,
                max_results_per_query=10,
                available_tool_names=("kg_neighbors", "none"),
            ),
            rejection_limit=3,
        )
    )
    assert len(planner.contexts) == 3
    assert final["stop_reason"] == StopReason.NO_VALID_ACTION.value
    assert len(final["react_rejections"]) == 3


def test_an_exhausted_edge_budget_stops_the_run() -> None:
    planner = _ScriptedPlanner([_neighbor()])
    final = _run(
        _context(
            planner,
            policy=RunPolicy(
                has_evidence=True,
                remaining_edge_budget=0,
                available_tool_names=("kg_neighbors", "none"),
            ),
        )
    )
    assert final["stop_reason"] == StopReason.BUDGET_EXHAUSTED.value


# ---- the sufficiency hook -----------------------------------------------


def test_the_sufficiency_hook_stops_the_run() -> None:
    planner = _ScriptedPlanner([_neighbor(max_results=10 + index) for index in range(5)])
    final = _run(_context(planner, sufficiency=lambda observations: True))
    assert final["stop_reason"] == StopReason.EVIDENCE_SUFFICIENT.value


# ---- the audit trail -----------------------------------------------------


def test_the_observation_hook_receives_every_observation() -> None:
    seen: list[Any] = []
    planner = _ScriptedPlanner([_neighbor(), _finish()])
    _run(_context(planner, on_observation=seen.append))
    assert len(seen) == 1
    assert seen[0].action_type == "query_neighbors"


def test_the_rejection_hook_receives_every_rejection() -> None:
    seen: list[Any] = []
    planner = _ScriptedPlanner(
        [_neighbor(entity_id="drug:not_known"), _neighbor(), _finish()]
    )
    _run(_context(planner, on_rejection=seen.append))
    assert len(seen) == 1
    assert seen[0]["code"] == "unknown_entity"


def test_the_sealed_action_hook_receives_every_sealed_action() -> None:
    seen: list[Any] = []
    planner = _ScriptedPlanner([_neighbor(), _finish()])
    _run(_context(planner, on_sealed_action=seen.append))
    assert len(seen) == 2
    assert seen[0].action_type.value == "query_neighbors"
    assert seen[1].action_type.value == "finish"
