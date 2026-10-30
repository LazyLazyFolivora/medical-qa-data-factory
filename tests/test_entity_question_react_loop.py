"""RED tests for the entity-first ReAct question-generation loop.

These tests describe the orchestration boundary agreed in the development
architecture.  The model may choose an action or propose a candidate; only
validation feedback may move that candidate toward a validated task.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from medical_qa_data_factory.domain import EvidenceGap, SuggestedSource
from medical_qa_data_factory.generation.actions import RunPolicy
from medical_qa_data_factory.generation.exploration import (
    AgentDecision,
    ExplorationState,
    ValidationFeedback,
    apply_validation_feedback,
    build_exploration_planner_context,
    should_stop_exploration,
)
from medical_qa_data_factory.generation.task_contracts import TaskCandidate


def _candidate(candidate_id: str = "candidate-1") -> TaskCandidate:
    return TaskCandidate.model_validate(
        {
            "candidate_id": candidate_id,
            "seed_entity_id": "primekg:exposure:D008752",
            "question_type": "single_relation_entity",
            "steps": [
                {
                    "position": 0,
                    "relation": "associated_with",
                    "direction": "forward",
                    "target_entity_type": "disease",
                }
            ],
            "proposed_answer_ids": ["primekg:disease:als"],
            "private_answer_names": ["amyotrophic lateral sclerosis"],
            "answer_entity_type": "disease",
            "snapshot_id": "primekg-snapshot-1",
            "evidence_ids": ["kg-evidence-1"],
            "public_entity_names": {
                "primekg:exposure:D008752": "Methylene Chloride"
            },
            "discovery_source": "react",
            "generator_version": "entity-react-v1",
        }
    )


def _state(*, candidates: list[TaskCandidate] | None = None) -> ExplorationState:
    return ExplorationState(
        episode_id="episode-1",
        task_id="task-1",
        seed_entity_id="primekg:exposure:D008752",
        seed_entity_name="Methylene Chloride",
        max_questions=2,
        candidates=candidates or [],
        candidate_statuses={
            item.candidate_id: "proposed" for item in candidates or []
        },
    )


def _gap(gap_id: str = "gap-population") -> EvidenceGap:
    return EvidenceGap(
        gap_id=gap_id,
        gap_type="qualifier",
        description="the study population has not been verified",
        suggested_source=SuggestedSource.LITERATURE,
        blocking=True,
    )


def test_exploration_state_and_agent_decision_round_trip_as_json() -> None:
    state = _state(candidates=[_candidate()])
    decision = AgentDecision(
        decision="propose_candidate",
        state_digest=state.state_digest(),
        candidates=[_candidate()],
        expected_information=["validate population and effect direction"],
    )

    assert ExplorationState.model_validate_json(state.model_dump_json()) == state
    restored = AgentDecision.model_validate_json(decision.model_dump_json())
    assert restored.decision == "propose_candidate"
    assert restored.candidates[0].candidate_id == "candidate-1"


def test_model_decision_cannot_claim_validated_or_published_outputs() -> None:
    payload = {
        "decision": "propose_candidate",
        "state_digest": _state().state_digest(),
        "candidates": [_candidate().model_dump(mode="json")],
        "expected_information": ["validate the candidate"],
        "validated_task_id": "validated-by-the-model",
        "published_question_ids": ["question-by-the-model"],
    }

    with pytest.raises(ValidationError):
        AgentDecision.model_validate(payload)


def test_stop_decision_requires_a_structured_reason() -> None:
    with pytest.raises(ValidationError):
        AgentDecision(
            decision="stop",
            state_digest=_state().state_digest(),
        )

    decision = AgentDecision(
        decision="stop",
        state_digest=_state().state_digest(),
        stop_reason="search_saturated",
        reason_codes=["no_state_gain_after_distinct_actions"],
        unresolved_gap_ids=["gap-population"],
    )
    assert decision.stop_reason == "search_saturated"
    assert decision.unresolved_gap_ids == ["gap-population"]


def test_recoverable_validation_gap_returns_to_the_next_planner_context() -> None:
    state = _state(candidates=[_candidate()])
    feedback = ValidationFeedback(
        candidate_id="candidate-1",
        status="recoverable_gap",
        evidence_gaps=[_gap()],
        reason_codes=["qualifier_not_supported"],
        dependency_digest="d" * 64,
    )

    updated = apply_validation_feedback(state, feedback)
    context = build_exploration_planner_context(
        updated,
        turn=2,
        policy=RunPolicy(
            max_turns=4,
            remaining_tool_calls=3,
            available_tool_names=("kg_neighbors", "literature_search"),
            allowed_action_types=("query_neighbors", "retrieve_literature"),
        ),
    )

    assert updated.candidate_statuses["candidate-1"] == "needs_evidence"
    assert context.evidence_gaps == [
        "the study population has not been verified"
    ]
    assert "retrieve_literature" in context.available_actions


def test_terminal_rejection_closes_only_its_candidate() -> None:
    first = _candidate("candidate-1")
    second = _candidate("candidate-2")
    state = _state(candidates=[first, second])
    feedback = ValidationFeedback(
        candidate_id="candidate-1",
        status="terminal_rejection",
        reason_codes=["known_multiple_answers"],
        dependency_digest="e" * 64,
    )

    updated = apply_validation_feedback(state, feedback)

    assert updated.candidate_statuses == {
        "candidate-1": "rejected",
        "candidate-2": "proposed",
    }
    assert updated.stop_reason is None
    assert should_stop_exploration(updated) is False


def test_max_questions_is_an_upper_bound_not_a_quota() -> None:
    state = _state()
    assert should_stop_exploration(state) is False

    reached = state.model_copy(
        update={"published_task_ids": ["task-a", "task-b"]}
    )
    assert should_stop_exploration(reached) is True

    exhausted_without_filling_quota = state.model_copy(
        update={"stop_reason": "search_saturated"}
    )
    assert exhausted_without_filling_quota.published_task_ids == []
    assert should_stop_exploration(exhausted_without_filling_quota) is True
