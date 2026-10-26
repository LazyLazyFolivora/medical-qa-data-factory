import pytest

from medical_qa_data_factory.domain import (
    Answerability,
    Route,
    ValidationLevel,
    ValidationStatus,
)
from medical_qa_data_factory.machine_validation import (
    REQUIRED_FOR_FORMAL_EXPORT,
    apply_machine_validation,
    assess_formal_export,
    automated_checks_passed,
    check_requirements,
)
from medical_qa_data_factory.review import InvalidReviewAction, review_question
from medical_qa_data_factory.validators import (
    BLOCKING_FOR_MACHINE_VALIDATION,
    jaccard,
    run_all_validators,
)

from test_review import build_result


def unvalidated(tmp_path):
    """The raw draft straight from the agent, before any validator ran."""
    result = build_result(tmp_path)
    question = result.questions[0]
    return result.bundles[0], question.model_copy(
        update={"validation_checks": [], "validation_level": ValidationLevel.DRAFT}
    )


def validated(tmp_path):
    result = build_result(tmp_path)
    question = result.questions[0]
    return result.bundles[0], apply_machine_validation(question, [result.bundles[0]])


def statuses(checks):
    return {check.check_type: check.status for check in checks}


def test_every_required_check_is_actually_executed(tmp_path):
    bundle, question = validated(tmp_path)

    recorded = statuses(question.validation_checks)

    assert set(recorded) >= set(BLOCKING_FOR_MACHINE_VALIDATION)
    assert recorded["schema_references"] is ValidationStatus.PASSED
    assert recorded["question_leakage"] is ValidationStatus.PASSED
    assert recorded["evidence_support"] is ValidationStatus.PASSED
    assert all(
        check.validator_version.startswith("validators.")
        for check in question.validation_checks
    )


def test_machine_validation_promotes_level_and_records_audit(tmp_path):
    bundle, question = validated(tmp_path)

    assert question.validation_level is ValidationLevel.MACHINE_VALIDATED
    assert question.changes[-1].operation == "machine_validation"
    assert question.changes[-1].parent_version
    assert question.question_version != question.changes[-1].parent_version
    assert not any(
        check.check_type == "human_confirmation" for check in question.validation_checks
    )


def test_leaking_question_fails_leakage_and_stays_draft(tmp_path):
    result = build_result(tmp_path)
    question = result.questions[0]
    leaking = question.model_copy(
        update={
            "canonical_question": f"aspirin 的靶点是 {question.gold_answer.short_answer} 吗？",
            "question_variants": [
                item.model_copy(
                    update={
                        "text": f"aspirin 的靶点是 {question.gold_answer.short_answer} 吗？"
                    }
                )
                for item in question.question_variants
            ],
        }
    )

    updated = apply_machine_validation(leaking, [result.bundles[0]])

    assert statuses(updated.validation_checks)["question_leakage"] is ValidationStatus.FAILED
    assert updated.validation_level is ValidationLevel.DRAFT


def test_near_duplicate_in_same_split_group_is_flagged_not_failed(tmp_path):
    """A candidate that looks like another one is a signal, not a verdict.

    The reviewer may keep both, so the check may not block the machine
    validation.
    """
    result = build_result(tmp_path)
    question = result.questions[0]
    peer = question.model_copy(
        update={
            "question_id": "peer-question",
            "logical_question_id": "peer-logical",
            "canonical_question": question.canonical_question,
        }
    )

    checks = run_all_validators(question, [result.bundles[0]], peers=[peer])

    assert statuses(checks)["duplicate_detection"] is ValidationStatus.WARNING


def test_duplicate_detection_is_not_a_blocking_check():
    """A similar candidate may still reach the machine-validated level."""
    from medical_qa_data_factory.validators import (
        ADVISORY_CHECKS,
        BLOCKING_FOR_MACHINE_VALIDATION,
        DUPLICATE_DETECTION,
    )

    assert DUPLICATE_DETECTION in ADVISORY_CHECKS
    assert DUPLICATE_DETECTION not in BLOCKING_FOR_MACHINE_VALIDATION


def test_the_review_group_key_must_be_recomputable(tmp_path):
    """A key that cannot be recomputed from the structure is a defect."""
    from medical_qa_data_factory.validators import REVIEW_GROUP_INTEGRITY

    result = build_result(tmp_path)
    question = result.questions[0]
    checks = run_all_validators(question, [result.bundles[0]])
    # A question without grouping structure has nothing to recompute.
    assert statuses(checks)[REVIEW_GROUP_INTEGRITY] is ValidationStatus.PASSED


def test_unrelated_split_group_is_not_a_duplicate(tmp_path):
    result = build_result(tmp_path)
    question = result.questions[0]
    peer = question.model_copy(
        update={
            "question_id": "peer-question",
            "logical_question_id": "peer-logical",
            "split_group_id": "different-group",
            "canonical_question": question.canonical_question,
        }
    )

    checks = run_all_validators(question, [result.bundles[0]], peers=[peer])

    assert statuses(checks)["duplicate_detection"] is ValidationStatus.PASSED


def test_missing_required_evidence_ids_are_reported_not_ignored(tmp_path):
    result = build_result(tmp_path)
    question = result.questions[0]
    requirements = question.evidence_requirements.model_copy(
        update={"required_kg_evidence_ids": ["kg_does_not_exist"]}
    )

    checks = run_all_validators(
        question.model_copy(update={"evidence_requirements": requirements}),
        [result.bundles[0]],
    )

    recorded = statuses(checks)
    assert recorded["schema_references"] is ValidationStatus.FAILED
    assert recorded["evidence_sufficiency"] is ValidationStatus.FAILED
    assert "kg_does_not_exist" in next(
        check.message
        for check in checks
        if check.check_type == "schema_references"
    )


def test_kg_route_requires_kg_evidence(tmp_path):
    bundle, question = unvalidated(tmp_path)
    requirements = question.evidence_requirements.model_copy(
        update={"required_kg_evidence_ids": []}
    )

    checks = run_all_validators(
        question.model_copy(update={"evidence_requirements": requirements}), [bundle]
    )

    recorded = statuses(checks)
    assert recorded["route_plausibility"] is ValidationStatus.FAILED
    assert recorded["answer_evidence_consistency"] is ValidationStatus.FAILED


def test_missing_check_records_are_not_run_not_passed(tmp_path):
    bundle, question = unvalidated(tmp_path)

    requirements = check_requirements(list(question.validation_checks))

    assert len(requirements) == len(REQUIRED_FOR_FORMAL_EXPORT)
    assert all(item.status is ValidationStatus.NOT_RUN for item in requirements)
    assert all(item.validator_version is None for item in requirements)
    assert automated_checks_passed(question) is False


def test_confirm_is_refused_until_required_checks_pass(tmp_path):
    bundle, question = unvalidated(tmp_path)

    with pytest.raises(InvalidReviewAction) as error:
        review_question(question, "confirm", summary="想直接确认")

    assert "schema_references" in str(error.value)


def test_confirmed_question_with_passed_checks_is_exportable(tmp_path):
    bundle, validated_question = validated(tmp_path)
    confirmed = review_question(
        validated_question,
        "confirm",
        summary="人工复核通过",
    )

    assessment = assess_formal_export(confirmed)

    assert assessment.satisfied is True
    assert assessment.missing == []
    assert confirmed.validation_level is ValidationLevel.HUMAN_CONFIRMED


def test_human_click_cannot_satisfy_an_automated_check(tmp_path):
    bundle, validated_question = validated(tmp_path)
    stale = validated_question.model_copy(
        update={
            "validation_checks": [
                check.model_copy(
                    update={"status": ValidationStatus.NOT_RUN, "message": "待重跑"}
                )
                if check.check_type == "question_leakage"
                else check
                for check in validated_question.validation_checks
            ]
        }
    )

    with pytest.raises(InvalidReviewAction):
        review_question(stale, "confirm", summary="人工判断没问题")


def test_unanswerable_question_needs_an_empty_gold_answer(tmp_path):
    result = build_result(tmp_path)
    question = result.questions[0]
    unanswerable = question.model_copy(
        update={
            "answerability": Answerability.UNANSWERABLE,
            "expected_route": Route.UNANSWERABLE,
        }
    )

    checks = run_all_validators(unanswerable, [result.bundles[0]])

    assert statuses(checks)["answer_evidence_consistency"] is ValidationStatus.FAILED


def test_jaccard_is_symmetric_and_bounded():
    assert jaccard("aspirin target", "aspirin target") == pytest.approx(1.0)
    assert jaccard("aspirin", "") == 0.0
    assert jaccard("abc", "abd") == jaccard("abd", "abc")
