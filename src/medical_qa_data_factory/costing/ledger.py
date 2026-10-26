"""Normalized fixed-budget accounting for generated trajectories."""
from __future__ import annotations

from .models import BudgetAmount, CostModelSnapshot, PolicyCost


class BudgetExceededError(ValueError):
    """Raised when a charge would exceed the remaining policy budget."""


class CostModelMismatchError(ValueError):
    """Raised when a cost belongs to a different unit or model version."""


class NormalizedBudgetLedger:
    """Ledger that accepts only PolicyCost from its frozen cost snapshot."""

    def __init__(self, initial_amount: float, snapshot: CostModelSnapshot) -> None:
        if initial_amount < 0:
            raise ValueError("initial_amount must be non-negative")
        self.snapshot = snapshot
        self.initial_amount = float(initial_amount)
        self._spent = 0.0
        self._entries: list[dict[str, object]] = []

    @property
    def remaining(self) -> BudgetAmount:
        return BudgetAmount(
            amount=self.initial_amount - self._spent,
            budget_unit=self.snapshot.budget_unit,
            cost_model_version=self.snapshot.cost_model_version,
        )

    @property
    def spent(self) -> float:
        return self._spent

    @property
    def entries(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(entry) for entry in self._entries)

    def _validate(self, cost: PolicyCost) -> None:
        if (
            cost.cost_model_version != self.snapshot.cost_model_version
            or cost.budget_unit != self.snapshot.budget_unit
        ):
            raise CostModelMismatchError(
                "PolicyCost does not match the ledger's frozen cost model"
            )

    def can_afford(self, cost: PolicyCost) -> bool:
        self._validate(cost)
        amount = cost.estimated_before_action
        if amount is None:
            amount = cost.charged_after_action
        assert amount is not None
        return amount <= self.remaining.amount

    def charge(self, cost: PolicyCost, label: str) -> None:
        self._validate(cost)
        amount = cost.charged_after_action
        if amount is None:
            raise ValueError("charged_after_action is required for budget charging")
        if amount > self.remaining.amount:
            raise BudgetExceededError(
                f"charge {amount} exceeds remaining budget {self.remaining.amount}"
            )
        self._spent += amount
        self._entries.append(
            {
                "label": label,
                "charged": amount,
                "cumulative": self._spent,
                "remaining": self.remaining.amount,
                "cost_model_version": self.snapshot.cost_model_version,
            }
        )

