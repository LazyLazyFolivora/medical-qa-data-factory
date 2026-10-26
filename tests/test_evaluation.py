"""The pinned acceptance manifest and the report.

Step 11 of the real-generation-agent plan. The properties under test are that
the manifest pins what a run depends on, and that the report says what
happened rather than what was hoped for.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from medical_qa_data_factory.domain.questions import (
    QuestionSpec,
    ReviewDisposition,
)
from medical_qa_data_factory.domain.common import utc_now
from medical_qa_data_factory.evaluation.manifest import (
    EVAL_MANIFEST_VERSION,
    EvalGraphSnapshot,
    EvalManifest,
    EvalModelConfig,
    EvalRagSnapshot,
    EvalSeed,
    load_eval_manifest,
    verify_against_environment,
    write_eval_manifest,
)
from medical_qa_data_factory.evaluation.runner import (
    AcceptanceReport,
    attach_seed_details,
    build_report,
    finalize,
    summarize_questions,
)


def _manifest(**overrides: Any) -> EvalManifest:
    payload: dict[str, Any] = {
        "manifest_id": "test-v1",
        "policy_version": "entity-generation-v1",
        "seeds": [
            EvalSeed(
                seed_id="s1",
                provider_id="primekg",
                native_id="DB01050",
                entity_type="drug",
                canonical_name="Ibuprofen",
            )
        ],
        "graph": EvalGraphSnapshot(
            provider_id="primekg",
            sha256="abc",
            data_snapshot_id="snap-1",
        ),
        "model": EvalModelConfig(
            provider_id="none",
            model_id="rule-based",
            config_hash="n/a",
        ),
        "rag": EvalRagSnapshot(
            provider_id="none",
            pinned=False,
            supports_read_chunk=False,
            exclusion_reason="no literature provider was available",
        ),
        "created_at": utc_now(),
    }
    payload.update(overrides)
    return EvalManifest(**payload)


def _question(
    question_id: str = "q1",
    *,
    disposition: ReviewDisposition = ReviewDisposition.PENDING,
    group: str | None = "group.abc",
    hops: int = 1,
    route: str = "kg",
) -> QuestionSpec:
    return QuestionSpec(
        question_id=question_id,
        logical_question_id=f"l-{question_id}",
        question_version=f"{question_id}.v1",
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
            "hop_complexity": hops,
        },
        query_intent={},
        answerability="answerable",
        gold_answer={"answer_mode": "fact", "short_answer": "x"},
        evidence_requirements={"sufficiency_rule_version": "v1"},
        expected_route=route,
        split_group_id="s1",
        review_group_key=group,
        review_disposition=disposition,
        created_at=utc_now(),
    )


# ---- the manifest --------------------------------------------------------


def test_a_manifest_round_trips(tmp_path: Path) -> None:
    manifest = _manifest()
    path = write_eval_manifest(manifest, tmp_path / "m.json")
    loaded = load_eval_manifest(path)
    assert loaded.manifest_id == manifest.manifest_id
    assert loaded.seeds[0].native_id == "DB01050"


def test_the_manifest_hash_is_stable() -> None:
    assert _manifest().manifest_hash() == _manifest().manifest_hash()


def test_the_manifest_hash_changes_with_the_content() -> None:
    first = _manifest()
    second = _manifest(policy_version="other")
    assert first.manifest_hash() != second.manifest_hash()


def test_a_manifest_must_have_at_least_one_seed() -> None:
    with pytest.raises(ValueError):
        _manifest(seeds=[])


def test_the_manifest_records_the_pinned_graph_digest() -> None:
    manifest = _manifest()
    assert manifest.graph.sha256 == "abc"
    assert manifest.graph.data_snapshot_id == "snap-1"


def test_a_manifest_without_a_pinned_rag_is_not_formal_eligible() -> None:
    assert not _manifest().formal_rag_eligible()


def test_a_manifest_with_a_pinned_rag_is_formal_eligible() -> None:
    manifest = _manifest(
        rag=EvalRagSnapshot(
            provider_id="weknora",
            snapshot_id="corpus-1",
            pinned=True,
            supports_read_chunk=True,
        )
    )
    assert manifest.formal_rag_eligible()


def test_a_manifest_with_a_snapshot_but_no_read_back_is_not_eligible() -> None:
    manifest = _manifest(
        rag=EvalRagSnapshot(
            provider_id="weknora",
            snapshot_id="corpus-1",
            pinned=True,
            supports_read_chunk=False,
        )
    )
    assert not manifest.formal_rag_eligible()


def test_a_snapshot_mismatch_is_reported() -> None:
    problems = verify_against_environment(
        _manifest(), graph_snapshot_id="snap-other"
    )
    assert problems
    assert "snap-other" in problems[0]


def test_a_matching_environment_has_no_problems() -> None:
    assert not verify_against_environment(
        _manifest(), graph_snapshot_id="snap-1"
    )


def test_the_schema_version_is_fixed() -> None:
    assert EVAL_MANIFEST_VERSION == "eval-manifest-v1"


# ---- the report ----------------------------------------------------------


def test_a_report_records_the_manifest_it_came_from() -> None:
    manifest = _manifest()
    report = build_report(manifest, mode="rule-based")
    assert report.manifest_id == manifest.manifest_id
    assert report.manifest_hash == manifest.manifest_hash()


def test_a_run_that_produced_nothing_failed() -> None:
    report = finalize(build_report(_manifest(), mode="rule-based"))
    assert report.verdict == "failed"
    assert "no question" in report.notes[0]


def test_an_environment_mismatch_makes_the_run_not_reproducible() -> None:
    report = build_report(
        _manifest(), mode="rule-based", environment_problems=["the graph moved"]
    )
    report = finalize(report)
    assert report.verdict == "not_reproducible"


def test_a_run_with_questions_passes() -> None:
    report = build_report(_manifest(), mode="rule-based")
    summarize_questions(report, [_question()])
    report = finalize(report)
    assert report.verdict == "passed"
    assert report.question_count == 1


def test_the_report_counts_the_verified_routes() -> None:
    """The report counts the route the ablation verified, not the expected one.

    A question that has not been through the ablation has no verified route
    yet, and the report says "unknown" rather than borrowing the expected one.
    """
    report = build_report(_manifest(), mode="rule-based")
    summarize_questions(
        report, [_question("q1", route="kg"), _question("q2", route="hybrid")]
    )
    assert report.route_counts == {"unknown": 2}


def test_the_report_counts_the_groups() -> None:
    report = build_report(_manifest(), mode="rule-based")
    summarize_questions(
        report,
        [
            _question("q1", group="g1"),
            _question("q2", group="g1"),
            _question("q3", group="g2"),
        ],
    )
    assert report.group_count == 2
    assert report.group_sizes == [2, 1]


def test_the_report_counts_the_dispositions() -> None:
    report = build_report(_manifest(), mode="rule-based")
    summarize_questions(
        report,
        [
            _question("q1", disposition=ReviewDisposition.KEEP_INDEPENDENT),
            _question("q2", disposition=ReviewDisposition.PENDING),
        ],
    )
    assert report.kept_count == 1
    assert report.pending_count == 1
    assert report.kept_rate == 0.5


def test_the_report_records_the_hop_complexities() -> None:
    report = build_report(_manifest(), mode="rule-based")
    summarize_questions(report, [_question("q1", hops=1), _question("q2", hops=3)])
    assert report.hop_complexities == [1, 3]


def test_the_report_says_a_kept_rate_of_zero_when_nothing_was_decided() -> None:
    report = build_report(_manifest(), mode="rule-based")
    assert report.kept_rate == 0.0


def test_the_seed_details_report_the_hops_and_the_gaps() -> None:
    report = build_report(_manifest(), mode="rule-based")
    report.seed_outcomes.append(
        type(report.seed_outcomes[0]) if report.seed_outcomes else _seed_outcome()
    )
    from medical_qa_data_factory.evaluation.runner import SeedOutcome

    report.seed_outcomes = [
        SeedOutcome(seed_id="s1", canonical_name="Ibuprofen", question_ids=["q1"])
    ]
    attach_seed_details(report, [_question("q1", hops=3)])
    assert report.seed_outcomes[0].hops_observed == [3]


def _seed_outcome():
    from medical_qa_data_factory.evaluation.runner import SeedOutcome

    return SeedOutcome(seed_id="s", canonical_name="c")


def test_the_report_is_written_as_json(tmp_path: Path) -> None:
    report = build_report(_manifest(), mode="rule-based")
    summarize_questions(report, [_question()])
    finalize(report)
    path = report.write(tmp_path / "report.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["manifest_id"] == "test-v1"
    assert "seed_outcomes" in payload
    assert "notes" in payload