"""Old records must stay readable, and must not silently satisfy a gate.

Step 1 of the real-generation-agent plan defines the read/reject policy for
legacy JSON, checkpoints and check records:

* A legacy record is still readable. Reading it must not crash.
* A legacy check carries no provenance, so it can never satisfy a gate. It is
  reported as ``not_run`` with a reason, not as a pass and not as a silent
  absence.
"""
from __future__ import annotations

import json
from pathlib import Path

from medical_qa_data_factory.domain.common import (
    ValidationCheck,
    ValidationLevel,
    ValidationStatus,
)
from medical_qa_data_factory.machine_validation import (
    applicable_requirements,
    assess_formal_export,
    check_requirements,
)
from medical_qa_data_factory.validation_applicability import (
    check_applicability,
    question_dependency_digest,
)


LEGACY_CHECK = {
    "check_id": "check_legacy",
    "check_type": "schema_references",
    "status": "passed",
    "severity": "info",
    "message": "ok",
    "validator_version": "validators.v1",
}


def test_a_legacy_check_record_is_still_readable() -> None:
    check = ValidationCheck.model_validate(LEGACY_CHECK)
    assert check.check_id == "check_legacy"
    assert check.status is ValidationStatus.PASSED
    assert check.question_id is None
    assert check.evaluated_question_version is None
    assert check.input_digest is None


def test_a_legacy_check_never_satisfies_a_gate() -> None:
    check = ValidationCheck.model_validate(LEGACY_CHECK)
    verdict = check_applicability(
        check,
        current_question_id="q1",
        current_question_version="v1",
        current_input_digest="digest",
    )
    assert not verdict.applicable
    assert "legacy" in verdict.reason


def test_a_legacy_check_is_reported_as_not_run_not_passed() -> None:
    """The raw view still sees it; the applicability view must not."""
    check = ValidationCheck.model_validate(LEGACY_CHECK)
    raw = check_requirements([check])
    assert raw[0].status is ValidationStatus.PASSED
    # A question carrying only legacy checks has no applicable requirement.
    question = _question_with_legacy_check(check)
    requirements = applicable_requirements(question)
    assert requirements[0].status is ValidationStatus.NOT_RUN
    assert "legacy" in requirements[0].message


def test_a_question_with_only_legacy_checks_cannot_be_exported() -> None:
    check = ValidationCheck.model_validate(LEGACY_CHECK)
    question = _question_with_legacy_check(check)
    assessment = assess_formal_export(question)
    assert assessment.satisfied is False
    assert assessment.missing


def _question_with_legacy_check(check: ValidationCheck):
    from medical_qa_data_factory.domain.questions import QuestionSpec
    from medical_qa_data_factory.domain.common import utc_now

    now = utc_now()
    return QuestionSpec(
        question_id="q1",
        logical_question_id="l1",
        question_version="v1",
        bundle_ids=["b1"],
        generation_mode="automatic",
        canonical_question="q?",
        question_variants=[
            {
                "variant_id": "v1",
                "text": "q?",
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
        validation_level=ValidationLevel.HUMAN_CONFIRMED,
        validation_checks=[check],
        created_at=now,
    )


def test_a_legacy_question_json_on_disk_is_still_loadable(tmp_path: Path) -> None:
    """An old artifact file must not break the repository reader."""
    from medical_qa_data_factory.repository import LocalArtifactRepository

    repository = LocalArtifactRepository(tmp_path)
    payload = {
        "schema_version": "0.1.0",
        "check_id": "check_legacy",
        "check_type": "schema_references",
        "status": "passed",
        "severity": "info",
        "message": "ok",
        "validator_version": "validators.v1",
    }
    directory = tmp_path / "validation_checks" / "check_legacy"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "check_legacy.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    loaded = repository.load(
        "validation_checks", "check_legacy", "check_legacy", ValidationCheck
    )
    assert loaded.check_id == "check_legacy"
    assert loaded.input_digest is None
