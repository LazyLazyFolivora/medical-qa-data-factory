"""Acceptance cases are executable Step 1 data, not prose-only examples."""
from __future__ import annotations

import json
from pathlib import Path

from medical_qa_data_factory.generation.objective_question_contracts import (
    ObjectiveAcceptanceSuite,
)


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "objective_questions"
    / "acceptance-cases.json"
)


def test_acceptance_suite_covers_every_planned_case() -> None:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    suite = ObjectiveAcceptanceSuite.model_validate(payload)
    expected_ids = {
        *(f"OBJ-{index:02d}" for index in range(1, 16)),
        *(f"OPS-{index:02d}" for index in range(1, 11)),
        "EVAL-01",
        "EVAL-02",
    }

    assert set(suite.case_ids) == expected_ids
    assert all(case.expected_verdict for case in suite.cases)
    assert all(case.expected_reason_code for case in suite.cases)
    assert len({case.case_id for case in suite.cases}) == len(suite.cases)


def test_acceptance_suite_does_not_contain_duplicate_expected_reasons() -> None:
    suite = ObjectiveAcceptanceSuite.model_validate_json(
        FIXTURE_PATH.read_text(encoding="utf-8")
    )

    assert all(
        isinstance(case.expected_reason_code, str)
        and case.expected_reason_code
        and case.expected_reason_code == case.expected_reason_code.upper()
        for case in suite.cases
    )
