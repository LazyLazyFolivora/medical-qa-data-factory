import json

import pytest

from medical_qa_data_factory.costing import (
    BudgetExceededError,
    CostModelSnapshot,
    FixedPriceCostModel,
    NormalizedBudgetLedger,
    UnknownActionCostError,
    load_cost_model_snapshot,
)


@pytest.fixture
def snapshot() -> CostModelSnapshot:
    return CostModelSnapshot(
        cost_model_version="selector-cost-test",
        budget_unit="normalized_unit",
        action_price_table={
            "stop": 0.0,
            "kg_lookup": 1.0,
            "literature_search": 3.0,
        },
        component_weights={"base_action": 1.0},
        aggregation_rule="fixed_action_price",
        effective_at="2026-10-15T00:00:00Z",
        notes="test fixture",
    )


def test_fixed_price_model_estimates_and_charges_same_amount(snapshot):
    model = FixedPriceCostModel(snapshot)

    estimated = model.estimate("literature_search")
    charged = model.charge("literature_search")

    assert estimated.estimated_before_action == 3.0
    assert estimated.charged_after_action is None
    assert charged.estimated_before_action == 3.0
    assert charged.charged_after_action == 3.0
    assert charged.components == {"base_action": 3.0}
    assert charged.cost_model_version == "selector-cost-test"


def test_unknown_action_has_no_implicit_free_fallback(snapshot):
    model = FixedPriceCostModel(snapshot)

    with pytest.raises(UnknownActionCostError, match="unpriced_action"):
        model.estimate("unpriced_action")


def test_cost_snapshot_cannot_be_mutated_in_place(snapshot):
    with pytest.raises(TypeError):
        snapshot.action_price_table["kg_lookup"] = 99.0


def test_budget_ledger_uses_policy_cost_not_raw_usage(snapshot):
    model = FixedPriceCostModel(snapshot)
    ledger = NormalizedBudgetLedger(initial_amount=5.0, snapshot=snapshot)

    estimated = model.estimate("literature_search")
    assert ledger.can_afford(estimated)

    ledger.charge(model.charge("literature_search"), label="search-1")

    assert ledger.remaining.amount == 2.0
    assert ledger.spent == 3.0
    assert ledger.entries[0]["charged"] == 3.0


def test_budget_ledger_rejects_overspend(snapshot):
    model = FixedPriceCostModel(snapshot)
    ledger = NormalizedBudgetLedger(initial_amount=2.0, snapshot=snapshot)

    assert not ledger.can_afford(model.estimate("literature_search"))
    with pytest.raises(BudgetExceededError):
        ledger.charge(model.charge("literature_search"), label="search-1")


def test_snapshot_can_be_loaded_from_json(tmp_path):
    path = tmp_path / "cost-model.json"
    path.write_text(
        json.dumps(
            {
                "cost_model_version": "selector-cost-json",
                "budget_unit": "normalized_unit",
                "action_price_table": {"stop": 0, "kg_lookup": 1},
                "component_weights": {"base_action": 1},
                "aggregation_rule": "fixed_action_price",
                "effective_at": "2026-10-15T00:00:00Z",
                "notes": None,
            }
        ),
        encoding="utf-8",
    )

    snapshot = load_cost_model_snapshot(path)

    assert snapshot.cost_model_version == "selector-cost-json"
    assert snapshot.action_price_table["kg_lookup"] == 1.0


@pytest.mark.parametrize("prices", [{}, {"kg_lookup": -1.0}])
def test_snapshot_rejects_invalid_price_tables(prices):
    with pytest.raises(ValueError):
        CostModelSnapshot(
            cost_model_version="selector-cost-invalid",
            budget_unit="normalized_unit",
            action_price_table=prices,
            component_weights={"base_action": 1.0},
            aggregation_rule="fixed_action_price",
            effective_at="2026-10-15T00:00:00Z",
        )


def test_snapshot_rejects_negative_component_weight():
    with pytest.raises(ValueError, match="component weights"):
        CostModelSnapshot(
            cost_model_version="selector-cost-invalid",
            budget_unit="normalized_unit",
            action_price_table={"kg_lookup": 1.0},
            component_weights={"base_action": -1.0},
            aggregation_rule="fixed_action_price",
            effective_at="2026-10-15T00:00:00Z",
        )
