"""Routing decisions come from typed state, never from free text."""
from __future__ import annotations

from medical_qa_data_factory.generation.contracts import StopReason
from medical_qa_data_factory.generation.routing import (
    Route,
    after_build_claims,
    after_collect_neighbors,
    after_curate_evidence,
    after_machine_validate,
    after_persist_draft_questions,
    after_verify_questions,
    after_verify_seed,
)
from medical_qa_data_factory.generation.state import EntityGenerationState


def _state(**overrides) -> EntityGenerationState:
    payload: dict = {
        "episode_id": "ep1",
        "task_id": "t1",
        "stop_reason": None,
        "draft_question_ids": [],
        "accepted_question_ids": [],
        "rejected_question_ids": [],
        "revision_counts": {"used": 0, "pending": 0},
    }
    payload.update(overrides)
    return payload  # type: ignore[return-value]


def test_invalid_upstream_identity_routes_to_fail() -> None:
    state = _state(stop_reason=StopReason.INVALID_UPSTREAM_IDENTITY.value)
    assert after_verify_seed(state) == Route.FAIL


def test_valid_seed_routes_forward() -> None:
    assert after_verify_seed(_state()) == Route.NEXT


def test_seed_provider_error_routes_to_fail() -> None:
    state = _state(stop_reason=StopReason.PROVIDER_ERROR.value)
    assert after_verify_seed(state) == Route.FAIL


def test_seed_fatal_error_routes_to_fail() -> None:
    state = _state(stop_reason=StopReason.FATAL_ERROR.value)
    assert after_verify_seed(state) == Route.FAIL


def test_provider_error_routes_to_fail() -> None:
    state = _state(stop_reason=StopReason.PROVIDER_ERROR.value)
    assert after_collect_neighbors(state) == Route.FAIL


def test_no_evidence_routes_to_partial() -> None:
    state = _state(stop_reason=StopReason.NO_EVIDENCE.value)
    assert after_collect_neighbors(state) == Route.FINALIZE_PARTIAL


def test_budget_exhausted_during_collection_is_partial() -> None:
    state = _state(stop_reason=StopReason.BUDGET_EXHAUSTED.value)
    assert after_collect_neighbors(state) == Route.FINALIZE_PARTIAL


def test_curation_with_no_evidence_is_partial() -> None:
    state = _state(stop_reason=StopReason.NO_EVIDENCE.value)
    assert after_curate_evidence(state) == Route.FINALIZE_PARTIAL


def test_no_valid_claims_is_partial() -> None:
    state = _state(stop_reason=StopReason.NO_VALID_CLAIMS.value)
    assert after_build_claims(state) == Route.FINALIZE_PARTIAL


def test_no_persisted_drafts_is_partial() -> None:
    assert after_persist_draft_questions(_state()) == Route.FINALIZE_PARTIAL


def test_persisted_drafts_continue() -> None:
    state = _state(draft_question_ids=["q1"])
    assert after_persist_draft_questions(state) == Route.NEXT


def test_revision_is_allowed_only_once() -> None:
    pending = _state(
        accepted_question_ids=["q1"], revision_counts={"used": 0, "pending": 1}
    )
    assert after_verify_questions(pending) == Route.REWRITE
    used = _state(accepted_question_ids=["q1"], revision_counts={"used": 1, "pending": 1})
    assert after_verify_questions(used) == Route.NEXT


def test_all_hard_rejected_is_partial() -> None:
    state = _state(
        stop_reason=StopReason.ALL_CANDIDATES_HARD_REJECTED.value,
        rejected_question_ids=["q1"],
        accepted_question_ids=[],
    )
    assert after_verify_questions(state) == Route.FINALIZE_PARTIAL


def test_machine_validate_budget_exhausted_is_partial() -> None:
    state = _state(stop_reason=StopReason.BUDGET_EXHAUSTED.value)
    assert after_machine_validate(state) == Route.FINALIZE_PARTIAL


def test_target_reached_continues_to_finalize() -> None:
    state = _state(stop_reason=StopReason.TARGET_REACHED.value)
    assert after_machine_validate(state) == Route.NEXT


def test_routing_inputs_are_typed_values_only() -> None:
    """The router reads state fields, never a parsed natural-language blob."""
    import inspect

    from medical_qa_data_factory.generation import routing

    source = inspect.getsource(routing)
    assert "json.loads" not in source
    assert "re.search" not in source
