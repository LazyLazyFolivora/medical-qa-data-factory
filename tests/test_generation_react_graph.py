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
from medical_qa_data_factory.generation.exploration import (
    AgentDecision,
    ValidationFeedback,
)
from medical_qa_data_factory.generation.planner import PlannerContext
from medical_qa_data_factory.generation.react_graph import (
    ReactContext,
    build_react_graph,
)
from medical_qa_data_factory.generation.skills import (
    build_default_skill_registry,
)
from medical_qa_data_factory.generation.task_contracts import TaskCandidate
from medical_qa_data_factory.domain import EvidenceGap, SuggestedSource


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


def _candidate() -> TaskCandidate:
    return TaskCandidate.model_validate(
        {
            "candidate_id": "candidate-1",
            "seed_entity_id": "drug:aspirin",
            "question_type": "single_relation_entity",
            "steps": [
                {
                    "position": 0,
                    "relation": "treats",
                    "direction": "forward",
                    "target_entity_type": "disease",
                }
            ],
            "proposed_answer_ids": ["disease:headache"],
            "private_answer_names": ["headache"],
            "answer_entity_type": "disease",
            "snapshot_id": "snapshot-1",
            "evidence_ids": ["e-drug:aspirin-disease:headache"],
            "public_entity_names": {"drug:aspirin": "aspirin"},
            "discovery_source": "react",
            "generator_version": "test-v1",
        }
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
            _neighbor(max_results=20),
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


def test_a_failed_tool_action_can_be_retried() -> None:
    class _FlakyKG(_KG):
        def neighbors(self, request: dict[str, Any]):
            self.calls.append(request)
            if len(self.calls) == 1:
                raise RuntimeError("temporary provider failure")
            return self.edges, "trace-2"

    kg = _FlakyKG()
    planner = _ScriptedPlanner([_neighbor(), _neighbor(), _finish()])

    final = _run(_context(planner, kg))

    assert len(kg.calls) == 2
    assert [
        item["status"] for item in final["react_observations"]
    ] == ["error", "success"]
    assert not any(
        item["code"] == "duplicate_action"
        for item in final.get("react_rejections", [])
    )


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
        [_neighbor(max_results=20) for _ in range(5)]
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


def test_recoverable_candidate_gap_returns_to_search_instead_of_finishing() -> None:
    planner = _ScriptedPlanner(
        [_neighbor(max_results=10), _finish(), _neighbor(max_results=9), _finish()]
    )
    assessments = {"count": 0}

    def assess(observations, state):
        assessments["count"] += 1
        if assessments["count"] == 1:
            return [_candidate()], [
                ValidationFeedback(
                    candidate_id="candidate-1",
                    status="recoverable_gap",
                    evidence_gaps=[
                        EvidenceGap(
                            gap_id="gap-population",
                            gap_type="qualifier",
                            description="the study population has not been verified",
                            suggested_source=SuggestedSource.LITERATURE,
                            blocking=True,
                        )
                    ],
                    reason_codes=["qualifier_not_supported"],
                    dependency_digest="a" * 64,
                )
            ]
        return [_candidate()], [
            ValidationFeedback(
                candidate_id="candidate-1",
                status="validated",
                validated_task_id="validated-task-1",
                dependency_digest="b" * 64,
            )
        ]

    final = _run(
        _context(
            planner,
            candidate_assessor=assess,
            max_questions=1,
        )
    )

    assert assessments["count"] == 2
    assert planner.contexts[2].evidence_gaps == [
        "the study population has not been verified"
    ]
    assert final["react_candidate_statuses"] == {"candidate-1": "validated"}
    assert final["react_published_task_ids"] == ["validated-task-1"]
    assert final["stop_reason"] == StopReason.EVIDENCE_SUFFICIENT.value


def test_entity_planner_may_propose_but_only_assessor_may_validate() -> None:
    class _EntityPlanner:
        def __init__(self) -> None:
            self.turn = 0

        def propose(self, state, context):
            self.turn += 1
            if self.turn == 1:
                return AgentDecision(
                    decision="continue_search",
                    state_digest=state.state_digest(),
                    next_action=_neighbor().action,
                    expected_information=["find a candidate relation"],
                )
            return AgentDecision(
                decision="propose_candidate",
                state_digest=state.state_digest(),
                candidates=[_candidate()],
                expected_information=["validate the candidate"],
            )

    def assess(observations, state):
        candidate = TaskCandidate.model_validate(state["react_candidates"][0])
        return [candidate], [
            ValidationFeedback(
                candidate_id=candidate.candidate_id,
                status="validated",
                validated_task_id="validated-task-1",
                dependency_digest="c" * 64,
            )
        ]

    planner = _ScriptedPlanner([])
    final = _run(
        _context(
            planner,
            entity_planner=_EntityPlanner(),
            candidate_assessor=assess,
        )
    )

    assert planner.contexts == []
    assert final["react_candidate_statuses"] == {"candidate-1": "validated"}
    assert final["react_published_task_ids"] == ["validated-task-1"]


def test_entity_planner_can_select_a_retrieved_skill() -> None:
    class _SkillEntityPlanner:
        def __init__(self) -> None:
            self.turn = 0
            self.available_skill_ids: list[str] = []

        def propose(self, state, context):
            self.turn += 1
            self.available_skill_ids = [
                item["skill_id"] for item in context.available_skills
            ]
            if self.turn == 1:
                return AgentDecision(
                    decision="continue_search",
                    state_digest=state.state_digest(),
                    selected_skill_id="primekg-neighbor-exploration",
                    skill_arguments={
                        "subject_entity_id": "drug:aspirin",
                        "direction": "outgoing",
                        "max_results": 10,
                    },
                    expected_information=["find a candidate relation"],
                )
            return AgentDecision(
                decision="propose_candidate",
                state_digest=state.state_digest(),
                candidates=[_candidate()],
                expected_information=["validate the candidate"],
            )

    def assess(observations, state):
        candidate = TaskCandidate.model_validate(state["react_candidates"][0])
        return [candidate], [
            ValidationFeedback(
                candidate_id=candidate.candidate_id,
                status="validated",
                validated_task_id="validated-task-1",
                dependency_digest="d" * 64,
            )
        ]

    kg = _KG()
    entity_planner = _SkillEntityPlanner()
    final = _run(
        _context(
            _ScriptedPlanner([]),
            kg,
            entity_planner=entity_planner,
            candidate_assessor=assess,
            skill_registry=build_default_skill_registry(),
            seed_entity_type="drug",
        )
    )

    assert "primekg-neighbor-exploration" in entity_planner.available_skill_ids
    assert len(kg.calls) == 1
    assert final["react_skill_invocations"][0]["skill_id"] == (
        "primekg-neighbor-exploration"
    )
    assert final["react_skill_outcomes"][0]["status"] == "success"
    assert final["react_skill_outcomes"][0]["evidence_gain"] == 1
    assert final["react_candidate_statuses"] == {"candidate-1": "validated"}


def test_empty_skill_execution_is_a_warning_not_a_success() -> None:
    class _EmptySkillPlanner:
        def propose(self, state, context):
            return AgentDecision(
                decision="continue_search",
                state_digest=state.state_digest(),
                selected_skill_id="primekg-neighbor-exploration",
                skill_arguments={
                    "subject_entity_id": "drug:aspirin",
                    "direction": "outgoing",
                    "max_results": 10,
                },
                expected_information=["look for candidate relations"],
            )

    final = _run(
        _context(
            _ScriptedPlanner([]),
            _KG(edges=[]),
            entity_planner=_EmptySkillPlanner(),
            candidate_assessor=lambda observations, state: ([], []),
            skill_registry=build_default_skill_registry(),
            seed_entity_type="drug",
            stagnation_limit=1,
        )
    )

    assert final["react_skill_outcomes"][0]["status"] == "warning"
    assert final["react_skill_outcomes"][0]["evidence_gain"] == 0
    assert final["react_observations"][0]["status"] == "empty"
    assert final["react_planner_steps"][0]["execution"]["status"] == "empty"
    assert final["react_planner_steps"][0]["outcome"]["useful"] is False


def test_entity_planner_can_execute_ctd_skill() -> None:
    class _CtdRecord:
        source_record_locator = "ctd-record:D001241:MESH:D006261"
        content_hash = "ctd-content-hash"

    class _CTD:
        def __init__(self) -> None:
            self.calls = []

        def lookup(self, request):
            self.calls.append(request)
            return [_CtdRecord()], "ctd-trace", "ctd-snapshot"

    class _CtdPlanner:
        def __init__(self) -> None:
            self.turn = 0
            self.available_skill_ids = []

        def propose(self, state, context):
            self.turn += 1
            self.available_skill_ids = [
                item["skill_id"] for item in context.available_skills
            ]
            if self.turn == 1:
                return AgentDecision(
                    decision="continue_search",
                    state_digest=state.state_digest(),
                    selected_skill_id="ctd-direct-evidence-lookup",
                    skill_arguments={
                        "chemical_entity_id": "exposure:D001241",
                        "max_results": 10,
                    },
                )
            return AgentDecision(
                decision="stop",
                state_digest=state.state_digest(),
                stop_reason="search_saturated",
            )

    ctd = _CTD()
    planner = _CtdPlanner()
    final = _run(
        _context(
            _ScriptedPlanner([]),
            entity_planner=planner,
            candidate_assessor=lambda observations, state: ([], []),
            executor=Executor(kg=_KG(), ctd=ctd),
            skill_registry=build_default_skill_registry(),
            seed_entity_id="exposure:D001241",
            seed_entity_name="Chemical One",
            seed_entity_type="exposure",
            available_tools=["ctd_lookup"],
            available_action_types=["query_ctd"],
            policy=RunPolicy(
                available_tool_names=("ctd_lookup", "none"),
                allowed_action_types=("query_ctd", "finish"),
            ),
        )
    )

    assert "ctd-direct-evidence-lookup" in planner.available_skill_ids
    assert len(ctd.calls) == 1
    assert final["react_skill_outcomes"][0]["status"] == "success"
    assert final["react_skill_outcomes"][0]["produced_artifact_types"] == [
        "ctd_evidence"
    ]
    assert final["react_observations"][0]["artifact_summaries"] == []
    assert final["react_policy"]["has_evidence"] is False


def test_pubmed_skill_is_unlocked_by_kg_evidence_and_executes() -> None:
    class _LiteratureItem:
        evidence_id = "lit-pubmed-1"

    class _Literature:
        def __init__(self) -> None:
            self.calls = []

        def search(self, request):
            self.calls.append(request)
            return [_LiteratureItem()], "pubmed-trace", "pubmed-snapshot"

    class _PubMedPlanner:
        def __init__(self) -> None:
            self.turn = 0
            self.skills_by_turn = []
            self.open_needs_by_turn = []

        def propose(self, state, context):
            self.turn += 1
            self.skills_by_turn.append(
                [item["skill_id"] for item in context.available_skills]
            )
            self.open_needs_by_turn.append(context.open_needs)
            if self.turn == 1:
                return AgentDecision(
                    decision="continue_search",
                    state_digest=state.state_digest(),
                    selected_skill_id="primekg-neighbor-exploration",
                    skill_arguments={
                        "subject_entity_id": "drug:aspirin",
                        "direction": "outgoing",
                        "max_results": 10,
                    },
                )
            if self.turn == 2:
                return AgentDecision(
                    decision="continue_search",
                    state_digest=state.state_digest(),
                    selected_skill_id="pubmed-evidence-search",
                    skill_arguments={
                        "query": "aspirin AND headache",
                        "evidence_need_id": (
                            "need.97c8f90f6e5266021bd3cfb8"
                            "ec6dfa7d4c9101c78751fa93f5b78d8e0d1b7cd6"
                        ),
                        "max_results": 5,
                    },
                )
            return AgentDecision(
                decision="stop",
                state_digest=state.state_digest(),
                stop_reason="search_saturated",
            )

    literature = _Literature()
    planner = _PubMedPlanner()
    final = _run(
        _context(
            _ScriptedPlanner([]),
            entity_planner=planner,
            candidate_assessor=lambda observations, state: ([], []),
            executor=Executor(kg=_KG(), literature=literature),
            skill_registry=build_default_skill_registry(),
            seed_entity_type="drug",
            open_needs=[
                {
                    "need_id": "need.97c8f90f6e5266021bd3cfb8",
                    "dependency_digest": (
                        "97c8f90f6e5266021bd3cfb8ec6dfa7d4c9101c7"
                        "8751fa93f5b78d8e0d1b7cd6"
                    ),
                    "information_need": "support",
                }
            ],
            available_tools=["kg_neighbors", "literature_search"],
            available_action_types=[
                "query_neighbors",
                "expand_entity",
                "retrieve_literature",
            ],
            policy=RunPolicy(
                available_tool_names=(
                    "kg_neighbors",
                    "literature_search",
                    "none",
                ),
                allowed_action_types=(
                    "query_neighbors",
                    "expand_entity",
                    "retrieve_literature",
                    "finish",
                ),
                open_need_ids=("need.97c8f90f6e5266021bd3cfb8",),
            ),
        )
    )

    assert "pubmed-evidence-search" not in planner.skills_by_turn[0]
    assert "pubmed-evidence-search" in planner.skills_by_turn[1]
    assert "dependency_digest" not in planner.open_needs_by_turn[1][0]
    assert len(literature.calls) == 1
    assert final["react_skill_invocations"][1]["arguments"][
        "evidence_need_id"
    ] == "need.97c8f90f6e5266021bd3cfb8"
    assert final["react_rejections"] == []
    assert [item["skill_id"] for item in final["react_skill_invocations"]] == [
        "primekg-neighbor-exploration",
        "pubmed-evidence-search",
    ]
    assert final["react_skill_outcomes"][1]["produced_artifact_types"] == [
        "literature_evidence"
    ]


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
