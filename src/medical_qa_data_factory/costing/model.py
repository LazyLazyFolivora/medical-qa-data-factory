"""Policy-cost calculation implementations."""
from __future__ import annotations

from .models import CostModelSnapshot, PolicyCost


class UnknownActionCostError(KeyError):
    """Raised when an action has no price in the frozen snapshot."""


class FixedPriceCostModel:
    """Calculate policy cost from a frozen per-action price table."""

    def __init__(self, snapshot: CostModelSnapshot) -> None:
        if snapshot.aggregation_rule != "fixed_action_price":
            raise ValueError(
                "FixedPriceCostModel requires aggregation_rule=fixed_action_price"
            )
        self.snapshot = snapshot

    def _price(self, action_kind: str) -> float:
        try:
            return self.snapshot.action_price_table[action_kind]
        except KeyError as exc:
            raise UnknownActionCostError(
                f"No policy cost configured for action kind: {action_kind}"
            ) from exc

    def estimate(self, action_kind: str) -> PolicyCost:
        """Return the cost visible before an action executes."""
        price = self._price(action_kind)
        return PolicyCost(
            cost_model_version=self.snapshot.cost_model_version,
            budget_unit=self.snapshot.budget_unit,
            estimated_before_action=price,
            components={"base_action": price},
        )

    def charge(self, action_kind: str) -> PolicyCost:
        """Return the amount charged after execution.

        In the v0 fixed-price model, the estimate and final charge are identical.
        Raw usage values do not alter this result.
        """
        price = self._price(action_kind)
        return PolicyCost(
            cost_model_version=self.snapshot.cost_model_version,
            budget_unit=self.snapshot.budget_unit,
            estimated_before_action=price,
            charged_after_action=price,
            components={"base_action": price},
        )

