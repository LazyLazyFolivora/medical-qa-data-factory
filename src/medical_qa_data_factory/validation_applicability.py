"""Whether a recorded validation result still applies to a question.

A gate that reads "the last ``passed`` check of this type" is wrong: the
question may have changed since that check ran, and the check may have been
produced by a validator version whose semantics have since moved.

Two rules make the result reproducible:

1. A check may be reused only when the version it evaluated is the current
   version, or an ancestor of it that the validator policy allows.
2. A check may be reused only when the canonical digest of the input fields it
   depended on still matches the current question.

Both are evaluated from real stored provenance. Nothing here re-runs a
validator, and nothing here can turn a failed check into a passed one.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from .domain.common import ValidationCheck, ValidationStatus
from .domain.questions import QuestionSpec


#: Validator-policy version. Bumping it invalidates checks produced under a
#: different policy, which is how a semantic change to a check type stops
#: being read as if it were the new semantics.
VALIDATOR_POLICY_VERSION = "validators.v1"


#: The question fields a validation result depends on.
#:
#: Only these are hashed, so editing a review note or a ranking score does
#: not invalidate every check on the question, while editing the question
#: text, the answer or the required evidence does.
#:
#: ``verified_route`` is deliberately absent. It is the outcome of source
#: ablation, not an input to evidence correctness: a human who confirms the
#: route of an already-validated question must not invalidate that
#: question's evidence checks.
DEPENDENT_QUESTION_FIELDS: tuple[str, ...] = (
    "canonical_question",
    "question_variants",
    "taxonomy",
    "query_intent",
    "answerability",
    "gold_answer",
    "evidence_requirements",
    "expected_route",
    "split_group_id",
)


@dataclass(frozen=True)
class Applicability:
    """Why a check may or may not satisfy a gate."""

    applicable: bool
    reason: str


def dependency_digest(payload: Any) -> str:
    """Canonical digest of the fields a check depends on."""
    text = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def question_dependency_digest(question: QuestionSpec) -> str:
    """Digest of the question fields that a validation result depends on."""
    payload = {
        name: getattr(question, name, None)
        for name in DEPENDENT_QUESTION_FIELDS
    }
    return dependency_digest(payload)


def version_ancestry(question: QuestionSpec) -> set[str]:
    """Every version this question passed through, including itself.

    A check records the version it actually evaluated. Whether that version is
    an ancestor of the current one is decided from the immutable change
    chain, not from guessing at the version string: the version ids are
    random, so only the recorded parent links are trustworthy.
    """
    versions: set[str] = {question.question_version}
    # The change list is append-only and ordered, so the parent of the
    # current version is the newest change whose ``new_version`` is the
    # current version. Walking backwards from there reaches every ancestor.
    by_new_version = {
        change.new_version: change.parent_version
        for change in question.changes
        if change.new_version
    }
    current = question.question_version
    while current in by_new_version:
        parent = by_new_version[current]
        if not parent or parent in versions:
            break
        versions.add(parent)
        current = parent
    return versions


def check_applicability(
    check: ValidationCheck,
    *,
    current_question_id: str,
    current_question_version: str,
    current_input_digest: str,
    ancestor_versions: Iterable[str] | None = None,
    allowed_validator_versions: Iterable[str] | None = None,
) -> Applicability:
    """Decide whether one stored check still applies to the current question."""
    if check.question_id is not None and check.question_id != current_question_id:
        return Applicability(
            False, f"check belongs to question {check.question_id}"
        )
    if check.evaluated_question_version is None:
        # A check with no recorded version is a legacy record. It may never
        # satisfy a gate: the version it evaluated is simply unknown.
        return Applicability(
            False, "check carries no evaluated question version (legacy record)"
        )
    if check.evaluated_question_version != current_question_version:
        # An ancestor version is acceptable only when the input digest still
        # matches, which is checked below.
        allowed_ancestors = set(ancestor_versions or ())
        if check.evaluated_question_version not in allowed_ancestors:
            return Applicability(
                False,
                "check evaluated version "
                f"{check.evaluated_question_version} is not the current version "
                f"{current_question_version} nor an ancestor of it",
            )
    if allowed_validator_versions is not None:
        if check.validator_version not in set(allowed_validator_versions):
            return Applicability(
                False,
                f"validator {check.validator_version} is not allowed by the "
                "current validator policy",
            )
    if check.input_digest is None:
        return Applicability(False, "check carries no input digest")
    if check.input_digest != current_input_digest:
        return Applicability(
            False,
            "the inputs this check depended on have changed "
            f"(recorded {check.input_digest[:12]}, current {current_input_digest[:12]})",
        )
    if check.status is ValidationStatus.NOT_RUN:
        return Applicability(False, "check was not run")
    if check.status is ValidationStatus.FAILED:
        return Applicability(False, "check failed")
    return Applicability(True, "check applies to the current question version")


def applicable_checks(
    checks: Iterable[ValidationCheck],
    *,
    current_question_id: str,
    current_question_version: str,
    current_input_digest: str,
    ancestor_versions: Iterable[str] | None = None,
    allowed_validator_versions: Iterable[str] | None = None,
) -> tuple[list[ValidationCheck], list[tuple[ValidationCheck, Applicability]]]:
    """Split checks into the ones that apply and the ones that do not.

    The second list keeps the reason, so a gate can explain itself instead of
    silently ignoring a stale result.
    """
    applies: list[ValidationCheck] = []
    excluded: list[tuple[ValidationCheck, Applicability]] = []
    for check in checks:
        verdict = check_applicability(
            check,
            current_question_id=current_question_id,
            current_question_version=current_question_version,
            current_input_digest=current_input_digest,
            ancestor_versions=ancestor_versions,
            allowed_validator_versions=allowed_validator_versions,
        )
        if verdict.applicable:
            applies.append(check)
        else:
            excluded.append((check, verdict))
    return applies, excluded
