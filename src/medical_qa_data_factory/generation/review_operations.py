"""Review dispositions and the operations that change them.

Step 10 of the real-generation-agent plan. A reviewer's decisions inside a
group are the point of the whole grouping machinery, and they are the part
that is easiest to get wrong.

The stable disposition is one of four states:

    pending  ->  keep_independent  |  variant_of  |  rejected

The commands a reviewer issues are not dispositions. ``keep_both``,
``revise`` and ``return`` resolve into one of the four.

The hardest command is ``set_variant_of``. It is a composite write: the
primary question gains a variant and the candidate becomes that variant, and
both must be true or neither. It is therefore journalled as a
``ReviewOperation`` with an operation id, an optimistic expected-version check,
and a reconciliation step for the case where the process died halfway
through.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from pydantic import Field

from ..domain.common import (
    ActorType,
    ChangeRecord,
    ContractModel,
    UtcDateTime,
    new_id,
    utc_now,
)
from ..domain.questions import QuestionSpec, ReviewDisposition


#: The review operation version.
REVIEW_OPERATION_VERSION = "review-operation-v1"


class ReviewCommand(str, Enum):
    """The commands a reviewer may issue within a group."""

    KEEP_INDEPENDENT = "keep_independent"
    SET_VARIANT_OF = "set_variant_of"
    KEEP_BOTH = "keep_both"
    REVISE = "revise"
    RETURN = "return"
    REJECT = "reject"


class ReviewOperationStatus(str, Enum):
    """The state of a journalled review operation."""

    PENDING = "pending"
    COMMITTED = "committed"
    FAILED = "failed"


class ReviewOperation(ContractModel):
    """A journalled, composite review operation.

    The journal is what makes a half-written operation recoverable. It records
    what was intended, the versions it expected, and what it managed to do.
    """

    operation_id: str = Field(min_length=1)
    operation_version: str = Field(min_length=1)
    command: ReviewCommand
    status: ReviewOperationStatus = ReviewOperationStatus.PENDING
    #: The question the command was issued against.
    subject_question_id: str = Field(min_length=1)
    #: The other question, for the composite commands.
    target_question_id: str | None = None
    #: The versions the operation expected to find. A mismatch aborts it.
    expected_versions: dict[str, str] = Field(default_factory=dict)
    #: The review group the operation belongs to.
    review_group_key: str | None = None
    #: The versions the operation actually produced.
    written_versions: dict[str, str] = Field(default_factory=dict)
    #: The question ids the operation already completed.
    completed_steps: list[str] = Field(default_factory=list)
    actor_type: str = "human"
    summary: str = ""
    error_code: str | None = None
    error_message: str | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime
    committed_at: UtcDateTime | None = None


class ReviewCommandError(RuntimeError):
    """The command cannot be applied to the current state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ReviewOutcome:
    """The result of a review command."""

    operation: ReviewOperation
    #: The questions the command produced, in version order.
    questions: list[QuestionSpec] = field(default_factory=list)
    #: The versions that were written.
    written_versions: dict[str, str] = field(default_factory=dict)


def _disposition_update(
    question: QuestionSpec,
    *,
    disposition: ReviewDisposition,
    variant_of: str | None = None,
) -> dict[str, Any]:
    """The update that sets a disposition.

    A disposition change produces a new immutable version and records it in
    the change chain, so the version ancestry stays intact. The question's
    evidence checks are inherited unchanged: the disposition is not one of
    the fields a check depends on, so a review decision may not invalidate
    the evidence verification that preceded it.
    """
    now = utc_now()
    version = new_id("version")
    return {
        "review_disposition": disposition,
        "variant_of_question_id": variant_of,
        "question_version": version,
        "changes": [
            *question.changes,
            ChangeRecord(
                change_id=new_id("change"),
                actor_type=ActorType.HUMAN,
                operation="review_disposition",
                summary=(
                    f"review disposition set to {disposition.value}"
                    + (f", variant of {variant_of}" if variant_of else "")
                ),
                created_at=now,
                parent_version=question.question_version,
                new_version=version,
            ),
        ],
    }


def apply_keep_independent(question: QuestionSpec) -> ReviewOutcome:
    """Keep the candidate as an independent question."""
    if question.review_disposition is ReviewDisposition.REJECTED:
        raise ReviewCommandError(
            "already_rejected",
            "a rejected candidate cannot be kept; run a new generation first",
        )
    updated = question.model_copy(
        update=_disposition_update(
            question, disposition=ReviewDisposition.KEEP_INDEPENDENT
        )
    )
    operation = ReviewOperation(
        operation_id=new_id("reviewop"),
        operation_version="v1",
        command=ReviewCommand.KEEP_INDEPENDENT,
        status=ReviewOperationStatus.COMMITTED,
        subject_question_id=question.question_id,
        expected_versions={question.question_id: question.question_version},
        review_group_key=question.review_group_key,
        written_versions={question.question_id: updated.question_version},
        completed_steps=["disposition"],
        summary="kept the candidate as an independent question",
        created_at=utc_now(),
        updated_at=utc_now(),
        committed_at=utc_now(),
    )
    return ReviewOutcome(
        operation=operation, questions=[updated], written_versions=operation.written_versions
    )


def apply_reject(question: QuestionSpec) -> ReviewOutcome:
    """Reject the candidate."""
    updated = question.model_copy(
        update=_disposition_update(
            question, disposition=ReviewDisposition.REJECTED
        )
    )
    operation = ReviewOperation(
        operation_id=new_id("reviewop"),
        operation_version="v1",
        command=ReviewCommand.REJECT,
        status=ReviewOperationStatus.COMMITTED,
        subject_question_id=question.question_id,
        expected_versions={question.question_id: question.question_version},
        review_group_key=question.review_group_key,
        written_versions={question.question_id: updated.question_version},
        completed_steps=["disposition"],
        summary="rejected the candidate",
        created_at=utc_now(),
        updated_at=utc_now(),
        committed_at=utc_now(),
    )
    return ReviewOutcome(
        operation=operation, questions=[updated], written_versions=operation.written_versions
    )


def apply_revise_or_return(
    question: QuestionSpec, *, command: ReviewCommand
) -> ReviewOutcome:
    """Send the candidate back for a new draft.

    The disposition resets to ``pending``: the new draft has to be reviewed
    again, and it may not inherit the old decision.
    """
    if command not in (ReviewCommand.REVISE, ReviewCommand.RETURN):
        raise ReviewCommandError(
            "bad_command", f"{command.value} is not a revise or a return"
        )
    if question.review_disposition is ReviewDisposition.REJECTED:
        raise ReviewCommandError(
            "already_rejected",
            "a rejected candidate cannot be revised",
        )
    updated = question.model_copy(
        update=_disposition_update(question, disposition=ReviewDisposition.PENDING)
    )
    operation = ReviewOperation(
        operation_id=new_id("reviewop"),
        operation_version="v1",
        command=command,
        status=ReviewOperationStatus.COMMITTED,
        subject_question_id=question.question_id,
        expected_versions={question.question_id: question.question_version},
        review_group_key=question.review_group_key,
        written_versions={question.question_id: updated.question_version},
        completed_steps=["disposition"],
        summary=(
            "sent the candidate back for a new draft"
            if command is ReviewCommand.REVISE
            else "returned the candidate to the review queue"
        ),
        created_at=utc_now(),
        updated_at=utc_now(),
        committed_at=utc_now(),
    )
    return ReviewOutcome(
        operation=operation, questions=[updated], written_versions=operation.written_versions
    )


def apply_keep_both(
    primary: QuestionSpec, secondary: QuestionSpec
) -> ReviewOutcome:
    """Keep two candidates, both as independent questions."""
    if primary.review_group_key != secondary.review_group_key:
        raise ReviewCommandError(
            "different_group",
            "keep_both only applies to two candidates in the same review group",
        )
    updated = [
        item.model_copy(
            update=_disposition_update(
                item, disposition=ReviewDisposition.KEEP_INDEPENDENT
            )
        )
        for item in (primary, secondary)
    ]
    operation = ReviewOperation(
        operation_id=new_id("reviewop"),
        operation_version="v1",
        command=ReviewCommand.KEEP_BOTH,
        status=ReviewOperationStatus.COMMITTED,
        subject_question_id=primary.question_id,
        target_question_id=secondary.question_id,
        expected_versions={
            primary.question_id: primary.question_version,
            secondary.question_id: secondary.question_version,
        },
        review_group_key=primary.review_group_key,
        written_versions={
            item.question_id: new.question_version
            for item, new in zip((primary, secondary), updated)
        },
        completed_steps=["primary_disposition", "secondary_disposition"],
        summary="kept both candidates as independent questions",
        created_at=utc_now(),
        updated_at=utc_now(),
        committed_at=utc_now(),
    )
    return ReviewOutcome(
        operation=operation, questions=updated, written_versions=operation.written_versions
    )


def apply_set_variant_of(
    candidate: QuestionSpec,
    primary: QuestionSpec,
    *,
    all_questions: Sequence[QuestionSpec] = (),
) -> ReviewOutcome:
    """Make the candidate a wording variant of the primary question.

    This is the composite operation. The rules it enforces:

    * The candidate and the primary must be in the same review group.
    * The primary must be a root candidate that is kept independently.
    * The candidate may not be the primary, may not already be a variant, and
      may not point at a variant of a variant.
    * The candidate inherits the primary's logical question id, split group and
      dataset split, so the two never land in different splits.
    """
    if candidate.question_id == primary.question_id:
        raise ReviewCommandError(
            "self_reference", "a candidate cannot be a variant of itself"
        )
    if candidate.review_group_key != primary.review_group_key:
        raise ReviewCommandError(
            "different_group",
            "a variant must belong to the same review group as its primary",
        )
    if primary.review_disposition is not ReviewDisposition.KEEP_INDEPENDENT:
        raise ReviewCommandError(
            "primary_not_kept",
            "the primary must be kept independently before it gains a variant",
        )
    # A cycle is checked first: it is the more fundamental defect, and a
    # primary that points at the candidate would otherwise be reported as
    # a variant of a variant.
    for item in all_questions:
        if item.question_id == primary.question_id and (
            item.variant_of_question_id == candidate.question_id
        ):
            raise ReviewCommandError(
                "cycle", "the variant relation would form a cycle"
            )
    if primary.variant_of_question_id:
        raise ReviewCommandError(
            "primary_is_variant",
            "a variant of a variant is not allowed; the primary must be a root "
            "question",
        )
    if candidate.review_disposition is ReviewDisposition.VARIANT_OF:
        raise ReviewCommandError(
            "already_variant",
            "the candidate is already a variant of another question",
        )
    if candidate.review_disposition is ReviewDisposition.REJECTED:
        raise ReviewCommandError(
            "already_rejected",
            "a rejected candidate cannot become a variant",
        )

    updated_candidate = candidate.model_copy(
        update={
            **_disposition_update(
                candidate,
                disposition=ReviewDisposition.VARIANT_OF,
                variant_of=primary.question_id,
            ),
            # The candidate follows the primary into its split, so the two
            # can never end up in different splits.
            "logical_question_id": primary.logical_question_id,
            "split_group_id": primary.split_group_id,
            "dataset_split": primary.dataset_split,
        }
    )
    # The primary keeps its identity; only its variant link changes. It gets
    # a new version so the composite write is a real new version, not a
    # rewrite of the existing one.
    primary_version = new_id("version")
    updated_primary = primary.model_copy(
        update={
            "review_disposition": ReviewDisposition.KEEP_INDEPENDENT,
            "question_version": primary_version,
            "changes": [
                *primary.changes,
                ChangeRecord(
                    change_id=new_id("change"),
                    actor_type=ActorType.HUMAN,
                    operation="review_variant_added",
                    summary=f"gained a variant: {candidate.question_id}",
                    created_at=utc_now(),
                    parent_version=primary.question_version,
                    new_version=primary_version,
                ),
            ],
        }
    )
    operation = ReviewOperation(
        operation_id=new_id("reviewop"),
        operation_version="v1",
        command=ReviewCommand.SET_VARIANT_OF,
        status=ReviewOperationStatus.COMMITTED,
        subject_question_id=candidate.question_id,
        target_question_id=primary.question_id,
        expected_versions={
            candidate.question_id: candidate.question_version,
            primary.question_id: primary.question_version,
        },
        review_group_key=candidate.review_group_key,
        written_versions={
            candidate.question_id: updated_candidate.question_version,
            primary.question_id: updated_primary.question_version,
        },
        completed_steps=["candidate_disposition", "primary_disposition"],
        summary=f"made the candidate a variant of {primary.question_id}",
        created_at=utc_now(),
        updated_at=utc_now(),
        committed_at=utc_now(),
    )
    return ReviewOutcome(
        operation=operation,
        questions=[updated_candidate, updated_primary],
        written_versions=operation.written_versions,
    )


def check_expected_versions(
    operation: ReviewOperation,
    current: dict[str, str],
) -> None:
    """The optimistic check. A version mismatch aborts the operation."""
    for question_id, expected in operation.expected_versions.items():
        actual = current.get(question_id)
        if actual != expected:
            raise ReviewCommandError(
                "version_conflict",
                f"question {question_id} is at version {actual!r}, the operation "
                f"expected {expected!r}",
            )