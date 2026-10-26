"""The three export levels.

Step 10 of the real-generation-agent plan. Each level has its own predicate,
and the level is chosen explicitly.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from medical_qa_data_factory.domain import (
    EndpointKind,
    ValidationLevel,
)
from medical_qa_data_factory.domain.questions import ReviewDisposition
from medical_qa_data_factory.export import (
    ExportBlockedError,
    ExportLevel,
    build_export_plan,
    select_for_level,
    write_dataset_export,
)
from medical_qa_data_factory.machine_validation import apply_machine_validation
from medical_qa_data_factory.repository import LocalArtifactRepository
from medical_qa_data_factory.review import review_question


def _plan_with(
    tmp_path: Path,
    *,
    level: ValidationLevel = ValidationLevel.MACHINE_VALIDATED,
    disposition: ReviewDisposition | None = None,
    endpoint: EndpointKind = EndpointKind.PYTHON,
) -> tuple[LocalArtifactRepository, Any]:
    """Build a workspace holding a question at one validation level.

    The demo pipeline already machine-validates its question, so a ``DRAFT``
    level means the question is put back to draft. ``disposition`` overrides
    the disposition the level would otherwise have.
    """
    from test_validation_export import build_result

    result = build_result(tmp_path)
    repository = LocalArtifactRepository(tmp_path)
    question = result.questions[0]
    from medical_qa_data_factory.domain.common import new_id

    if level is ValidationLevel.DRAFT:
        question = question.model_copy(
            update={
                "validation_level": ValidationLevel.DRAFT,
                "question_version": new_id("version"),
            }
        )
        repository.save_question(question)
    else:
        validated = apply_machine_validation(
            result.questions[0], [result.bundles[0]]
        )
        question = validated
        if level is ValidationLevel.HUMAN_CONFIRMED:
            question = review_question(validated, "confirm", summary="人工确认")
        repository.save_question(question)
    bundle = repository.load_latest_bundle(result.bundles[0].bundle_id)
    repository.save_bundle(
        bundle.model_copy(
            update={
                "bundle_version": f"{bundle.bundle_id}.v99",
                "tool_snapshots": [
                    snapshot.model_copy(update={"endpoint_kind": endpoint})
                    for snapshot in bundle.tool_snapshots
                ],
            }
        )
    )
    # Set the disposition the way the review operations would.
    if disposition is not None:
        from medical_qa_data_factory.domain.common import ChangeRecord, utc_now
        from medical_qa_data_factory.generation.review_operations import (
            ReviewCommand,
            apply_keep_independent,
            apply_reject,
            apply_revise_or_return,
        )

        if disposition is ReviewDisposition.VARIANT_OF:
            # The candidate is a variant of another, independent question.
            version = new_id("version")
            question = question.model_copy(
                update={
                    "review_disposition": ReviewDisposition.VARIANT_OF,
                    "variant_of_question_id": "primary",
                    "question_version": version,
                    "changes": [
                        *question.changes,
                        ChangeRecord(
                            change_id=new_id("change"),
                            actor_type="human",
                            operation="review_disposition",
                            summary="made a variant of primary",
                            created_at=utc_now(),
                            parent_version=question.question_version,
                            new_version=version,
                        ),
                    ],
                }
            )
            repository.save_question(question)
        elif disposition is ReviewDisposition.REJECTED:
            outcome = apply_reject(question)
            for updated in outcome.questions:
                repository.save_question(updated)
            question = outcome.questions[-1]
        elif disposition is ReviewDisposition.PENDING:
            outcome = apply_revise_or_return(question, command=ReviewCommand.RETURN)
            for updated in outcome.questions:
                repository.save_question(updated)
            question = outcome.questions[-1]
        else:
            outcome = apply_keep_independent(question)
            for updated in outcome.questions:
                repository.save_question(updated)
            question = outcome.questions[-1]
    return repository, question


def test_the_development_level_includes_everything(tmp_path: Path) -> None:
    repository, _ = _plan_with(
        tmp_path, level=ValidationLevel.DRAFT, endpoint=EndpointKind.MOCK
    )
    plan = build_export_plan(repository)
    included, excluded = select_for_level(plan, ExportLevel.DEVELOPMENT)
    assert included
    assert excluded == []


def test_the_machine_level_excludes_a_draft_question(tmp_path: Path) -> None:
    repository, _ = _plan_with(tmp_path, level=ValidationLevel.DRAFT)
    plan = build_export_plan(repository)
    included, excluded = select_for_level(plan, ExportLevel.MACHINE_VALIDATED)
    assert included == []
    assert excluded


def test_the_machine_level_includes_a_machine_validated_question(
    tmp_path: Path,
) -> None:
    repository, _ = _plan_with(tmp_path, level=ValidationLevel.MACHINE_VALIDATED)
    plan = build_export_plan(repository)
    included, excluded = select_for_level(plan, ExportLevel.MACHINE_VALIDATED)
    assert len(included) == 1
    assert excluded == []


def test_the_machine_level_excludes_a_mock_backed_question(tmp_path: Path) -> None:
    repository, _ = _plan_with(
        tmp_path, level=ValidationLevel.MACHINE_VALIDATED, endpoint=EndpointKind.MOCK
    )
    plan = build_export_plan(repository)
    included, excluded = select_for_level(plan, ExportLevel.MACHINE_VALIDATED)
    assert included == []
    assert any("mock" in reason for _, reason in excluded)


def test_the_human_level_requires_a_resolved_disposition(tmp_path: Path) -> None:
    repository, _ = _plan_with(
        tmp_path,
        level=ValidationLevel.HUMAN_CONFIRMED,
        disposition=ReviewDisposition.PENDING,
    )
    plan = build_export_plan(repository)
    included, excluded = select_for_level(plan, ExportLevel.HUMAN_CONFIRMED)
    assert included == []
    assert excluded


def test_the_human_level_includes_a_kept_question(tmp_path: Path) -> None:
    repository, _ = _plan_with(
        tmp_path,
        level=ValidationLevel.HUMAN_CONFIRMED,
        disposition=ReviewDisposition.KEEP_INDEPENDENT,
    )
    plan = build_export_plan(repository)
    included, _ = select_for_level(plan, ExportLevel.HUMAN_CONFIRMED)
    assert len(included) == 1


def test_a_variant_is_never_exported_on_its_own(tmp_path: Path) -> None:
    repository, _ = _plan_with(
        tmp_path,
        level=ValidationLevel.HUMAN_CONFIRMED,
        disposition=ReviewDisposition.VARIANT_OF,
    )
    plan = build_export_plan(repository)
    included, excluded = select_for_level(plan, ExportLevel.HUMAN_CONFIRMED)
    assert included == []
    assert any("变体" in reason for _, reason in excluded)


def test_a_rejected_question_is_never_exported(tmp_path: Path) -> None:
    repository, _ = _plan_with(
        tmp_path,
        level=ValidationLevel.HUMAN_CONFIRMED,
        disposition=ReviewDisposition.REJECTED,
    )
    plan = build_export_plan(repository)
    included, _ = select_for_level(plan, ExportLevel.HUMAN_CONFIRMED)
    assert included == []


def test_the_level_must_be_chosen_explicitly(tmp_path: Path) -> None:
    repository, _ = _plan_with(tmp_path, level=ValidationLevel.DRAFT)
    # No level and no development flag defaults to the human-confirmed
    # level, which the draft question does not reach.
    with pytest.raises(ExportBlockedError):
        write_dataset_export(repository, tmp_path / "out")


def test_the_manifest_records_the_level(tmp_path: Path) -> None:
    repository, _ = _plan_with(
        tmp_path,
        level=ValidationLevel.HUMAN_CONFIRMED,
        disposition=ReviewDisposition.KEEP_INDEPENDENT,
    )
    target = write_dataset_export(
        repository, tmp_path / "out", level=ExportLevel.HUMAN_CONFIRMED
    )
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["export_level"] == "human_confirmed"
    assert manifest["mode"] == "human_confirmed"
    assert manifest["review_dispositions"]
    assert "pending_review_groups" in manifest


def test_the_machine_level_manifest_records_pending_groups(tmp_path: Path) -> None:
    repository, _ = _plan_with(tmp_path, level=ValidationLevel.MACHINE_VALIDATED)
    target = write_dataset_export(
        repository, tmp_path / "out", level=ExportLevel.MACHINE_VALIDATED
    )
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["export_level"] == "machine_validated"
    assert manifest["excluded"] == []
    assert "pending_review_groups" in manifest


def test_the_development_level_is_watermarked(tmp_path: Path) -> None:
    repository, _ = _plan_with(
        tmp_path, level=ValidationLevel.DRAFT, endpoint=EndpointKind.MOCK
    )
    target = write_dataset_export(
        repository, tmp_path / "out", level=ExportLevel.DEVELOPMENT
    )
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["export_level"] == "development"
    assert "warning" in manifest
    assert manifest["contains_mock_tools"] is True
