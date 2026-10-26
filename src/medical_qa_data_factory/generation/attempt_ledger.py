"""Persistent attempt ledger: reserve, charge, release, reconcile.

The aggregate budget is always derived from the latest version of every
``AttemptRecord`` in scope. There is deliberately no parallel counter: a second
source of truth is exactly how a resume ends up silently resetting the budget.

Reconciliation rules (all versioned, all tested):

* ``reserved`` past its reservation timeout, with no evidence the call went
  out, is released and refunded.
* ``running`` with an unknown result becomes ``unknown`` and is charged
  conservatively at the reserved ceiling, never at zero.
* ``completed`` with missing usage is charged at the reserved ceiling.
* A retry of an ``unknown`` attempt is only allowed when the provider
  explicitly supports idempotency keys; otherwise the run must ask a human.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Protocol

from .run_identity import (
    AttemptOutcome,
    AttemptRecord,
    AttemptStatus,
    RUN_IDENTITY_POLICY_VERSION,
)


#: Reconciliation policy version. Bumping it changes how stale attempts are
#: settled, so it must be recorded with every aggregate.
RECONCILIATION_POLICY_VERSION = "reconciliation-v1"


class BudgetExhaustedError(RuntimeError):
    """Raised when a new reservation would exceed the remaining budget."""


class ReconciliationError(RuntimeError):
    """Raised when an attempt cannot be settled deterministically."""


class AttemptStore(Protocol):
    """Where attempts live. The ledger never assumes in-memory state."""

    def list_attempts(self, scope: str, owner_id: str) -> list[AttemptRecord]: ...

    def list_all_attempts(self) -> list[AttemptRecord]: ...

    def save_attempt(self, attempt: AttemptRecord) -> Any: ...


class AttemptScope(str, Enum):
    TASK = "task"
    RUN = "run"


@dataclass(frozen=True)
class BudgetTotals:
    """Derived budget facts. Never stored as the source of truth."""

    reserved_units: float
    charged_units: float
    outstanding_units: float
    released_units: float
    conservative_charge_units: float
    open_attempts: int
    unknown_attempts: int

    @property
    def total_charge(self) -> float:
        """What the ledger will actually bill, including conservative charges."""
        return self.charged_units + self.conservative_charge_units


def _latest(attempts: Iterable[AttemptRecord]) -> dict[str, AttemptRecord]:
    """Collapse version history to the newest version per attempt id."""
    latest: dict[str, AttemptRecord] = {}
    for attempt in attempts:
        current = latest.get(attempt.attempt_id)
        if current is None or _version_number(attempt) >= _version_number(current):
            latest[attempt.attempt_id] = attempt
    return latest


def _version_number(attempt: AttemptRecord) -> int:
    try:
        return int(attempt.attempt_version.rsplit(".v", 1)[1])
    except (IndexError, ValueError):  # pragma: no cover - defensive
        return 1


def totals_for(attempts: Iterable[AttemptRecord]) -> BudgetTotals:
    """Derive budget totals from attempt versions only."""
    reserved = 0.0
    charged = 0.0
    conservative = 0.0
    open_attempts = 0
    unknown_attempts = 0
    for attempt in _latest(attempts).values():
        reserved += attempt.reserved_units
        if attempt.status is AttemptStatus.COMPLETED:
            charged += attempt.charged_units or 0.0
        elif attempt.status is AttemptStatus.FAILED:
            charged += attempt.charged_units or 0.0
        elif attempt.status is AttemptStatus.UNKNOWN:
            unknown_attempts += 1
            # An unknown outcome is billed at the reservation ceiling. Billing
            # it at zero would let a crashed run retry for free.
            conservative += attempt.reserved_units
        elif attempt.status in (AttemptStatus.RESERVED, AttemptStatus.RUNNING):
            open_attempts += 1
            conservative += attempt.reserved_units
    outstanding = 0.0
    released = 0.0
    return BudgetTotals(
        reserved_units=reserved,
        charged_units=charged,
        outstanding_units=outstanding,
        released_units=released,
        conservative_charge_units=conservative,
        open_attempts=open_attempts,
        unknown_attempts=unknown_attempts,
    )


@dataclass
class AttemptLedger:
    """Reserve / charge / release around a persistent attempt store."""

    store: AttemptStore
    #: Ceiling for the units this ledger may reserve in total.
    max_units: float
    #: How long a ``reserved`` attempt may sit before it is refunded.
    reservation_timeout_seconds: int = 300
    reconciliation_version: str = RECONCILIATION_POLICY_VERSION
    _open: dict[str, AttemptRecord] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.max_units < 0:
            raise ValueError("max_units must be non-negative")

    # ---- derivation ----------------------------------------------------

    def totals(self, scope: str, owner_id: str) -> BudgetTotals:
        return totals_for(self.store.list_attempts(scope, owner_id))

    def remaining_units(self, scope: str, owner_id: str) -> float:
        totals = self.totals(scope, owner_id)
        return max(self.max_units - totals.total_charge, 0.0)

    def would_exceed(self, scope: str, owner_id: str, units: float) -> bool:
        return self.totals(scope, owner_id).total_charge + units > self.max_units

    # ---- lifecycle -----------------------------------------------------

    def reserve(
        self,
        *,
        scope: str,
        owner_id: str,
        tool_name: str,
        operation: str,
        request_digest: str,
        units: float,
        run_id: str | None = None,
        episode_id: str | None = None,
    ) -> AttemptRecord:
        if units < 0:
            raise ValueError("reserved units must be non-negative")
        if self.would_exceed(scope, owner_id, units):
            raise BudgetExhaustedError(
                f"reserving {units} would exceed the {self.max_units} unit ceiling"
            )
        attempt = AttemptRecord(
            attempt_id=f"attempt_{request_digest[:32]}_{len(self._open) + 1}",
            attempt_version="v1",
            idempotency_key=f"{scope}:{owner_id}:{request_digest}",
            scope=scope,
            owner_id=owner_id,
            run_id=run_id,
            episode_id=episode_id,
            tool_name=tool_name,
            operation=operation,
            request_digest=request_digest,
            status=AttemptStatus.RESERVED,
            reserved_units=units,
            created_at=_now(),
            updated_at=_now(),
        )
        self.store.save_attempt(attempt)
        self._open[attempt.attempt_id] = attempt
        return attempt

    def mark_running(self, attempt: AttemptRecord) -> AttemptRecord:
        """Only a ``running`` record may precede the physical external call."""
        running = attempt.with_status(AttemptStatus.RUNNING)
        self.store.save_attempt(running)
        self._open[running.attempt_id] = running
        return running

    def complete(
        self,
        attempt: AttemptRecord,
        *,
        outcome: AttemptOutcome,
        charged_units: float | None = None,
        response_digest: str | None = None,
        provider_request_id: str | None = None,
        usage: list[Any] | None = None,
        error_code: str | None = None,
        retryable: bool = False,
    ) -> AttemptRecord:
        if outcome is AttemptOutcome.UNKNOWN:
            settled = attempt.with_status(
                AttemptStatus.UNKNOWN,
                outcome=outcome,
                error_code=error_code,
                retryable=retryable,
            )
        else:
            charge = charged_units
            if charge is None:
                # Missing usage is charged at the ceiling, never at zero.
                charge = attempt.reserved_units
            status = (
                AttemptStatus.COMPLETED
                if outcome is AttemptOutcome.SUCCESS
                else AttemptStatus.FAILED
            )
            settled = attempt.with_status(
                status,
                outcome=outcome,
                charged_units=charge,
                response_digest=response_digest,
                provider_request_id=provider_request_id,
                usage=usage,
                error_code=error_code,
                retryable=retryable,
            )
        self.store.save_attempt(settled)
        self._open.pop(settled.attempt_id, None)
        return settled

    def release(self, attempt: AttemptRecord, *, reason: str) -> AttemptRecord:
        """Refund a reservation that never became a call."""
        if attempt.status not in (AttemptStatus.RESERVED, AttemptStatus.RUNNING):
            raise ReconciliationError(
                f"attempt {attempt.attempt_id} is {attempt.status.value}; "
                "only an in-flight attempt can be released"
            )
        released = attempt.with_status(
            AttemptStatus.FAILED,
            outcome=AttemptOutcome.POLICY_REJECTED,
            charged_units=0.0,
            error_code=reason,
        )
        self.store.save_attempt(released)
        self._open.pop(released.attempt_id, None)
        return released

    # ---- reconciliation ------------------------------------------------

    def reconcile(
        self,
        scope: str,
        owner_id: str,
        *,
        now_epoch_seconds: float,
        idempotent_provider: bool = False,
    ) -> list[AttemptRecord]:
        """Settle stale attempts deterministically.

        Returns the attempts that were written. ``reserved`` records past their
        timeout are refunded; ``running`` records become ``unknown`` and are
        billed at the reservation ceiling.
        """
        settled: list[AttemptRecord] = []
        for attempt in _latest(self.store.list_attempts(scope, owner_id)).values():
            if attempt.status is AttemptStatus.RESERVED:
                age = now_epoch_seconds - _epoch(attempt.updated_at)
                if age > self.reservation_timeout_seconds:
                    settled.append(self.release(attempt, reason="reservation_timeout"))
            elif attempt.status is AttemptStatus.RUNNING:
                settled.append(
                    self.complete(
                        attempt,
                        outcome=AttemptOutcome.UNKNOWN,
                        error_code="unknown_outcome",
                        retryable=idempotent_provider,
                    )
                )
        return settled

    def retry_allowed(self, attempt: AttemptRecord, *, idempotent_provider: bool) -> bool:
        """Only an idempotent provider may retry an unknown outcome."""
        if attempt.status is not AttemptStatus.UNKNOWN:
            return True
        return idempotent_provider


def _now():
    from ..domain.common import utc_now

    return utc_now()


def _epoch(value) -> float:
    from datetime import timezone

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


__all__ = [
    "AttemptLedger",
    "AttemptScope",
    "AttemptStore",
    "BudgetExhaustedError",
    "BudgetTotals",
    "RECONCILIATION_POLICY_VERSION",
    "ReconciliationError",
    "RUN_IDENTITY_POLICY_VERSION",
    "totals_for",
]