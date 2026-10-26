"""Validation dependency rules and staleness handling.

Editing a field invalidates the checks that read it. Rather than silently
keeping stale ``passed`` results, affected checks are reset to ``not_run`` so
the workbench never shows a green check for data that no longer exists.
"""
from __future__ import annotations

from dataclasses import dataclass

from .domain import ValidationCheck, ValidationLevel, ValidationStatus


@dataclass(frozen=True)
class CheckDependency:
    """Which question fields a check type reads."""

    check_type: str
    fields: frozenset[str]


#: Field -> check types that must re-run when that field changes.
FIELD_DEPENDENCIES: dict[str, set[str]] = {
    "canonical_question": {"question_naturalness", "question_leakage", "semantic_equivalence"},
    "question_variants": {"question_naturalness", "semantic_equivalence"},
    "gold_answer": {"answer_correctness", "question_leakage", "duplicate_detection"},
    "evidence_requirements": {"evidence_support", "evidence_sufficiency"},
    "query_intent": {"question_naturalness", "semantic_equivalence"},
    "expected_route": {"route_plausibility"},
    "taxonomy": {"taxonomy_consistency", "duplicate_detection"},
}

#: Check types that a human confirmation is allowed to satisfy directly.
HUMAN_OVERRIDABLE: frozenset[str] = frozenset(
    {
        "question_naturalness",
        "semantic_equivalence",
        "answer_correctness",
        "route_plausibility",
        "taxonomy_consistency",
        "evidence_support",
    }
)

#: Check types that must never be marked passed by a human click.
BLOCKING_AUTOMATED: frozenset[str] = frozenset(
    {"question_leakage", "duplicate_detection"}
)


def affected_check_types(changed_fields: set[str]) -> set[str]:
    """Return the check types invalidated by the given edited fields."""
    affected: set[str] = set()
    for field in changed_fields:
        affected.update(FIELD_DEPENDENCIES.get(field, set()))
    return affected


def reset_stale_checks(
    checks: list[ValidationCheck],
    changed_fields: set[str],
    *,
    preserve_human_confirmations: bool = True,
) -> list[ValidationCheck]:
    """Reset checks invalidated by an edit back to ``not_run``.

    A check stays as-is when its type is unaffected, or when it is a recorded
    human confirmation for a field the edit did not touch.
    """
    affected = affected_check_types(changed_fields)
    if not affected:
        return list(checks)
    if preserve_human_confirmations:
        affected -= HUMAN_OVERRIDABLE
    if not affected:
        return list(checks)

    reset: list[ValidationCheck] = []
    for check in checks:
        if (
            check.check_type in affected
            and check.status is not ValidationStatus.NOT_RUN
        ):
            reset.append(
                check.model_copy(
                    update={
                        "status": ValidationStatus.NOT_RUN,
                        "message": (
                            f"字段已修改（{', '.join(sorted(changed_fields))}），"
                            "该校验需要重新运行"
                        ),
                    }
                )
            )
        else:
            reset.append(check.model_copy())
    return reset


def unresolved_blocking_checks(
    checks: list[ValidationCheck],
) -> list[ValidationCheck]:
    """Return automated checks that block a formal export."""
    return [
        check
        for check in checks
        if check.check_type in BLOCKING_AUTOMATED
        and check.status in {ValidationStatus.FAILED, ValidationStatus.NOT_RUN}
    ]


def exportable_questions(
    questions: list["object"],
) -> tuple[list["object"], list["object"]]:
    """Split questions into exportable and blocked groups.

    Only ``human_confirmed`` questions with no unresolved blocking check can be
    exported. ``draft`` output from mock tools is never exportable.
    """
    ready: list[object] = []
    blocked: list[object] = []
    for question in questions:  # type: ignore[assignment]
        checks = getattr(question, "validation_checks", [])
        if (
            getattr(question, "validation_level", None) is ValidationLevel.HUMAN_CONFIRMED
            and not unresolved_blocking_checks(list(checks))
        ):
            ready.append(question)
        else:
            blocked.append(question)
    return ready, blocked
