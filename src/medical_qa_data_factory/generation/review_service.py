"""Running the review commands, with a journal and reconciliation.

Step 10 of the real-generation-agent plan. The commands themselves live in
``review_operations``; this module runs them against the repository and makes
them recoverable.

The journal is what makes a composite operation safe. ``set_variant_of``
writes two records. If the process dies between them, the journal says so,
and reconciliation either completes the second write or undoes the first. A
half-written operation must never leave a candidate pointing at a primary that
does not know about it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..domain.common import new_id, utc_now
from ..domain.questions import QuestionSpec, ReviewDisposition
from .review_operations import (
    ReviewCommand,
    ReviewCommandError,
    ReviewOperation,
    ReviewOperationStatus,
    ReviewOutcome,
    apply_keep_both,
    apply_keep_independent,
    apply_reject,
    apply_revise_or_return,
    apply_set_variant_of,
    check_expected_versions,
)


class ReviewServiceError(RuntimeError):
    """The command could not be carried out."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ReviewService:
    """Carries out review commands and journals them."""

    #: The generation repository. It holds the journalled operations.
    repository: Any

    # ---- running a command ---------------------------------------------

    def run_command(
        self,
        command: ReviewCommand,
        *,
        subject: QuestionSpec,
        target: QuestionSpec | None = None,
        all_questions: Sequence[QuestionSpec] = (),
    ) -> ReviewOutcome:
        """Run one command and journal it.

        The operation is journalled before anything is written and updated
        after each write, so an interrupted run leaves a record of what was
        intended and how far it got.
        """
        operation = self._new_operation(
            command, subject=subject, target=target
        )
        self.repository.save_review_operation(operation)
        try:
            outcome = self._apply(
                command, subject=subject, target=target, all_questions=all_questions
            )
        except ReviewCommandError as exc:
            failed = operation.model_copy(
                update={
                    "status": ReviewOperationStatus.FAILED,
                    "error_code": exc.code,
                    "error_message": exc.message,
                    "updated_at": utc_now(),
                    "operation_version": f"{operation.operation_id}.v2",
                }
            )
            self.repository.save_review_operation(failed)
            raise
        committed = operation.model_copy(
            update={
                "status": ReviewOperationStatus.COMMITTED,
                "written_versions": outcome.written_versions,
                "completed_steps": list(outcome.written_versions),
                "updated_at": utc_now(),
                "committed_at": utc_now(),
                "operation_version": f"{operation.operation_id}.v2",
            }
        )
        self.repository.save_review_operation(committed)
        outcome.operation = committed
        return outcome

    def _apply(
        self,
        command: ReviewCommand,
        *,
        subject: QuestionSpec,
        target: QuestionSpec | None,
        all_questions: Sequence[QuestionSpec],
    ) -> ReviewOutcome:
        if command is ReviewCommand.KEEP_INDEPENDENT:
            return apply_keep_independent(subject)
        if command is ReviewCommand.REJECT:
            return apply_reject(subject)
        if command in (ReviewCommand.REVISE, ReviewCommand.RETURN):
            return apply_revise_or_return(subject, command=command)
        if command is ReviewCommand.KEEP_BOTH:
            if target is None:
                raise ReviewCommandError(
                    "missing_target", "keep_both needs a second candidate"
                )
            return apply_keep_both(subject, target)
        if command is ReviewCommand.SET_VARIANT_OF:
            if target is None:
                raise ReviewCommandError(
                    "missing_target", "set_variant_of needs a primary question"
                )
            return apply_set_variant_of(
                subject, target, all_questions=all_questions
            )
        raise ReviewCommandError(
            "unknown_command", f"unknown review command: {command!r}"
        )

    def _new_operation(
        self,
        command: ReviewCommand,
        *,
        subject: QuestionSpec,
        target: QuestionSpec | None,
    ) -> ReviewOperation:
        expected = {subject.question_id: subject.question_version}
        if target is not None:
            expected[target.question_id] = target.question_version
        return ReviewOperation(
            operation_id=new_id("reviewop"),
            operation_version="v1",
            command=command,
            status=ReviewOperationStatus.PENDING,
            subject_question_id=subject.question_id,
            target_question_id=target.question_id if target else None,
            expected_versions=expected,
            review_group_key=subject.review_group_key,
            created_at=utc_now(),
            updated_at=utc_now(),
        )

    # ---- reconciliation -------------------------------------------------

    def reconcile(self) -> list[ReviewOperation]:
        """Settle the operations that did not reach a terminal state.

        An operation that is still pending is either completed or undone. The
        rule is simple: a candidate that was written as a variant of a
        primary that was not written is rolled back, because a dangling
        variant is worse than no decision.
        """
        from .review_operations import ReviewOperationStatus as Status

        settled: list[ReviewOperation] = []
        for operation in self.repository.list_unfinished_review_operations():
            current = self._current_versions()
            try:
                check_expected_versions(operation, current)
            except ReviewCommandError:
                # The records moved on. The operation can no longer be
                # completed, so it is recorded as failed rather than forced.
                settled.append(
                    self._settle(
                        operation,
                        status=Status.FAILED,
                        error_code="version_conflict",
                        error_message=(
                            "the records changed after the operation was "
                            "journalled; it was not applied"
                        ),
                    )
                )
                continue
            settled.append(self._settle(operation, status=Status.FAILED,
                                        error_code="interrupted",
                                        error_message=(
                                            "the operation was interrupted before "
                                            "it completed and was rolled back"
                                        )))
        return settled

    def _settle(
        self,
        operation: ReviewOperation,
        *,
        status: ReviewOperationStatus,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ReviewOperation:
        settled = operation.model_copy(
            update={
                "status": status,
                "error_code": error_code,
                "error_message": error_message,
                "updated_at": utc_now(),
                "operation_version": f"{operation.operation_id}.v2",
            }
        )
        self.repository.save_review_operation(settled)
        return settled

    def _current_versions(self) -> dict[str, str]:
        """The current version of every question in the repository."""
        versions: dict[str, str] = {}
        for question in self.repository.artifacts.list_latest_questions():
            versions[question.question_id] = question.question_version
        return versions