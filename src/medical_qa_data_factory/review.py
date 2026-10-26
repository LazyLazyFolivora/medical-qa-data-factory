"""Human review transitions for questions and claims."""
from __future__ import annotations

from medical_qa_data_factory.domain import (
    ActorType,
    ChangeRecord,
    ClaimStatus,
    DatasetSplit,
    EvidenceBundle,
    EvidenceRequirements,
    QuestionSpec,
    QuestionTaxonomy,
    QuestionVariant,
    ReviewDisposition,
    Route,
    ValidationCheck,
    ValidationLevel,
    ValidationSeverity,
    ValidationStatus,
)
from medical_qa_data_factory.domain.common import new_id, utc_now
from medical_qa_data_factory.machine_validation import REQUIRED_FOR_FORMAL_EXPORT
from medical_qa_data_factory.validation import (
    BLOCKING_AUTOMATED,
    reset_stale_checks,
)


REVIEWER_VERSION = "review.v1"


class InvalidReviewAction(ValueError):
    """Raised when a review action cannot be applied to the current record."""


QUESTION_PROTECTED_FIELDS = {
    "question_id",
    "logical_question_id",
    "question_version",
    "created_at",
    "changes",
}
BUNDLE_PROTECTED_FIELDS = {
    "bundle_id",
    "bundle_version",
    "task_id",
    "created_at",
    "changes",
}

#: Fields a reviewer may edit; each edit produces a new immutable version.
EDITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "canonical_question",
        "question_variants",
        "gold_answer",
        "evidence_requirements",
        "query_intent",
        "taxonomy",
        "expected_route",
    }
)


def _revision(
    model: QuestionSpec | EvidenceBundle,
    protected: set[str],
    *,
    actor_type: ActorType,
    operation: str,
    summary: str,
    version_field: str,
    updates: dict[str, object],
) -> dict[str, object]:
    """Build the payload of the next immutable version with an audit record."""
    invalid = protected.intersection(updates)
    if invalid:
        raise ValueError(f"protected fields cannot be updated: {sorted(invalid)}")

    now = utc_now()
    new_version = new_id("version")
    payload = model.model_dump()
    payload.update(updates)
    payload[version_field] = new_version
    payload.setdefault("changes", [])
    payload["changes"] = [
        *model.changes,
        ChangeRecord(
            change_id=new_id("change"),
            actor_type=actor_type,
            operation=operation,
            summary=summary,
            created_at=now,
            parent_version=getattr(model, version_field),
            new_version=new_version,
        ),
    ]
    return payload


def _review_check(check_type: str, summary: str, status: ValidationStatus, severity: ValidationSeverity, artifact_ids: list[str]) -> ValidationCheck:
    return ValidationCheck(
        check_id=new_id("check"),
        check_type=check_type,
        status=status,
        severity=severity,
        message=summary,
        validator_version=REVIEWER_VERSION,
        artifact_ids=artifact_ids,
    )


def _field_updates(
    question: QuestionSpec,
    edited: set[str],
    *,
    canonical_question: str | None = None,
    question_variants: list[dict[str, object]] | None = None,
    gold_answer: dict[str, object] | None = None,
    evidence_requirements: dict[str, object] | None = None,
    taxonomy: dict[str, object] | None = None,
    query_intent: dict[str, object] | None = None,
    gold_short_answer: str | None = None,
) -> dict[str, object]:
    """Apply only the fields the reviewer actually changed.

    An edit that produces an identical value is not recorded as a change, so
    ``changed_fields`` stays a truthful diff and dependent validators are not
    needlessly invalidated.
    """
    updates: dict[str, object] = {}
    if canonical_question is not None and canonical_question.strip():
        text = canonical_question.strip()
        if text != question.canonical_question:
            updates["canonical_question"] = text
    if question_variants is not None:
        variants = [QuestionVariant.model_validate(item) for item in question_variants]
        if [item.model_dump() for item in variants] != [
            item.model_dump() for item in question.question_variants
        ]:
            updates["question_variants"] = variants
    gold_updates: dict[str, object] = {}
    if gold_answer:
        gold_updates.update(gold_answer)
    if gold_short_answer and gold_short_answer.strip():
        if gold_short_answer.strip() != question.gold_answer.short_answer:
            gold_updates["short_answer"] = gold_short_answer.strip()
    if gold_updates:
        merged = question.gold_answer.model_copy(update=gold_updates)
        if merged != question.gold_answer:
            updates["gold_answer"] = merged
    if evidence_requirements:
        merged = EvidenceRequirements.model_validate(
            {**question.evidence_requirements.model_dump(), **evidence_requirements}
        )
        if merged != question.evidence_requirements:
            updates["evidence_requirements"] = merged
    if taxonomy:
        merged = QuestionTaxonomy.model_validate(
            {**question.taxonomy.model_dump(), **taxonomy}
        )
        if merged != question.taxonomy:
            updates["taxonomy"] = merged
    if query_intent:
        merged = {**question.query_intent, **query_intent}
        if merged != question.query_intent:
            updates["query_intent"] = merged
    known = set(updates)
    unknown = edited - known - EDITABLE_FIELDS
    if unknown:
        raise InvalidReviewAction(
            "changed_fields 包含未知字段: " + ", ".join(sorted(unknown))
        )
    return updates


def review_question(
    question: QuestionSpec,
    action: str,
    *,
    summary: str,
    actor_type: ActorType = ActorType.HUMAN,
    changed_fields: set[str] | None = None,
    verified_route: Route | None = None,
    gold_short_answer: str | None = None,
    dataset_split: DatasetSplit | None = None,
    canonical_question: str | None = None,
    question_variants: list[dict[str, object]] | None = None,
    gold_answer: dict[str, object] | None = None,
    evidence_requirements: dict[str, object] | None = None,
    taxonomy: dict[str, object] | None = None,
    query_intent: dict[str, object] | None = None,
) -> QuestionSpec:
    """Confirm, return or reject a question draft as a new immutable version."""
    if question.validation_level is ValidationLevel.REJECTED and action != "return":
        raise InvalidReviewAction("a rejected question can only be returned")

    updates: dict[str, object] = {}
    edited = changed_fields or set()
    updates.update(
        _field_updates(
            question,
            edited,
            canonical_question=canonical_question,
            question_variants=question_variants,
            gold_answer=gold_answer,
            evidence_requirements=evidence_requirements,
            taxonomy=taxonomy,
            query_intent=query_intent,
            gold_short_answer=gold_short_answer,
        )
    )
    checks = reset_stale_checks(list(question.validation_checks), edited)

    if action == "confirm":
        recorded = {check.check_type: check for check in checks}
        blocking = [
            check_type
            for check_type in REQUIRED_FOR_FORMAL_EXPORT
            if check_type not in recorded
            or recorded[check_type].status is not ValidationStatus.PASSED
        ]
        if blocking:
            raise InvalidReviewAction(
                "正式导出所需校验必须先通过: " + ", ".join(sorted(blocking))
            )
        updates["validation_level"] = ValidationLevel.HUMAN_CONFIRMED
        # Confirming a question is a decision to keep it as an independent
        # question. Without this the human-confirmed export level could
        # never be reached, because that level requires a resolved review
        # disposition.
        if question.review_disposition is ReviewDisposition.PENDING:
            updates["review_disposition"] = ReviewDisposition.KEEP_INDEPENDENT
            updates["variant_of_question_id"] = None
        checks.append(
            _review_check(
                "human_confirmation",
                summary,
                ValidationStatus.PASSED,
                ValidationSeverity.INFO,
                question.evidence_requirements.required_claim_ids,
            )
        )
        route = verified_route or question.verified_route
        if route is Route.UNKNOWN:
            route = question.expected_route
        updates["verified_route"] = route
        if dataset_split:
            updates["dataset_split"] = dataset_split
    elif action == "return":
        updates["validation_level"] = ValidationLevel.DRAFT
        checks.append(
            _review_check(
                "human_return",
                summary,
                ValidationStatus.WARNING,
                ValidationSeverity.WARNING,
                [question.question_id],
            )
        )
    elif action == "reject":
        updates["validation_level"] = ValidationLevel.REJECTED
        checks.append(
            _review_check(
                "human_rejection",
                summary,
                ValidationStatus.FAILED,
                ValidationSeverity.BLOCKING,
                [question.question_id],
            )
        )
    else:
        raise InvalidReviewAction(f"unknown review action: {action!r}")

    updates["validation_checks"] = checks
    payload = _revision(
        question,
        QUESTION_PROTECTED_FIELDS,
        actor_type=actor_type,
        operation=f"human_{action}",
        summary=summary,
        version_field="question_version",
        updates=updates,
    )
    return QuestionSpec.model_validate(payload)


def review_claim(
    bundle: EvidenceBundle,
    claim_id: str,
    status: ClaimStatus,
    *,
    summary: str,
    actor_type: ActorType = ActorType.HUMAN,
) -> EvidenceBundle:
    """Record a human claim verdict as a new immutable bundle version."""
    claims = [claim.model_copy() for claim in bundle.claims]
    for claim in claims:
        if claim.claim_id == claim_id:
            claim.status = status
            claim.rationale = summary
            break
    else:
        raise InvalidReviewAction(f"unknown claim: {claim_id!r}")

    checks = [
        *bundle.validation_checks,
        _review_check(
            "human_claim_review",
            summary,
            ValidationStatus.PASSED
            if status is ClaimStatus.SUPPORTED
            else ValidationStatus.WARNING,
            ValidationSeverity.INFO,
            [claim_id],
        ),
    ]
    payload = _revision(
        bundle,
        BUNDLE_PROTECTED_FIELDS,
        actor_type=actor_type,
        operation="human_claim_review",
        summary=summary,
        version_field="bundle_version",
        updates={"claims": claims, "validation_checks": checks},
    )
    return EvidenceBundle.model_validate(payload)
