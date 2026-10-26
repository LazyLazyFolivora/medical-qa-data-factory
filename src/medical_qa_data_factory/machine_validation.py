"""Machine validation state flow and the formal-export check gate.

The rule this module enforces: a missing check record is ``not_run``, never a
pass. A formal export requires a fixed set of blocking checks, each recorded
as ``passed`` by a named validator version, for the exact question version
being exported.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .domain import (
    ActorType,
    ChangeRecord,
    EvidenceBundle,
    QuestionSpec,
    ValidationCheck,
    ValidationLevel,
    ValidationStatus,
)
from .domain.common import new_id, utc_now
from .validation_applicability import (
    VALIDATOR_POLICY_VERSION,
    applicable_checks,
    question_dependency_digest,
    version_ancestry,
)
from .validators import (
    ADVISORY_CHECKS,
    BLOCKING_FOR_MACHINE_VALIDATION,
    VALIDATOR_VERSION,
    run_all_validators,
)


#: The fixed set of checks a formal export must see as ``passed``.
REQUIRED_FOR_FORMAL_EXPORT: tuple[str, ...] = BLOCKING_FOR_MACHINE_VALIDATION

#: Checks that a human review click may never satisfy.
AUTOMATED_ONLY: frozenset[str] = frozenset(REQUIRED_FOR_FORMAL_EXPORT)

MACHINE_VALIDATION_VERSION = "machine-validation.v1"


class MachineValidationError(RuntimeError):
    """Raised when a machine validation transition is not allowed."""


@dataclass(frozen=True)
class CheckRequirement:
    """One required check resolved against the recorded checks."""

    check_type: str
    status: ValidationStatus
    validator_version: str | None
    message: str


@dataclass(frozen=True)
class FormalExportAssessment:
    """Whether one question satisfies the formal export gate."""

    question_id: str
    question_version: str
    satisfied: bool
    requirements: list[CheckRequirement] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        return [
            item.check_type
            for item in self.requirements
            if item.validator_version is None
        ]

    @property
    def failed(self) -> list[str]:
        return [
            item.check_type
            for item in self.requirements
            if item.status is not ValidationStatus.PASSED
        ]

    def failure_reasons(self) -> list[str]:
        return [
            f"{item.check_type}（{item.message}）"
            for item in self.requirements
            if item.status is not ValidationStatus.PASSED
        ]


def check_requirements(
    checks: list[ValidationCheck],
    *,
    required: tuple[str, ...] = REQUIRED_FOR_FORMAL_EXPORT,
) -> list[CheckRequirement]:
    """Resolve each required check type against the recorded checks.

    This is the raw view: it does not judge applicability. Use
    :func:`applicable_requirements` for a gate.
    """
    by_type: dict[str, ValidationCheck] = {}
    for check in checks:
        if check.check_type in required:
            by_type[check.check_type] = check
    resolved: list[CheckRequirement] = []
    for check_type in required:
        check = by_type.get(check_type)
        if check is None:
            resolved.append(
                CheckRequirement(
                    check_type=check_type,
                    status=ValidationStatus.NOT_RUN,
                    validator_version=None,
                    message="没有该校验的执行记录",
                )
            )
            continue
        resolved.append(
            CheckRequirement(
                check_type=check_type,
                status=check.status,
                validator_version=check.validator_version,
                message=check.message,
            )
        )
    return resolved


def applicable_requirements(
    question: QuestionSpec,
    *,
    required: tuple[str, ...] = REQUIRED_FOR_FORMAL_EXPORT,
    allowed_validator_versions: frozenset[str] | None = None,
) -> list[CheckRequirement]:
    """Resolve required checks, honouring each check's applicability.

    A check that no longer applies is reported as ``not_run`` with the reason
    it was excluded, never as passed and never as a silent absence.
    """
    digest = question_dependency_digest(question)
    applies, excluded = applicable_checks(
        question.validation_checks,
        current_question_id=question.question_id,
        current_question_version=question.question_version,
        current_input_digest=digest,
        ancestor_versions=version_ancestry(question),
        allowed_validator_versions=allowed_validator_versions,
    )
    by_type = {check.check_type: check for check in applies}
    reasons = {check.check_type: verdict.reason for check, verdict in excluded}
    resolved: list[CheckRequirement] = []
    for check_type in required:
        check = by_type.get(check_type)
        if check is None:
            resolved.append(
                CheckRequirement(
                    check_type=check_type,
                    status=ValidationStatus.NOT_RUN,
                    validator_version=None,
                    message=reasons.get(check_type, "没有该校验的执行记录"),
                )
            )
            continue
        resolved.append(
            CheckRequirement(
                check_type=check_type,
                status=check.status,
                validator_version=check.validator_version,
                message=check.message,
            )
        )
    return resolved


def assess_formal_export(question: QuestionSpec) -> FormalExportAssessment:
    """Decide whether a question may enter a formal dataset."""
    requirements = applicable_requirements(question)
    human_recorded = [
        check.check_type
        for check in question.validation_checks
        if check.check_type in AUTOMATED_ONLY
        and check.validator_version.startswith("review.")
    ]
    failed = [
        item.check_type
        for item in requirements
        if item.status is not ValidationStatus.PASSED
    ] + [f"{item}(human_recorded)" for item in human_recorded]
    satisfied = (
        not failed
        and question.validation_level is ValidationLevel.HUMAN_CONFIRMED
    )
    return FormalExportAssessment(
        question_id=question.question_id,
        question_version=question.question_version,
        satisfied=satisfied,
        requirements=requirements,
    )


def machine_validation_blockers(
    question: QuestionSpec,
) -> list[CheckRequirement]:
    """Blocking requirements that are not ``passed``."""
    return [
        item
        for item in applicable_requirements(question)
        if item.status is not ValidationStatus.PASSED
    ]


def automated_checks_passed(question: QuestionSpec) -> bool:
    """True when every required check passed and no human click claimed it."""
    checks = list(question.validation_checks)
    for item in applicable_requirements(question):
        if item.status is not ValidationStatus.PASSED:
            return False
    return not human_recorded_blocking_checks(checks)


def human_recorded_blocking_checks(
    checks: list[ValidationCheck],
) -> list[str]:
    """Blocking check types whose only record came from a human review."""
    automated: dict[str, ValidationCheck] = {}
    human_only: dict[str, ValidationCheck] = {}
    for check in checks:
        if check.check_type not in AUTOMATED_ONLY:
            continue
        target = (
            human_only
            if check.validator_version.startswith("review.")
            else automated
        )
        target[check.check_type] = check
    return sorted(set(human_only) - set(automated))


def apply_machine_validation(
    question: QuestionSpec,
    bundles: list[EvidenceBundle],
    *,
    peers: list[QuestionSpec] | None = None,
    actor_type: ActorType = ActorType.AGENT,
    summary: str | None = None,
    validator_version: str = VALIDATOR_VERSION,
) -> QuestionSpec:
    """Run all validators and store a new immutable question version.

    ``validation_level`` only becomes ``machine_validated`` when every blocking
    check passed. An automated flow never writes ``human_confirmed``.
    """
    new_checks = run_all_validators(
        question,
        bundles,
        peers=peers or [],
        validator_version=validator_version,
    )
    blocking = [
        check
        for check in new_checks
        if check.check_type in BLOCKING_FOR_MACHINE_VALIDATION
    ]
    all_blocking_passed = bool(blocking) and all(
        check.status is ValidationStatus.PASSED for check in blocking
    )
    warnings = [
        check for check in new_checks if check.status is ValidationStatus.WARNING
    ]

    retained = [
        check.model_copy()
        for check in question.validation_checks
        if not check.check_type.startswith("human_")
    ]
    checks = [*retained, *new_checks]

    level = question.validation_level
    if level is ValidationLevel.DRAFT and all_blocking_passed:
        level = ValidationLevel.MACHINE_VALIDATED
    elif not all_blocking_passed and level is ValidationLevel.MACHINE_VALIDATED:
        level = ValidationLevel.DRAFT

    passed = sum(1 for check in blocking if check.status is ValidationStatus.PASSED)
    message = summary or (
        f"机器校验 {passed}/{len(blocking)} 项阻塞检查通过，"
        f"{len(warnings)} 项提示"
    )

    now = utc_now()
    version = new_id("version")
    payload = question.model_dump()
    payload["validation_checks"] = [check.model_dump() for check in checks]
    payload["validation_level"] = level
    payload["question_version"] = version
    payload["changes"] = [
        *question.changes,
        ChangeRecord(
            change_id=new_id("change"),
            actor_type=actor_type,
            operation="machine_validation",
            summary=message,
            created_at=now,
            parent_version=question.question_version,
            new_version=version,
        ),
    ]
    return QuestionSpec.model_validate(payload)


def machine_validated_export_level(question: QuestionSpec) -> str | None:
    """Manifest level for a question that passed machine validation only."""
    if question.validation_level is not ValidationLevel.MACHINE_VALIDATED:
        return None
    if machine_validation_blockers(question):
        return None
    return "machine_validated"


def advisory_status(question: QuestionSpec) -> dict[str, str]:
    """Status of the human-advisory checks, for the workbench."""
    by_type = {check.check_type: check for check in question.validation_checks}
    return {
        check_type: (
            by_type[check_type].status.value
            if check_type in by_type
            else ValidationStatus.NOT_RUN.value
        )
        for check_type in ADVISORY_CHECKS
    }
