"""Versioned policy-cost contracts.

Raw tool/model usage is intentionally not represented as policy cost here.
PolicyCost is the normalized amount used for offline policy comparison and
fixed-budget trajectory generation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class CostModelSnapshot:
    """Immutable description of one experiment's policy-cost rules."""

    cost_model_version: str
    budget_unit: str
    action_price_table: Mapping[str, float]
    component_weights: Mapping[str, float]
    aggregation_rule: str
    effective_at: str
    notes: str | None = None

    def __post_init__(self) -> None:
        if not self.cost_model_version.strip():
            raise ValueError("cost_model_version must not be empty")
        if not self.budget_unit.strip():
            raise ValueError("budget_unit must not be empty")
        if not self.action_price_table:
            raise ValueError("action_price_table must not be empty")
        if any(not kind.strip() for kind in self.action_price_table):
            raise ValueError("action kinds must not be empty")
        if any(float(price) < 0 for price in self.action_price_table.values()):
            raise ValueError("action prices must be non-negative")
        if any(float(weight) < 0 for weight in self.component_weights.values()):
            raise ValueError("component weights must be non-negative")
        if not self.aggregation_rule.strip():
            raise ValueError("aggregation_rule must not be empty")

        object.__setattr__(
            self,
            "action_price_table",
            MappingProxyType(
                {kind: float(price) for kind, price in self.action_price_table.items()}
            ),
        )
        object.__setattr__(
            self,
            "component_weights",
            MappingProxyType(
                {name: float(weight) for name, weight in self.component_weights.items()}
            ),
        )


@dataclass(frozen=True)
class PolicyCost:
    """Normalized cost assigned by a frozen experimental cost model."""

    cost_model_version: str
    budget_unit: str
    estimated_before_action: float | None = None
    charged_after_action: float | None = None
    components: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        values = (self.estimated_before_action, self.charged_after_action)
        if all(value is None for value in values):
            raise ValueError("PolicyCost requires an estimate or a charge")
        if any(value is not None and value < 0 for value in values):
            raise ValueError("PolicyCost values must be non-negative")
        if any(value < 0 for value in self.components.values()):
            raise ValueError("PolicyCost components must be non-negative")
        if not self.cost_model_version.strip() or not self.budget_unit.strip():
            raise ValueError("PolicyCost version and budget unit must not be empty")
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))


@dataclass(frozen=True)
class BudgetAmount:
    """An initial or remaining normalized budget."""

    amount: float
    budget_unit: str
    cost_model_version: str

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("budget amount must be non-negative")
        if not self.budget_unit.strip() or not self.cost_model_version.strip():
            raise ValueError("budget unit and cost model version must not be empty")

