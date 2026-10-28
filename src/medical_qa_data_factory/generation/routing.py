"""Routing decisions.

Routing is decided from typed state values only. No free-text parsing and no
model output ever selects the next node.
"""
from __future__ import annotations

from .contracts import StopReason
from .state import EntityGenerationState


class Route:
    NEXT = "next"
    FINALIZE = "finalize"
    FINALIZE_PARTIAL = "finalize_partial"
    FAIL = "fail"
    REWRITE = "write_questions"


def after_verify_seed(state: EntityGenerationState) -> str:
    if state.get("stop_reason") in (
        StopReason.INVALID_UPSTREAM_IDENTITY.value,
        StopReason.PROVIDER_ERROR.value,
        StopReason.FATAL_ERROR.value,
    ):
        return Route.FAIL
    return Route.NEXT


def after_collect_neighbors(state: EntityGenerationState) -> str:
    stop = state.get("stop_reason")
    if stop in (
        StopReason.INVALID_UPSTREAM_IDENTITY.value,
        StopReason.PROVIDER_ERROR.value,
        StopReason.FATAL_ERROR.value,
    ):
        return Route.FAIL
    if not state.get("edges"):
        return Route.FINALIZE_PARTIAL
    if stop == StopReason.BUDGET_EXHAUSTED.value and not state.get("edges"):
        # The budget ran out before any evidence was collected. When evidence
        # *was* collected, the remaining budget belongs to the later stages,
        # and throwing the evidence away would lose the run's only result.
        return Route.FINALIZE_PARTIAL
    return Route.NEXT


def after_curate_evidence(state: EntityGenerationState) -> str:
    if state.get("stop_reason") == StopReason.NO_EVIDENCE.value:
        return Route.FINALIZE_PARTIAL
    return Route.NEXT


def after_build_claims(state: EntityGenerationState) -> str:
    stop = state.get("stop_reason")
    if stop in (StopReason.NO_VALID_CLAIMS.value, StopReason.BUDGET_EXHAUSTED.value):
        return Route.FINALIZE_PARTIAL
    if stop in (StopReason.FATAL_ERROR.value, StopReason.PROVIDER_ERROR.value):
        return Route.FAIL
    return Route.NEXT


def after_persist_draft_questions(state: EntityGenerationState) -> str:
    if state.get("stop_reason") in (
        StopReason.FATAL_ERROR.value,
        StopReason.PROVIDER_ERROR.value,
    ):
        return Route.FAIL
    if not state.get("draft_question_ids"):
        return Route.FINALIZE_PARTIAL
    return Route.NEXT


def after_verify_questions(state: EntityGenerationState) -> str:
    """A revision loop is allowed at most once per blueprint."""
    stop = state.get("stop_reason")
    if stop == StopReason.BUDGET_EXHAUSTED.value:
        return Route.FINALIZE_PARTIAL
    revisions = state.get("revision_counts") or {}
    if revisions.get("pending", 0) > 0 and revisions.get("used", 0) < 1:
        return Route.REWRITE
    if not state.get("accepted_question_ids"):
        return Route.FINALIZE_PARTIAL
    return Route.NEXT


def after_machine_validate(state: EntityGenerationState) -> str:
    if state.get("stop_reason") in (
        StopReason.ALL_CANDIDATES_HARD_REJECTED.value,
        StopReason.BUDGET_EXHAUSTED.value,
    ):
        return Route.FINALIZE_PARTIAL
    return Route.NEXT
