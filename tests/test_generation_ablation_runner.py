"""The isolated ablation runner.

Step 8 of the real-generation-agent plan: the gold must not reach the solver,
and the three modes must be isolated from each other.
"""
from __future__ import annotations

from typing import Any

import pytest

from medical_qa_data_factory.domain.questions import AblationMode
from medical_qa_data_factory.generation.ablation import (
    AblationOutcome,
    run_ablation,
)
from medical_qa_data_factory.generation.ablation_runner import (
    GoldHidingSolver,
    IsolatedAblationRunner,
)


class _RecordingSolver:
    """Records what it was asked and returns a scripted answer."""

    def __init__(self, answer_ids: list[str] | None = None):
        self.answer_ids = answer_ids if answer_ids is not None else ["disease:migraine"]
        self.calls: list[dict[str, Any]] = []

    def solve(
        self,
        *,
        mode: AblationMode,
        allowed_tools: Any,
        question_text: str,
        budget: float,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "mode": mode,
                "allowed_tools": list(allowed_tools),
                "question_text": question_text,
                "budget": budget,
            }
        )
        return {
            "answer_ids": list(self.answer_ids),
            "evidence_ids": ["kg.e0"],
            "evidence_coverage": 1.0,
            "policy_cost": 1.5,
        }


def test_each_mode_gets_its_own_allow_list() -> None:
    solver = _RecordingSolver()
    runner = IsolatedAblationRunner(solver=solver)
    run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
    )
    by_mode = {call["mode"]: call for call in solver.calls}
    assert "kg_neighbors" in by_mode[AblationMode.KG_ONLY]["allowed_tools"]
    assert "literature_search" not in by_mode[AblationMode.KG_ONLY]["allowed_tools"]
    assert "kg_neighbors" not in by_mode[AblationMode.LITERATURE_ONLY]["allowed_tools"]
    assert len(by_mode[AblationMode.COMBINED]["allowed_tools"]) == 4


def test_each_mode_gets_its_own_episode_id() -> None:
    solver = _RecordingSolver()
    runner = IsolatedAblationRunner(solver=solver)
    report = run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
    )
    assert len({episode.episode_id for episode in report.episodes}) == 3


def test_each_mode_gets_the_full_budget() -> None:
    """The three runs are independent, so none inherits another's spend."""
    solver = _RecordingSolver()
    runner = IsolatedAblationRunner(solver=solver, budget_per_episode=7.0)
    run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
    )
    assert all(call["budget"] == 7.0 for call in solver.calls)


def test_a_failing_mode_is_recorded_as_failed() -> None:
    class _Broken:
        def solve(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("the provider is down")

    runner = IsolatedAblationRunner(solver=_Broken())
    report = run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
    )
    assert all(
        episode.outcome is AblationOutcome.FAILED for episode in report.episodes
    )
    assert report.episodes[0].failure_reason


def test_the_gold_hiding_wrapper_drops_a_leaked_answer() -> None:
    class _Leaky:
        def solve(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "answer_ids": ["disease:migraine", "disease:other"],
                "gold_answer": "migraine",
                "writer_rationale": "the writer thought so",
            }

    wrapper = GoldHidingSolver(
        inner=_Leaky(), gold_answer_ids=frozenset({"disease:migraine"})
    )
    result = wrapper.solve(
        mode=AblationMode.KG_ONLY,
        allowed_tools=["kg_neighbors"],
        question_text="Which disease?",
        budget=1.0,
    )
    assert "disease:migraine" not in result["answer_ids"]
    assert "gold_answer" not in result
    assert "writer_rationale" not in result


def test_the_gold_hiding_wrapper_drops_leaked_evidence() -> None:
    class _Leaky:
        def solve(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "answer_ids": ["disease:migraine"],
                "evidence_ids": ["kg.e0", "kg.gold"],
            }

    wrapper = GoldHidingSolver(
        inner=_Leaky(), gold_evidence_ids=frozenset({"kg.gold"})
    )
    result = wrapper.solve(
        mode=AblationMode.COMBINED,
        allowed_tools=["kg_neighbors"],
        question_text="Which disease?",
        budget=1.0,
    )
    assert "kg.gold" not in result["evidence_ids"]


def test_the_wrapper_does_not_change_a_clean_answer() -> None:
    solver = _RecordingSolver()
    wrapper = GoldHidingSolver(inner=solver)
    result = wrapper.solve(
        mode=AblationMode.KG_ONLY,
        allowed_tools=["kg_neighbors"],
        question_text="Which disease?",
        budget=1.0,
    )
    assert result["answer_ids"] == ["disease:migraine"]


def test_the_solver_receives_the_question_text() -> None:
    """The solver must know what it is being asked."""
    solver = _RecordingSolver()
    runner = IsolatedAblationRunner(solver=solver, question_text="Which disease?")
    run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
    )
    assert all(
        call["question_text"] == "Which disease?" for call in solver.calls
    )


def test_the_solver_never_receives_the_gold() -> None:
    """The inner solver's input carries only the question and the tools."""
    solver = _RecordingSolver()
    wrapper = GoldHidingSolver(
        inner=solver,
        gold_answer_ids=frozenset({"disease:migraine"}),
        gold_evidence_ids=frozenset({"kg.gold"}),
    )
    wrapper.solve(
        mode=AblationMode.KG_ONLY,
        allowed_tools=["kg_neighbors"],
        question_text="Which disease?",
        budget=1.0,
    )
    call = solver.calls[0]
    assert set(call) == {"mode", "allowed_tools", "question_text", "budget"}
    assert "disease:migraine" not in call["question_text"]