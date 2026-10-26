import json
import shutil

import pytest

from medical_qa_data_factory.domain import (
    EndpointKind,
    ValidationCheck,
    ValidationLevel,
    ValidationSeverity,
    ValidationStatus,
)
from medical_qa_data_factory.export import (
    ExportBlockedError,
    build_export_plan,
    write_dataset_export,
)
from medical_qa_data_factory.domain.common import new_id
from medical_qa_data_factory.machine_validation import apply_machine_validation
from medical_qa_data_factory.repository import LocalArtifactRepository
from medical_qa_data_factory.review import InvalidReviewAction, review_question
from medical_qa_data_factory.validation import (
    affected_check_types,
    reset_stale_checks,
    unresolved_blocking_checks,
)

from test_review import build_result


def make_check(check_type: str, status: ValidationStatus) -> ValidationCheck:
    return ValidationCheck(
        check_id=f"check-{check_type}",
        check_type=check_type,
        status=status,
        severity=ValidationSeverity.BLOCKING,
        message="from validator",
        validator_version="test.v1",
    )


def test_field_edit_resets_only_dependent_checks():
    checks = [
        make_check("question_leakage", ValidationStatus.PASSED),
        make_check("route_plausibility", ValidationStatus.PASSED),
    ]

    updated = reset_stale_checks(checks, {"gold_answer"})

    statuses = {item.check_type: item.status for item in updated}
    assert statuses["question_leakage"] is ValidationStatus.NOT_RUN
    assert statuses["route_plausibility"] is ValidationStatus.PASSED


def test_reset_preserves_human_confirmations():
    checks = [make_check("answer_correctness", ValidationStatus.PASSED)]
    updated = reset_stale_checks(checks, {"gold_answer"})
    assert updated[0].status is ValidationStatus.PASSED


def test_reset_never_passes_blocking_automated_check():
    checks = [make_check("duplicate_detection", ValidationStatus.FAILED)]
    updated = reset_stale_checks(checks, {"taxonomy"})
    assert updated[0].status is ValidationStatus.NOT_RUN


def test_unrelated_edit_keeps_all_checks():
    checks = [make_check("question_leakage", ValidationStatus.PASSED)]
    assert reset_stale_checks(checks, set())[0].status is ValidationStatus.PASSED


def test_affected_types_map_covers_leakage_and_duplicates():
    assert "question_leakage" in affected_check_types({"canonical_question"})
    assert "duplicate_detection" in affected_check_types({"gold_answer"})


def test_unresolved_blocking_checks_detects_not_run():
    checks = [
        make_check("question_leakage", ValidationStatus.NOT_RUN),
        make_check("route_plausibility", ValidationStatus.NOT_RUN),
    ]
    assert [item.check_type for item in unresolved_blocking_checks(checks)] == [
        "question_leakage"
    ]


def test_editing_gold_then_confirming_is_refused(tmp_path):
    question = build_result(tmp_path).questions[0]
    with_checks = question.model_copy(
        update={
            "validation_checks": [make_check("question_leakage", ValidationStatus.NOT_RUN)]
        }
    )

    with pytest.raises(InvalidReviewAction) as error:
        review_question(
            with_checks,
            "confirm",
            summary="想直接确认",
            changed_fields={"gold_answer"},
        )

    assert "question_leakage" in str(error.value)


def test_draft_only_workspace_cannot_formally_export(tmp_path):
    build_result(tmp_path)
    repository = LocalArtifactRepository(tmp_path)

    plan = build_export_plan(repository)

    assert plan.formal_ready == []
    assert plan.mock_only is True
    assert not plan.is_formal_exportable
    reasons = {item.reason for item in plan.blockers}
    assert {"machine_validated", "draft"} & reasons
    assert "mock_tools" in reasons
    with pytest.raises(ExportBlockedError):
        write_dataset_export(repository, tmp_path / "out")


def test_development_export_writes_watermarked_snapshot(tmp_path):
    build_result(tmp_path)
    repository = LocalArtifactRepository(tmp_path)

    target = write_dataset_export(repository, tmp_path / "out", development=True)

    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "development"
    assert manifest["contains_mock_tools"] is True
    assert "DEVELOPMENT SNAPSHOT" in manifest["warning"]
    assert manifest["question_count"] == 1
    questions = (target / "questions.jsonl").read_text(encoding="utf-8").strip()
    assert questions.startswith("{")


def test_confirmed_question_becomes_exportable(tmp_path):
    result = build_result(tmp_path)
    question = result.questions[0]
    confirmed = review_question(
        question,
        "confirm",
        summary="人工复核通过",
        gold_short_answer=question.gold_answer.short_answer,
    )
    repository = LocalArtifactRepository(tmp_path)
    repository.save_question(confirmed)

    plan = build_export_plan(repository)

    assert [item.question_id for item in plan.formal_ready] == [question.question_id]
    assert confirmed.validation_level is ValidationLevel.HUMAN_CONFIRMED


def test_unvalidated_confirmed_question_is_still_blocked(tmp_path):
    result = build_result(tmp_path)
    question = result.questions[0]
    # Drop the pipeline-written versions so only the forced record remains.
    shutil.rmtree(tmp_path / "questions")
    forced = question.model_copy(
        update={
            "validation_level": ValidationLevel.HUMAN_CONFIRMED,
            "question_version": new_id("version"),
            "validation_checks": [],
        }
    )
    repository = LocalArtifactRepository(tmp_path)
    repository.save_question(forced)

    plan = build_export_plan(repository)

    assert plan.formal_ready == []
    reasons = {item.reason for item in plan.blockers}
    assert "required_checks_not_run" in reasons


def test_excluded_records_do_not_block_the_qualifying_subset(tmp_path):
    result = build_result(tmp_path)
    question = result.questions[0]
    validated = apply_machine_validation(question, [result.bundles[0]])
    repository = LocalArtifactRepository(tmp_path)
    repository.save_question(
        review_question(validated, "confirm", summary="人工确认通过")
    )
    # Simulate a non-mock workspace so only the check gate decides.
    bundle = repository.load_latest_bundle(result.bundles[0].bundle_id)
    repository.save_bundle(
        bundle.model_copy(
            update={
                "bundle_version": f"{bundle.bundle_id}.v99",
                "tool_snapshots": [
                    snapshot.model_copy(update={"endpoint_kind": EndpointKind.PYTHON})
                    for snapshot in bundle.tool_snapshots
                ],
            }
        )
    )

    plan = build_export_plan(repository)

    assert [item.question_id for item in plan.formal_ready] == [question.question_id]


def test_returned_question_is_excluded_but_confirmed_one_is_kept(tmp_path):
    result = build_result(tmp_path)
    validated = apply_machine_validation(result.questions[0], [result.bundles[0]])
    repository = LocalArtifactRepository(tmp_path)
    returned = review_question(validated, "return", summary="证据不足，退回")
    repository.save_question(returned)
    bundle = repository.load_latest_bundle(result.bundles[0].bundle_id)
    repository.save_bundle(
        bundle.model_copy(
            update={
                "bundle_version": f"{bundle.bundle_id}.v99",
                "tool_snapshots": [
                    snapshot.model_copy(update={"endpoint_kind": EndpointKind.PYTHON})
                    for snapshot in bundle.tool_snapshots
                ],
            }
        )
    )

    plan = build_export_plan(repository)

    assert plan.formal_ready == []
    reasons = {item.reason for item in plan.blockers}
    assert "draft" in reasons


def test_manifest_records_required_checks_and_exclusions(tmp_path):
    result = build_result(tmp_path)
    validated = apply_machine_validation(
        result.questions[0], [result.bundles[0]]
    )
    repository = LocalArtifactRepository(tmp_path)
    repository.save_question(validated)
    repository.save_question(
        review_question(validated, "confirm", summary="人工确认")
    )
    bundle = repository.load_latest_bundle(result.bundles[0].bundle_id)
    repository.save_bundle(
        bundle.model_copy(
            update={
                "bundle_version": f"{bundle.bundle_id}.v99",
                "tool_snapshots": [
                    snapshot.model_copy(update={"endpoint_kind": EndpointKind.PYTHON})
                    for snapshot in bundle.tool_snapshots
                ]
            }
        )
    )

    target = write_dataset_export(repository, tmp_path / "out")

    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "human_confirmed"
    assert manifest["export_level"] == "human_confirmed"
    assert manifest["required_checks"]
    assert manifest["contains_mock_tools"] is False
    assert manifest["validation_levels"]["human_confirmed"] == 1
    question_id = validated.question_id
    recorded = manifest["per_question_checks"][question_id]
    assert {item["check_type"] for item in recorded} == set(manifest["required_checks"])
    assert all(item["status"] == "passed" for item in recorded)


