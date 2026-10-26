"""The review commands and the export levels.

Step 10 of the real-generation-agent plan. The properties under test are that
a reviewer may keep two candidates, that a variant cannot be its own primary
or form a cycle, and that the three export levels have separate predicates.
"""
from __future__ import annotations

from typing import Any

import pytest

from medical_qa_data_factory.domain.common import utc_now
from medical_qa_data_factory.domain.questions import (
    QuestionSpec,
    ReviewDisposition,
)
from medical_qa_data_factory.generation.review_operations import (
    ReviewCommand,
    ReviewCommandError,
    ReviewOperation,
    ReviewOperationStatus,
    apply_keep_both,
    apply_keep_independent,
    apply_reject,
    apply_revise_or_return,
    apply_set_variant_of,
    check_expected_versions,
)


def _question(
    question_id: str = "q1",
    *,
    disposition: ReviewDisposition = ReviewDisposition.PENDING,
    variant_of: str | None = None,
    group: str | None = "group.abc",
    split_group_id: str = "s1",
    dataset_split: str | None = "train",
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
        },
        query_intent={},
        answerability="answerable",
        gold_answer={"answer_mode": "fact", "short_answer": "x"},
        evidence_requirements={"sufficiency_rule_version": "v1"},
        expected_route="kg",
        split_group_id=split_group_id,
        dataset_split=dataset_split,
        review_group_key=group,
        review_disposition=disposition,
        variant_of_question_id=variant_of,
        created_at=utc_now(),
    )


# ---- the simple commands -------------------------------------------------


def test_keep_independent_sets_the_disposition() -> None:
    outcome = apply_keep_independent(_question())
    assert outcome.questions[0].review_disposition is (
        ReviewDisposition.KEEP_INDEPENDENT
    )
    assert outcome.operation.status is ReviewOperationStatus.COMMITTED


def test_reject_sets_the_disposition() -> None:
    outcome = apply_reject(_question())
    assert outcome.questions[0].review_disposition is ReviewDisposition.REJECTED


def test_revise_resets_the_disposition_to_pending() -> None:
    outcome = apply_revise_or_return(
        _question(disposition=ReviewDisposition.KEEP_INDEPENDENT),
        command=ReviewCommand.REVISE,
    )
    assert outcome.questions[0].review_disposition is ReviewDisposition.PENDING


def test_return_resets_the_disposition_to_pending() -> None:
    outcome = apply_revise_or_return(
        _question(disposition=ReviewDisposition.KEEP_INDEPENDENT),
        command=ReviewCommand.RETURN,
    )
    assert outcome.questions[0].review_disposition is ReviewDisposition.PENDING


def test_a_rejected_candidate_cannot_be_revised() -> None:
    with pytest.raises(ReviewCommandError) as excinfo:
        apply_revise_or_return(
            _question(disposition=ReviewDisposition.REJECTED),
            command=ReviewCommand.REVISE,
        )
    assert excinfo.value.code == "already_rejected"


def test_a_rejected_candidate_cannot_be_kept() -> None:
    with pytest.raises(ReviewCommandError) as excinfo:
        apply_keep_independent(_question(disposition=ReviewDisposition.REJECTED))
    assert excinfo.value.code == "already_rejected"


# ---- keep both -----------------------------------------------------------


def test_keep_both_keeps_two_candidates() -> None:
    outcome = apply_keep_both(_question("q1"), _question("q2"))
    assert len(outcome.questions) == 2
    assert all(
        item.review_disposition is ReviewDisposition.KEEP_INDEPENDENT
        for item in outcome.questions
    )


def test_keep_both_requires_the_same_group() -> None:
    with pytest.raises(ReviewCommandError) as excinfo:
        apply_keep_both(_question("q1", group="group.a"), _question("q2", group="group.b"))
    assert excinfo.value.code == "different_group"


# ---- set variant of ------------------------------------------------------


def _primary() -> QuestionSpec:
    return _question(
        "primary", disposition=ReviewDisposition.KEEP_INDEPENDENT
    )


def test_a_candidate_becomes_a_variant_of_the_primary() -> None:
    candidate = _question("candidate")
    outcome = apply_set_variant_of(candidate, _primary())
    updated = outcome.questions[0]
    assert updated.review_disposition is ReviewDisposition.VARIANT_OF
    assert updated.variant_of_question_id == "primary"


def test_the_variant_inherits_the_primary_split() -> None:
    candidate = _question("candidate", split_group_id="s2", dataset_split="test")
    outcome = apply_set_variant_of(candidate, _primary())
    updated = outcome.questions[0]
    assert updated.split_group_id == "s1"
    assert updated.dataset_split == "train"
    assert updated.logical_question_id == "l-primary"


def test_a_candidate_cannot_be_a_variant_of_itself() -> None:
    primary = _primary()
    with pytest.raises(ReviewCommandError) as excinfo:
        apply_set_variant_of(primary, primary)
    assert excinfo.value.code == "self_reference"


def test_a_variant_must_be_in_the_same_group() -> None:
    candidate = _question("candidate", group="group.other")
    with pytest.raises(ReviewCommandError) as excinfo:
        apply_set_variant_of(candidate, _primary())
    assert excinfo.value.code == "different_group"


def test_the_primary_must_be_kept_independently() -> None:
    candidate = _question("candidate")
    with pytest.raises(ReviewCommandError) as excinfo:
        apply_set_variant_of(candidate, _question("primary"))
    assert excinfo.value.code == "primary_not_kept"


def test_a_variant_of_a_variant_is_refused() -> None:
    primary = _primary().model_copy(update={"variant_of_question_id": "root"})
    with pytest.raises(ReviewCommandError) as excinfo:
        apply_set_variant_of(_question("candidate"), primary)
    assert excinfo.value.code == "primary_is_variant"


def test_a_candidate_that_is_already_a_variant_is_refused() -> None:
    candidate = _question(
        "candidate",
        disposition=ReviewDisposition.VARIANT_OF,
        variant_of="other",
    )
    with pytest.raises(ReviewCommandError) as excinfo:
        apply_set_variant_of(candidate, _primary())
    assert excinfo.value.code == "already_variant"


def test_a_cycle_is_refused() -> None:
    primary = _primary()
    candidate = _question("candidate")
    # The primary already points at the candidate: the relation would close.
    cyclic = primary.model_copy(update={"variant_of_question_id": "candidate"})
    with pytest.raises(ReviewCommandError) as excinfo:
        apply_set_variant_of(
            candidate, cyclic, all_questions=[cyclic, candidate]
        )
    assert excinfo.value.code == "cycle"


def test_the_operation_records_both_versions() -> None:
    outcome = apply_set_variant_of(_question("candidate"), _primary())
    operation = outcome.operation
    assert operation.command is ReviewCommand.SET_VARIANT_OF
    assert set(operation.expected_versions) == {"candidate", "primary"}
    assert set(operation.written_versions) == {"candidate", "primary"}
    assert len(operation.completed_steps) == 2


# ---- the optimistic check ------------------------------------------------


def test_a_version_mismatch_aborts_the_operation() -> None:
    operation = ReviewOperation(
        operation_id="op1",
        operation_version="v1",
        command=ReviewCommand.SET_VARIANT_OF,
        subject_question_id="q1",
        target_question_id="q2",
        expected_versions={"q1": "q1.v1"},
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    with pytest.raises(ReviewCommandError) as excinfo:
        check_expected_versions(operation, {"q1": "q1.v2"})
    assert excinfo.value.code == "version_conflict"


def test_a_matching_version_passes_the_check() -> None:
    operation = ReviewOperation(
        operation_id="op1",
        operation_version="v1",
        command=ReviewCommand.KEEP_INDEPENDENT,
        subject_question_id="q1",
        expected_versions={"q1": "q1.v1"},
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    check_expected_versions(operation, {"q1": "q1.v1"})