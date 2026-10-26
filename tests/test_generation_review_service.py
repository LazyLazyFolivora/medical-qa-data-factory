"""The review service: journalled commands and reconciliation.

Step 10 of the real-generation-agent plan. The property under test is that an
interrupted composite operation leaves no dangling state.
"""
from __future__ import annotations

from pathlib import Path
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
)
from medical_qa_data_factory.generation.review_service import ReviewService
from medical_qa_data_factory.generation.repository import GenerationRepository
from medical_qa_data_factory.repository import LocalArtifactRepository


def _question(
    question_id: str = "q1",
    *,
    disposition: ReviewDisposition = ReviewDisposition.PENDING,
    group: str | None = "group.abc",
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
        split_group_id="s1",
        review_group_key=group,
        review_disposition=disposition,
        created_at=utc_now(),
    )


def _service(tmp_path: Path) -> tuple[ReviewService, GenerationRepository]:
    repository = GenerationRepository(tmp_path / "ws")
    return ReviewService(repository=repository), repository


def test_a_command_is_journalled_and_committed(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    outcome = service.run_command(
        ReviewCommand.KEEP_INDEPENDENT, subject=_question()
    )
    assert outcome.operation.status is ReviewOperationStatus.COMMITTED
    operations = repository.list_review_operations()
    assert len(operations) == 1
    assert operations[0].command is ReviewCommand.KEEP_INDEPENDENT


def test_a_failed_command_is_journalled_as_failed(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    with pytest.raises(ReviewCommandError):
        service.run_command(
            ReviewCommand.SET_VARIANT_OF,
            subject=_question("q1"),
            target=_question("q2", group="group.other"),
        )
    operations = repository.list_review_operations()
    assert len(operations) == 1
    assert operations[0].status is ReviewOperationStatus.FAILED
    assert operations[0].error_code == "different_group"


def test_an_interrupted_operation_is_found_by_reconciliation(
    tmp_path: Path,
) -> None:
    service, repository = _service(tmp_path)
    # The records are still where the operation expects them, so this is an
    # interruption rather than a conflict.
    repository.artifacts.save_question(_question("q1"))
    repository.artifacts.save_question(_question("q2"))
    pending = ReviewOperation(
        operation_id="reviewop_interrupted",
        operation_version="v1",
        command=ReviewCommand.SET_VARIANT_OF,
        status=ReviewOperationStatus.PENDING,
        subject_question_id="q1",
        target_question_id="q2",
        expected_versions={"q1": "q1.v1", "q2": "q2.v1"},
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    repository.save_review_operation(pending)
    assert repository.list_unfinished_review_operations()
    settled = service.reconcile()
    assert len(settled) == 1
    assert settled[0].status is ReviewOperationStatus.FAILED
    assert settled[0].error_code == "interrupted"
    assert not repository.list_unfinished_review_operations()


def test_reconciliation_rolls_back_a_dangling_variant(
    tmp_path: Path,
) -> None:
    """A candidate that points at a primary that does not know about it."""
    service, repository = _service(tmp_path)
    candidate = _question("candidate")
    primary = _question("primary", disposition=ReviewDisposition.KEEP_INDEPENDENT)
    repository.artifacts.save_question(candidate)
    repository.artifacts.save_question(primary)
    pending = ReviewOperation(
        operation_id="reviewop_dangling",
        operation_version="v1",
        command=ReviewCommand.SET_VARIANT_OF,
        status=ReviewOperationStatus.PENDING,
        subject_question_id="candidate",
        target_question_id="primary",
        expected_versions={
            "candidate": "candidate.v1",
            "primary": "primary.v1",
        },
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    repository.save_review_operation(pending)
    settled = service.reconcile()
    assert settled[0].status is ReviewOperationStatus.FAILED
    # The candidate is not left pointing at the primary.
    current = repository.artifacts.load_latest_question("candidate")
    assert current.review_disposition is not ReviewDisposition.VARIANT_OF


def test_reconciliation_leaves_a_committed_operation_alone(
    tmp_path: Path,
) -> None:
    service, repository = _service(tmp_path)
    service.run_command(ReviewCommand.KEEP_INDEPENDENT, subject=_question())
    assert service.reconcile() == []
    assert len(repository.list_review_operations()) == 1


def test_reconciliation_does_not_touch_a_committed_operation(
    tmp_path: Path,
) -> None:
    service, repository = _service(tmp_path)
    service.run_command(ReviewCommand.REJECT, subject=_question())
    operations = repository.list_review_operations()
    assert operations[0].status is ReviewOperationStatus.COMMITTED
    assert service.reconcile() == []


def test_the_journal_records_the_expected_versions(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)
    service.run_command(
        ReviewCommand.KEEP_BOTH,
        subject=_question("q1"),
        target=_question("q2"),
    )
    operation = repository.list_review_operations()[0]
    assert set(operation.expected_versions) == {"q1", "q2"}
    assert operation.completed_steps


def test_a_version_conflict_is_detected_by_reconciliation(
    tmp_path: Path,
) -> None:
    """The records moved on, so the operation cannot be applied."""
    service, repository = _service(tmp_path)
    question = _question("q1")
    repository.artifacts.save_question(question)
    pending = ReviewOperation(
        operation_id="reviewop_stale",
        operation_version="v1",
        command=ReviewCommand.KEEP_INDEPENDENT,
        status=ReviewOperationStatus.PENDING,
        subject_question_id="q1",
        expected_versions={"q1": "q1.v1"},
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    repository.save_review_operation(pending)
    # The question moves on.
    repository.artifacts.save_question(
        question.model_copy(update={"question_version": "q1.v2"})
    )
    settled = service.reconcile()
    assert settled[0].status is ReviewOperationStatus.FAILED
    assert settled[0].error_code == "version_conflict"


def test_a_missing_target_is_refused(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ReviewCommandError) as excinfo:
        service.run_command(
            ReviewCommand.KEEP_BOTH, subject=_question("q1")
        )
    assert excinfo.value.code == "missing_target"