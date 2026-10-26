"""Run identity, evidence-mode authority, and the attempt ledger.

Step 1 of the real-generation-agent plan. The point of these tests is that
the authority rules hold even when a caller tries to work around them.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from medical_qa_data_factory.domain.common import ToolSnapshot, utc_now
from medical_qa_data_factory.generation.attempt_ledger import (
    AttemptLedger,
    BudgetExhaustedError,
    totals_for,
)
from medical_qa_data_factory.generation.run_identity import (
    AttemptOutcome,
    AttemptRecord,
    AttemptStatus,
    GenerationPreparationRun,
    GenerationRunManifest,
    PreparationStatus,
    RequiredEvidenceMode,
    RunMode,
)


def _snapshot() -> ToolSnapshot:
    return ToolSnapshot(
        tool_name="kg_primekg",
        tool_version="1.0.0",
        data_snapshot_id="snap-1",
        config_hash="cfg",
        endpoint_kind="python",
        retrieved_at=utc_now(),
    )


def _attempt(**overrides: Any) -> AttemptRecord:
    payload: dict[str, Any] = {
        "attempt_id": "attempt_1",
        "attempt_version": "attempt_1.v1",
        "idempotency_key": "run:t1:digest",
        "scope": "run",
        "owner_id": "t1",
        "tool_name": "kg_primekg",
        "operation": "neighbors",
        "request_digest": "digest",
        "reserved_units": 2.0,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    payload.update(overrides)
    return AttemptRecord(**payload)


# ---- RequiredEvidenceMode ------------------------------------------------


def test_evidence_mode_has_the_four_documented_values() -> None:
    assert {item.value for item in RequiredEvidenceMode} == {
        "kg_only",
        "literature_only",
        "hybrid",
        "hybrid_per_hop",
    }


def test_evidence_mode_is_not_the_same_field_as_verified_route() -> None:
    """The generation requirement must not be relaxable by ablation."""
    from medical_qa_data_factory.domain.questions import Route

    assert RequiredEvidenceMode.HYBRID_PER_HOP.value not in {
        item.value for item in Route
    }


# ---- preparation and manifest -------------------------------------------


def _preparation(**overrides: Any) -> GenerationPreparationRun:
    payload: dict[str, Any] = {
        "preparation_id": "prep_1",
        "task_id": "t1",
        "run_mode": RunMode.STRUCTURED,
        "request_digest": "digest",
        "required_evidence_mode": RequiredEvidenceMode.HYBRID_PER_HOP,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    payload.update(overrides)
    return GenerationPreparationRun(**payload)


def _manifest(**overrides: Any) -> GenerationRunManifest:
    payload: dict[str, Any] = {
        "run_id": "run_1",
        "task_id": "t1",
        "preparation_id": "prep_1",
        "run_mode": RunMode.STRUCTURED,
        "run_policy_version": "policy-v1",
        "required_evidence_mode": RequiredEvidenceMode.HYBRID_PER_HOP,
        "request_digest": "digest",
        "created_at": utc_now(),
    }
    payload.update(overrides)
    return GenerationRunManifest(**payload)


def test_only_a_ready_preparation_is_ready() -> None:
    assert _preparation(status=PreparationStatus.READY).is_ready()
    assert not _preparation(status=PreparationStatus.FAILED).is_ready()
    assert not _preparation(status=PreparationStatus.PREPARING).is_ready()


def test_manifest_carries_the_authoritative_mode() -> None:
    manifest = _manifest()
    assert manifest.required_evidence_mode is RequiredEvidenceMode.HYBRID_PER_HOP
    assert manifest.rag_snapshot_id is None


def test_manifest_requires_a_preparation_reference() -> None:
    with pytest.raises(ValidationError):
        _manifest(preparation_id="")


def test_manifest_cannot_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _manifest(required_evidence_mode_override="kg_only")


# ---- AttemptRecord -------------------------------------------------------


def test_reserved_attempt_has_no_charge() -> None:
    attempt = _attempt()
    assert attempt.status is AttemptStatus.RESERVED
    assert attempt.charged_units is None


def test_in_flight_attempt_must_not_be_charged() -> None:
    with pytest.raises(ValidationError):
        _attempt(status=AttemptStatus.RUNNING, charged_units=1.0)


def test_terminal_attempt_requires_an_outcome() -> None:
    with pytest.raises(ValidationError):
        _attempt(status=AttemptStatus.COMPLETED, charged_units=1.0)


def test_terminal_attempt_requires_a_charge() -> None:
    with pytest.raises(ValidationError):
        _attempt(status=AttemptStatus.FAILED, outcome=AttemptOutcome.TIMEOUT)


def test_status_transition_bumps_the_version() -> None:
    attempt = _attempt()
    running = attempt.with_status(AttemptStatus.RUNNING)
    assert running.attempt_version == "attempt_1.v2"
    done = running.with_status(
        AttemptStatus.COMPLETED,
        outcome=AttemptOutcome.SUCCESS,
        charged_units=1.0,
    )
    assert done.attempt_version == "attempt_1.v3"
    assert done.charged_units == 1.0


# ---- budget derivation ---------------------------------------------------


def test_totals_are_derived_from_attempts_only() -> None:
    attempt = _attempt().with_status(
        AttemptStatus.COMPLETED, outcome=AttemptOutcome.SUCCESS, charged_units=1.5
    )
    totals = totals_for([attempt])
    assert totals.charged_units == 1.5
    assert totals.total_charge == 1.5


def test_unknown_attempt_is_billed_at_the_reservation_ceiling() -> None:
    """An unknown outcome must never be billed as free."""
    attempt = _attempt().with_status(
        AttemptStatus.UNKNOWN, outcome=AttemptOutcome.UNKNOWN
    )
    totals = totals_for([attempt])
    assert totals.conservative_charge_units == 2.0
    assert totals.total_charge == 2.0
    assert totals.unknown_attempts == 1


def test_open_attempt_counts_against_the_budget() -> None:
    totals = totals_for([_attempt()])
    assert totals.open_attempts == 1
    assert totals.total_charge == 2.0


def test_only_the_latest_version_of_an_attempt_counts() -> None:
    first = _attempt()
    second = first.with_status(
        AttemptStatus.COMPLETED, outcome=AttemptOutcome.SUCCESS, charged_units=1.0
    )
    totals = totals_for([first, second])
    assert totals.charged_units == 1.0
    assert totals.open_attempts == 0


class _MemoryStore:
    """Minimal attempt store for the ledger tests."""

    def __init__(self) -> None:
        self.records: dict[str, list[AttemptRecord]] = {}

    def list_attempts(self, scope: str, owner_id: str) -> list[AttemptRecord]:
        return [
            item
            for items in self.records.values()
            for item in items
            if item.scope == scope and item.owner_id == owner_id
        ]

    def list_all_attempts(self) -> list[AttemptRecord]:
        return [item for items in self.records.values() for item in items]

    def save_attempt(self, attempt: AttemptRecord) -> str:
        self.records.setdefault(attempt.attempt_id, []).append(attempt)
        return "written"


def test_ledger_reserves_and_charges() -> None:
    store = _MemoryStore()
    ledger = AttemptLedger(store=store, max_units=10.0)
    attempt = ledger.reserve(
        scope="run",
        owner_id="t1",
        tool_name="kg_primekg",
        operation="neighbors",
        request_digest="digest",
        units=2.0,
    )
    assert ledger.remaining_units("run", "t1") == 8.0
    ledger.mark_running(attempt)
    settled = ledger.complete(
        attempt, outcome=AttemptOutcome.SUCCESS, charged_units=1.0
    )
    assert settled.status is AttemptStatus.COMPLETED
    assert ledger.remaining_units("run", "t1") == 9.0


def test_ledger_refuses_to_exceed_the_ceiling() -> None:
    ledger = AttemptLedger(store=_MemoryStore(), max_units=1.0)
    with pytest.raises(BudgetExhaustedError):
        ledger.reserve(
            scope="run",
            owner_id="t1",
            tool_name="kg_primekg",
            operation="neighbors",
            request_digest="digest",
            units=2.0,
        )


def test_released_reservation_is_refunded() -> None:
    store = _MemoryStore()
    ledger = AttemptLedger(store=store, max_units=10.0)
    attempt = ledger.reserve(
        scope="run",
        owner_id="t1",
        tool_name="kg_primekg",
        operation="neighbors",
        request_digest="digest",
        units=2.0,
    )
    ledger.release(attempt, reason="policy_rejected")
    assert ledger.remaining_units("run", "t1") == 10.0


def test_reconciliation_refunds_a_stale_reservation() -> None:
    store = _MemoryStore()
    ledger = AttemptLedger(store=store, max_units=10.0)
    ledger.reserve(
        scope="run",
        owner_id="t1",
        tool_name="kg_primekg",
        operation="neighbors",
        request_digest="digest",
        units=2.0,
    )
    settled = ledger.reconcile(
        "run", "t1", now_epoch_seconds=utc_now().timestamp() + 10_000
    )
    assert len(settled) == 1
    assert settled[0].status is AttemptStatus.FAILED
    assert settled[0].charged_units == 0.0
    assert ledger.remaining_units("run", "t1") == 10.0


def test_reconciliation_settles_a_running_attempt_as_unknown() -> None:
    store = _MemoryStore()
    ledger = AttemptLedger(store=store, max_units=10.0)
    attempt = ledger.reserve(
        scope="run",
        owner_id="t1",
        tool_name="kg_primekg",
        operation="neighbors",
        request_digest="digest",
        units=2.0,
    )
    ledger.mark_running(attempt)
    settled = ledger.reconcile(
        "run", "t1", now_epoch_seconds=utc_now().timestamp()
    )
    assert len(settled) == 1
    assert settled[0].status is AttemptStatus.UNKNOWN
    # The unknown attempt is billed at the ceiling, never at zero.
    assert ledger.remaining_units("run", "t1") == 8.0


def test_unknown_outcome_may_only_be_retried_by_an_idempotent_provider() -> None:
    ledger = AttemptLedger(store=_MemoryStore(), max_units=10.0)
    attempt = _attempt().with_status(
        AttemptStatus.UNKNOWN, outcome=AttemptOutcome.UNKNOWN
    )
    assert ledger.retry_allowed(attempt, idempotent_provider=True)
    assert not ledger.retry_allowed(attempt, idempotent_provider=False)


def test_budget_survives_a_restart_because_it_comes_from_the_store() -> None:
    """A resume must not reset the budget: the store is the only truth."""
    store = _MemoryStore()
    first = AttemptLedger(store=store, max_units=10.0)
    attempt = first.reserve(
        scope="run",
        owner_id="t1",
        tool_name="kg_primekg",
        operation="neighbors",
        request_digest="digest",
        units=3.0,
    )
    first.complete(attempt, outcome=AttemptOutcome.SUCCESS, charged_units=2.0)
    # A fresh ledger over the same store sees the same spend.
    second = AttemptLedger(store=store, max_units=10.0)
    assert second.remaining_units("run", "t1") == 8.0


def test_preparation_and_run_scopes_are_separate_budgets() -> None:
    store = _MemoryStore()
    ledger = AttemptLedger(store=store, max_units=10.0)
    ledger.reserve(
        scope="task",
        owner_id="t1",
        tool_name="rag",
        operation="preflight",
        request_digest="d1",
        units=4.0,
    )
    # The preflight spend must not eat the run's budget.
    assert ledger.remaining_units("run", "t1") == 10.0
    assert ledger.remaining_units("task", "t1") == 6.0