"""Dataset export gate and manifest generation.

The gate is deliberately conservative: a question is only included in a
formal dataset when it is human confirmed *and* every check in the fixed
required set is recorded as ``passed`` by a named validator. A missing check
record counts as ``not_run``, never as a pass. Mock-tool output can never
reach a formal export.

Following the PRD principle that one bad sample must not block the others, a
formal export writes the qualifying subset and reports every excluded record
with its reason instead of refusing the whole batch.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .domain import (
    EndpointKind,
    EvidenceBundle,
    QuestionSpec,
    ReviewDisposition,
    ValidationLevel,
    ValidationStatus,
)
from .domain.common import SCHEMA_VERSION, new_id, utc_now
from .machine_validation import (
    REQUIRED_FOR_FORMAL_EXPORT,
    automated_checks_passed,
    check_requirements,
)
from .repository import LocalArtifactRepository
from .validation import exportable_questions, unresolved_blocking_checks


class ExportLevel(str, Enum):
    """The three export levels.

    The level is chosen explicitly by the caller. It is never inferred from
    the output directory or from the state of the records, because a level
    that can be guessed is a level that can be got wrong.
    """

    DEVELOPMENT = "development"
    MACHINE_VALIDATED = "machine_validated"
    HUMAN_CONFIRMED = "human_confirmed"


class ExportBlockedError(RuntimeError):
    """Raised when a formal export would include unsafe records."""


@dataclass(frozen=True)
class ExportBlocker:
    reason: str
    label: str
    question_ids: list[str] = field(default_factory=list)
    details: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ExportPlan:
    """Result of the pre-export gate check."""

    formal_ready: list[QuestionSpec]
    development_ready: list[QuestionSpec]
    blockers: list[ExportBlocker]
    mock_only: bool
    #: Every question the repository holds, whatever its state. The export
    #: levels select from this, so a level's predicate sees every candidate.
    all_questions: list[QuestionSpec] = field(default_factory=list)
    #: The bundles the questions refer to.
    bundles: list[EvidenceBundle] = field(default_factory=list)

    @property
    def is_formal_exportable(self) -> bool:
        """True when at least one question cleared every formal gate.

        Mock-backed workspaces still fail even when questions were confirmed,
        because a formal dataset may not carry mock evidence.
        """
        if self.mock_only:
            return False
        return bool(self.formal_ready)

    @property
    def excluded(self) -> list[tuple[str, str]]:
        """Human-readable reason for every question left out of a formal set."""
        reasons: list[tuple[str, str]] = []
        for blocker in self.blockers:
            for question_id in blocker.question_ids:
                reasons.append((question_id, blocker.label))
        return reasons


def uses_mock_tools(bundles: list[EvidenceBundle]) -> bool:
    return any(
        snapshot.endpoint_kind is EndpointKind.MOCK
        for bundle in bundles
        for snapshot in bundle.tool_snapshots
    )


def question_uses_mock_tools(
    question: QuestionSpec, bundles_by_id: dict[str, EvidenceBundle]
) -> bool:
    """Whether the evidence behind one question came from a mock tool.

    The check is per question, over the bundles it actually cites. A mock
    bundle that no exported question refers to does not contaminate the
    export.
    """
    for bundle_id in question.bundle_ids:
        bundle = bundles_by_id.get(bundle_id)
        if bundle is None:
            continue
        if any(
            snapshot.endpoint_kind is EndpointKind.MOCK
            for snapshot in bundle.tool_snapshots
        ):
            return True
    return False


def build_export_plan(repository: LocalArtifactRepository) -> ExportPlan:
    """Decide which questions may be exported and why the rest are blocked."""
    bundles = repository.list_latest_bundles()
    questions = repository.list_latest_questions()
    bundles_by_id = {bundle.bundle_id: bundle for bundle in bundles}
    mock_only = bool(bundles) and uses_mock_tools(bundles)

    ready: list[QuestionSpec] = []
    blocked: list[QuestionSpec] = []
    check_details: dict[str, list[str]] = {}
    for question in questions:
        unresolved = check_requirements(list(question.validation_checks))
        failing = [
            item.check_type
            for item in unresolved
            if item.status is not ValidationStatus.PASSED
        ]
        stale = unresolved_blocking_checks(list(question.validation_checks))
        confirmed = question.validation_level is ValidationLevel.HUMAN_CONFIRMED
        machine_ok = automated_checks_passed(question)
        if failing or stale:
            check_details[question.question_id] = failing or [
                item.check_type for item in stale
            ]
        if confirmed and not failing and not stale and machine_ok:
            ready.append(question)
        else:
            blocked.append(question)
    # The mock check is per question: only the bundles a question actually
    # cites matter. A mock bundle that nothing exported refers to does not
    # contaminate the export.
    mock_questions = [
        question
        for question in questions
        if question_uses_mock_tools(question, bundles_by_id)
    ]
    blockers: list[ExportBlocker] = []

    draft_ids = [
        question.question_id
        for question in blocked
        if question.validation_level is ValidationLevel.DRAFT
    ]
    if draft_ids:
        blockers.append(
            ExportBlocker(
                reason="draft",
                label="未人工确认",
                question_ids=draft_ids,
            )
        )

    machine_ids = [
        question.question_id
        for question in blocked
        if question.validation_level is ValidationLevel.MACHINE_VALIDATED
        and automated_checks_passed(question)
    ]
    if machine_ids:
        blockers.append(
            ExportBlocker(
                reason="machine_validated",
                label="仅通过机器校验，尚未人工确认",
                question_ids=machine_ids,
            )
        )

    rejected_ids = [
        question.question_id
        for question in blocked
        if question.validation_level is ValidationLevel.REJECTED
    ]
    if rejected_ids:
        blockers.append(
            ExportBlocker(
                reason="rejected",
                label="已拒绝",
                question_ids=rejected_ids,
            )
        )

    check_blocked: list[str] = []
    not_run_blocked: list[str] = []
    failed_blocked: list[str] = []
    source_blocked: list[str] = []
    for question in blocked:
        unresolved = unresolved_blocking_checks(list(question.validation_checks))
        if unresolved:
            check_blocked.append(question.question_id)
        missing = check_details.get(question.question_id, [])
        if missing:
            if any(
                item.status is ValidationStatus.NOT_RUN
                for item in check_requirements(list(question.validation_checks))
                if item.check_type in missing
            ):
                not_run_blocked.append(question.question_id)
            else:
                failed_blocked.append(question.question_id)
        missing_source = _missing_source(question, bundles_by_id)
        if missing_source:
            source_blocked.append(question.question_id)
    if check_blocked:
        blockers.append(
            ExportBlocker(
                reason="required_checks_unresolved",
                label="阻塞校验缺失或失败",
                question_ids=check_blocked,
                details=check_details,
            )
        )
    if not_run_blocked:
        blockers.append(
            ExportBlocker(
                reason="required_checks_not_run",
                label="正式导出所需校验项未运行",
                question_ids=not_run_blocked,
                details=check_details,
            )
        )
    if failed_blocked:
        blockers.append(
            ExportBlocker(
                reason="required_checks_failed",
                label="正式导出所需校验项失败",
                question_ids=failed_blocked,
                details=check_details,
            )
        )
    if source_blocked:
        blockers.append(
            ExportBlocker(
                reason="missing_source",
                label="缺少来源",
                question_ids=source_blocked,
            )
        )

    if mock_questions:
        blockers.append(
            ExportBlocker(
                reason="mock_tools",
                label="包含 mock 工具产物",
                question_ids=[
                    question.question_id for question in mock_questions
                ],
            )
        )

    return ExportPlan(
        formal_ready=ready,
        development_ready=questions,
        blockers=blockers,
        mock_only=mock_only,
        all_questions=questions,
        bundles=bundles,
    )


def _missing_source(
    question: QuestionSpec, bundles_by_id: dict[str, EvidenceBundle]
) -> bool:
    """True when a confirmed question has no citable evidence behind it."""
    requirements = question.evidence_requirements
    if not requirements.required_kg_evidence_ids and not requirements.required_literature_evidence_ids:
        return True
    for bundle_id in question.bundle_ids:
        bundle = bundles_by_id.get(bundle_id)
        if bundle is None:
            return True
        known = {item.evidence_id for item in bundle.kg_evidence}
        known.update(item.evidence_id for item in bundle.literature_evidence)
        if not known & set(
            requirements.required_kg_evidence_ids
            + requirements.required_literature_evidence_ids
        ):
            return True
    return False


def _disposition_counts(questions: list[QuestionSpec]) -> dict[str, int]:
    """How many questions sit in each review disposition."""
    counts: dict[str, int] = {}
    for question in questions:
        key = question.review_disposition.value
        counts[key] = counts.get(key, 0) + 1
    return counts


def _pending_group_count(questions: list[QuestionSpec]) -> int:
    """The number of groups that still have an undecided candidate."""
    groups: dict[str, list[QuestionSpec]] = {}
    for question in questions:
        if question.review_group_key:
            groups.setdefault(question.review_group_key, []).append(question)
    return sum(
        1
        for members in groups.values()
        if any(
            member.review_disposition is ReviewDisposition.PENDING
            for member in members
        )
    )


def _content_hash(payload: object) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def select_for_level(
    plan: ExportPlan, level: ExportLevel
) -> tuple[list[QuestionSpec], list[tuple[str, str]]]:
    """The questions a level includes, and the reasons the rest are out.

    Each level has its own predicate. A level is never inferred from the
    records, and the levels are not nested: a machine-validated export does
    not silently include human-confirmed questions with their own rules.
    """
    if level is ExportLevel.DEVELOPMENT:
        # A development snapshot may contain anything, including mock output
        # and candidates nobody has looked at yet.
        return list(plan.development_ready), []
    included: list[QuestionSpec] = []
    excluded: list[tuple[str, str]] = []
    for question in plan.all_questions:
        reason = _machine_level_reason(question, plan)
        if reason is None:
            included.append(question)
        else:
            excluded.append((question.question_id, reason))
    if level is ExportLevel.HUMAN_CONFIRMED:
        confirmed: list[QuestionSpec] = []
        for question in included:
            reason = _human_level_reason(question)
            if reason is None:
                confirmed.append(question)
            else:
                excluded.append((question.question_id, reason))
        included = confirmed
    return included, excluded


def _machine_level_reason(
    question: QuestionSpec, plan: ExportPlan
) -> str | None:
    """Why a question is out of the machine-validated level."""
    bundles_by_id = {bundle.bundle_id: bundle for bundle in plan.bundles}
    if question_uses_mock_tools(question, bundles_by_id):
        return "包含 mock 工具产物"
    if question.validation_level is ValidationLevel.REJECTED:
        return "已拒绝"
    if question.validation_level is ValidationLevel.DRAFT:
        return "未通过机器校验"
    # A human-confirmed question has already passed the blocking checks:
    # the confirm action refuses to run while any of them is unresolved.
    if question.review_disposition is ReviewDisposition.REJECTED:
        return "已拒绝"
    if not automated_checks_passed(question):
        return "阻塞校验未通过"
    if question.review_disposition is ReviewDisposition.VARIANT_OF:
        # A variant is exported with its primary, never on its own.
        return "表达变体随主问题导出"
    return None


def _human_level_reason(question: QuestionSpec) -> str | None:
    """Why a question is out of the human-confirmed level."""
    if question.validation_level is not ValidationLevel.HUMAN_CONFIRMED:
        return "未人工确认"
    if question.review_disposition is ReviewDisposition.REJECTED:
        return "已拒绝"
    if question.review_disposition is not ReviewDisposition.KEEP_INDEPENDENT:
        return "组内处置未完成"
    if question.variant_of_question_id:
        return "表达变体随主问题导出"
    return None


def write_dataset_export(
    repository: LocalArtifactRepository,
    destination: str | Path,
    *,
    development: bool = False,
    level: ExportLevel | None = None,
) -> Path:
    """Write a versioned dataset export and return the directory path.

    The level must be chosen explicitly. ``development=True`` is kept for the
    existing callers and means the development level.
    """
    if level is None:
        level = ExportLevel.DEVELOPMENT if development else ExportLevel.HUMAN_CONFIRMED
    plan = build_export_plan(repository)
    questions, excluded = select_for_level(plan, level)
    if level is not ExportLevel.DEVELOPMENT and not questions:
        raise ExportBlockedError(
            f"{level.value} export blocked: no qualifying questions"
        )
    if not questions:
        raise ExportBlockedError("nothing to export")

    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    version = new_id("export")
    target = root / version
    target.mkdir()

    bundles = repository.list_latest_bundles()
    used_bundle_ids = {
        bundle_id for question in questions for bundle_id in question.bundle_ids
    }
    used_bundles = [
        bundle for bundle in bundles if bundle.bundle_id in used_bundle_ids
    ]

    question_lines = [question.model_dump_json() for question in questions]
    bundle_lines = [bundle.model_dump_json() for bundle in used_bundles]
    (target / "questions.jsonl").write_text(
        "\n".join(question_lines) + "\n", encoding="utf-8"
    )
    (target / "bundles.jsonl").write_text(
        "\n".join(bundle_lines) + "\n", encoding="utf-8"
    )

    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "export_id": version,
        "created_at": utc_now().isoformat(),
        "mode": level.value,
        "export_level": level.value,
        "question_count": len(questions),
        "bundle_count": len(used_bundles),
        "contains_mock_tools": plan.mock_only,
        "validation_levels": {
            level.value: sum(
                1
                for question in questions
                if question.validation_level is level
            )
            for level in ValidationLevel
        },
        "required_checks": list(REQUIRED_FOR_FORMAL_EXPORT),
        "per_question_checks": {
            question.question_id: [
                {
                    "check_type": item.check_type,
                    "status": item.status.value,
                    "validator_version": item.validator_version,
                    "message": item.message,
                }
                for item in check_requirements(list(question.validation_checks))
            ]
            for question in questions
        },
        "tool_snapshots": [
            snapshot.model_dump(mode="json")
            for bundle in used_bundles
            for snapshot in bundle.tool_snapshots
        ],
        "question_version_hashes": {
            question.question_id: question.question_version for question in questions
        },
        "content_hash": _content_hash([question_lines, bundle_lines]),
        "known_limitations": [
            "v0 price model is not calibrated for formal comparison experiments"
        ],
    }
    if level is ExportLevel.DEVELOPMENT:
        manifest["warning"] = (
            "DEVELOPMENT SNAPSHOT - mock or unconfirmed data, not for gold release"
        )
        manifest["included_question_ids"] = [q.question_id for q in questions]
        manifest["review_dispositions"] = _disposition_counts(questions)
    else:
        manifest["excluded"] = [
            {"question_id": question_id, "reason": label}
            for question_id, label in excluded
        ]
        manifest["export_policy"] = (
            "qualifying subset only; a failing sample never blocks the rest"
        )
        manifest["review_dispositions"] = _disposition_counts(questions)
        manifest["pending_review_groups"] = _pending_group_count(questions)
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target
