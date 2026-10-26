"""Source ablation and the verified route.

Step 8 of the real-generation-agent plan. The route must be computed from the
three runs, not asserted, and the gold must be hidden in every run.
"""
from __future__ import annotations

from typing import Any

import pytest

from medical_qa_data_factory.domain.questions import (
    AblationMode,
    Route,
)
from medical_qa_data_factory.generation.ablation import (
    AblationEpisode,
    AblationOutcome,
    AblationReport,
    AblationRunnerPort,
    MODE_TOOLS,
    apply_verified_route,
    compute_verified_route,
    question_digest,
    run_ablation,
)
from medical_qa_data_factory.generation.run_identity import RequiredEvidenceMode


def _episode(
    mode: AblationMode,
    outcome: AblationOutcome,
    *,
    answer_ids: list[str] | None = None,
    gold_hidden: bool = True,
    failure_reason: str | None = None,
) -> AblationEpisode:
    from medical_qa_data_factory.domain.common import utc_now

    now = utc_now()
    return AblationEpisode(
        episode_id=f"ablation.q1.{mode.value}",
        mode=mode,
        allowed_tools=list(MODE_TOOLS[mode]),
        snapshot_ids=["snap-1"],
        trace_ids=[f"trace.{mode.value}"],
        gold_hidden=gold_hidden,
        outcome=outcome,
        answer_ids=answer_ids if answer_ids is not None else [],
        evidence_ids=["kg.e0"] if outcome is AblationOutcome.ANSWERED else [],
        evidence_coverage=1.0 if outcome is AblationOutcome.ANSWERED else 0.0,
        failure_reason=failure_reason,
        started_at=now,
        completed_at=now,
    )


class _ScriptedRunner(AblationRunnerPort):
    """Replays a scripted outcome per mode and records what it was asked."""

    def __init__(self, outcomes: dict[AblationMode, AblationOutcome]):
        self.outcomes = outcomes
        self.calls: list[tuple[AblationMode, str]] = []

    def run(self, mode: AblationMode, *, episode_id: str) -> AblationEpisode:
        self.calls.append((mode, episode_id))
        outcome = self.outcomes[mode]
        return _episode(
            mode,
            outcome,
            answer_ids=["disease:migraine"]
            if outcome is AblationOutcome.ANSWERED
            else [],
        )


# ---- the three runs ------------------------------------------------------


def test_three_isolated_episodes_are_run() -> None:
    runner = _ScriptedRunner(
        {
            AblationMode.KG_ONLY: AblationOutcome.ANSWERED,
            AblationMode.LITERATURE_ONLY: AblationOutcome.NOT_ANSWERED,
            AblationMode.COMBINED: AblationOutcome.ANSWERED,
        }
    )
    report = run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
    )
    assert len(report.episodes) == 3
    assert len(runner.calls) == 3
    # Each episode has its own id.
    assert len({episode_id for _, episode_id in runner.calls}) == 3


def test_each_mode_gets_its_own_tool_allow_list() -> None:
    runner = _ScriptedRunner(
        {
            AblationMode.KG_ONLY: AblationOutcome.ANSWERED,
            AblationMode.LITERATURE_ONLY: AblationOutcome.NOT_ANSWERED,
            AblationMode.COMBINED: AblationOutcome.ANSWERED,
        }
    )
    report = run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
    )
    by_mode = {episode.mode: episode for episode in report.episodes}
    assert "kg_neighbors" in by_mode[AblationMode.KG_ONLY].allowed_tools
    assert "literature_search" not in by_mode[AblationMode.KG_ONLY].allowed_tools
    assert "kg_neighbors" not in by_mode[AblationMode.LITERATURE_ONLY].allowed_tools
    assert len(by_mode[AblationMode.COMBINED].allowed_tools) == 4


def test_every_run_hides_the_gold() -> None:
    runner = _ScriptedRunner(
        {
            AblationMode.KG_ONLY: AblationOutcome.ANSWERED,
            AblationMode.LITERATURE_ONLY: AblationOutcome.NOT_ANSWERED,
            AblationMode.COMBINED: AblationOutcome.ANSWERED,
        }
    )
    report = run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
        gold_answer_ids=["disease:migraine"],
    )
    assert all(episode.gold_hidden for episode in report.episodes)
    assert all(result.gold_hidden for result in report.results)


def test_a_run_that_saw_the_gold_may_not_verify_a_route() -> None:
    """The report is incomplete when a run was not blind."""
    runner = _ScriptedRunner(
        {
            AblationMode.KG_ONLY: AblationOutcome.ANSWERED,
            AblationMode.LITERATURE_ONLY: AblationOutcome.NOT_ANSWERED,
            AblationMode.COMBINED: AblationOutcome.ANSWERED,
        }
    )
    report = run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
    )
    leaked = report.episodes[0].model_copy(update={"gold_hidden": False})
    from medical_qa_data_factory.generation.ablation import route_is_complete

    assert not route_is_complete(
        report.model_copy(update={"episodes": [leaked, *report.episodes[1:]]})
    )


# ---- the route -----------------------------------------------------------


def test_only_the_combined_run_answered_gives_hybrid() -> None:
    route, _ = compute_verified_route(
        [
            _episode(AblationMode.KG_ONLY, AblationOutcome.NOT_ANSWERED),
            _episode(AblationMode.LITERATURE_ONLY, AblationOutcome.NOT_ANSWERED),
            _episode(AblationMode.COMBINED, AblationOutcome.ANSWERED),
        ]
    )
    assert route is Route.HYBRID


def test_only_the_kg_run_answered_gives_kg() -> None:
    route, _ = compute_verified_route(
        [
            _episode(AblationMode.KG_ONLY, AblationOutcome.ANSWERED),
            _episode(AblationMode.LITERATURE_ONLY, AblationOutcome.NOT_ANSWERED),
            _episode(AblationMode.COMBINED, AblationOutcome.ANSWERED),
        ]
    )
    assert route is Route.KG


def test_only_the_literature_run_answered_gives_literature() -> None:
    route, _ = compute_verified_route(
        [
            _episode(AblationMode.KG_ONLY, AblationOutcome.NOT_ANSWERED),
            _episode(AblationMode.LITERATURE_ONLY, AblationOutcome.ANSWERED),
            _episode(AblationMode.COMBINED, AblationOutcome.ANSWERED),
        ]
    )
    assert route is Route.LITERATURE


def test_both_single_runs_answered_gives_redundant() -> None:
    route, rationale = compute_verified_route(
        [
            _episode(AblationMode.KG_ONLY, AblationOutcome.ANSWERED),
            _episode(AblationMode.LITERATURE_ONLY, AblationOutcome.ANSWERED),
            _episode(AblationMode.COMBINED, AblationOutcome.ANSWERED),
        ]
    )
    assert route is Route.REDUNDANT
    assert "both" in rationale


def test_no_run_answered_gives_unanswerable() -> None:
    route, _ = compute_verified_route(
        [
            _episode(AblationMode.KG_ONLY, AblationOutcome.NOT_ANSWERED),
            _episode(AblationMode.LITERATURE_ONLY, AblationOutcome.NOT_ANSWERED),
            _episode(AblationMode.COMBINED, AblationOutcome.NOT_ANSWERED),
        ]
    )
    assert route is Route.UNANSWERABLE


def test_a_failed_run_is_not_evidence_either_way() -> None:
    route, _ = compute_verified_route(
        [
            _episode(AblationMode.KG_ONLY, AblationOutcome.FAILED),
            _episode(AblationMode.LITERATURE_ONLY, AblationOutcome.NOT_ANSWERED),
            _episode(AblationMode.COMBINED, AblationOutcome.ANSWERED),
        ]
    )
    # The knowledge graph neither answered nor failed to answer; the run
    # broke, so it cannot claim the question needs only the graph.
    assert route is Route.HYBRID


def test_the_route_is_computed_deterministically() -> None:
    episodes = [
        _episode(AblationMode.KG_ONLY, AblationOutcome.NOT_ANSWERED),
        _episode(AblationMode.LITERATURE_ONLY, AblationOutcome.NOT_ANSWERED),
        _episode(AblationMode.COMBINED, AblationOutcome.ANSWERED),
    ]
    first, first_rationale = compute_verified_route(episodes)
    second, second_rationale = compute_verified_route(list(reversed(episodes)))
    assert first is second
    assert first_rationale == second_rationale


def test_the_route_explains_itself() -> None:
    _, rationale = compute_verified_route(
        [
            _episode(AblationMode.KG_ONLY, AblationOutcome.NOT_ANSWERED),
            _episode(AblationMode.LITERATURE_ONLY, AblationOutcome.NOT_ANSWERED),
            _episode(AblationMode.COMBINED, AblationOutcome.ANSWERED),
        ]
    )
    assert rationale
    assert "neither source" in rationale


# ---- the report ----------------------------------------------------------


def test_the_report_records_the_frozen_question() -> None:
    runner = _ScriptedRunner(
        {
            AblationMode.KG_ONLY: AblationOutcome.ANSWERED,
            AblationMode.LITERATURE_ONLY: AblationOutcome.NOT_ANSWERED,
            AblationMode.COMBINED: AblationOutcome.ANSWERED,
        }
    )
    report = run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
    )
    assert report.question_dependency_digest == question_digest({"question": "?"})
    assert report.ablation_config_version == "ablation-v1"


def test_the_report_records_the_generation_requirement() -> None:
    runner = _ScriptedRunner(
        {
            AblationMode.KG_ONLY: AblationOutcome.ANSWERED,
            AblationMode.LITERATURE_ONLY: AblationOutcome.NOT_ANSWERED,
            AblationMode.COMBINED: AblationOutcome.ANSWERED,
        }
    )
    report = run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
        required_evidence_mode=RequiredEvidenceMode.HYBRID_PER_HOP,
    )
    assert report.required_evidence_mode is RequiredEvidenceMode.HYBRID_PER_HOP


def test_the_results_carry_the_audit_fields() -> None:
    runner = _ScriptedRunner(
        {
            AblationMode.KG_ONLY: AblationOutcome.ANSWERED,
            AblationMode.LITERATURE_ONLY: AblationOutcome.NOT_ANSWERED,
            AblationMode.COMBINED: AblationOutcome.ANSWERED,
        }
    )
    report = run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
        gold_answer_ids=["disease:migraine"],
    )
    for result in report.results:
        assert result.ablation_config_version == "ablation-v1"
        assert result.question_dependency_digest
        assert result.episode_id
        assert result.gold_hidden is True
        assert result.allowed_tools
        assert result.snapshot_ids
        assert result.trace_ids


def test_the_answer_correctness_is_compared_deterministically() -> None:
    runner = _ScriptedRunner(
        {
            AblationMode.KG_ONLY: AblationOutcome.ANSWERED,
            AblationMode.LITERATURE_ONLY: AblationOutcome.NOT_ANSWERED,
            AblationMode.COMBINED: AblationOutcome.ANSWERED,
        }
    )
    report = run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
        gold_answer_ids=["disease:migraine"],
    )
    kg = next(
        item for item in report.results if item.mode is AblationMode.KG_ONLY
    )
    assert kg.answer_correct is True


# ---- applying the route --------------------------------------------------


def _question():
    from medical_qa_data_factory.domain.common import utc_now
    from medical_qa_data_factory.domain.questions import QuestionSpec

    now = utc_now()
    return QuestionSpec(
        question_id="q1",
        logical_question_id="l1",
        question_version="v1",
        bundle_ids=["b1"],
        generation_mode="automatic",
        canonical_question="Which disease?",
        question_variants=[
            {
                "variant_id": "v1",
                "text": "Which disease?",
                "language": "en",
                "persona": "system",
                "style": "neutral",
                "generation_source": "agent",
                "semantic_check_status": "warning",
            }
        ],
        taxonomy={
            "domain_intent": "single_fact",
            "answer_form": "fact",
            "reasoning_pattern": ["single_hop"],
        },
        query_intent={},
        answerability="answerable",
        gold_answer={"answer_mode": "fact", "short_answer": "migraine"},
        evidence_requirements={"sufficiency_rule_version": "v1"},
        expected_route="hybrid",
        split_group_id="s1",
        created_at=now,
    )


def test_applying_the_route_writes_the_verified_route() -> None:
    runner = _ScriptedRunner(
        {
            AblationMode.KG_ONLY: AblationOutcome.NOT_ANSWERED,
            AblationMode.LITERATURE_ONLY: AblationOutcome.NOT_ANSWERED,
            AblationMode.COMBINED: AblationOutcome.ANSWERED,
        }
    )
    report = run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
        required_evidence_mode=RequiredEvidenceMode.HYBRID_PER_HOP,
    )
    updated = apply_verified_route(_question(), report)
    assert updated.verified_route is Route.HYBRID
    assert updated.ablation_results


def test_applying_the_route_does_not_change_the_expected_route() -> None:
    """The ablation finds the question's route, not the run's obligation."""
    runner = _ScriptedRunner(
        {
            AblationMode.KG_ONLY: AblationOutcome.ANSWERED,
            AblationMode.LITERATURE_ONLY: AblationOutcome.NOT_ANSWERED,
            AblationMode.COMBINED: AblationOutcome.ANSWERED,
        }
    )
    report = run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
        required_evidence_mode=RequiredEvidenceMode.HYBRID_PER_HOP,
    )
    updated = apply_verified_route(_question(), report)
    assert updated.verified_route is Route.KG
    # The generation requirement is untouched.
    assert report.required_evidence_mode is RequiredEvidenceMode.HYBRID_PER_HOP
    assert updated.expected_route is Route.HYBRID


def test_applying_the_route_records_the_change() -> None:
    runner = _ScriptedRunner(
        {
            AblationMode.KG_ONLY: AblationOutcome.NOT_ANSWERED,
            AblationMode.LITERATURE_ONLY: AblationOutcome.NOT_ANSWERED,
            AblationMode.COMBINED: AblationOutcome.ANSWERED,
        }
    )
    report = run_ablation(
        question_id="q1",
        question_version="v1",
        question_payload={"question": "?"},
        runner=runner,
    )
    updated = apply_verified_route(_question(), report)
    assert updated.changes
    assert updated.changes[-1].operation == "source_ablation"