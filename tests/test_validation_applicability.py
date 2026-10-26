"""A stale validation result must not satisfy a gate.

Step 1 of the real-generation-agent plan: a check may only be reused when it
evaluated the current question version (or an ancestor of it) and the inputs it
depended on are unchanged.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

from medical_qa_data_factory.domain.common import (
    ChangeRecord,
    ValidationCheck,
    ValidationStatus,
    new_id,
    utc_now,
)
from medical_qa_data_factory.validation_applicability import (
    applicable_checks,
    check_applicability,
    dependency_digest,
    question_dependency_digest,
    version_ancestry,
)


def _check(**overrides) -> ValidationCheck:
    payload = {
        "check_id": "check_1",
        "check_type": "schema_references",
        "status": ValidationStatus.PASSED,
        "severity": "info",
        "message": "ok",
        "validator_version": "validators.v1",
        "question_id": "q1",
        "evaluated_question_version": "v2",
        "input_digest": "digest-a",
    }
    payload.update(overrides)
    return ValidationCheck(**payload)


def test_a_check_for_the_current_version_applies() -> None:
    verdict = check_applicability(
        _check(),
        current_question_id="q1",
        current_question_version="v2",
        current_input_digest="digest-a",
    )
    assert verdict.applicable
    assert "applies" in verdict.reason


def test_a_check_for_another_question_does_not_apply() -> None:
    verdict = check_applicability(
        _check(question_id="q2"),
        current_question_id="q1",
        current_question_version="v2",
        current_input_digest="digest-a",
    )
    assert not verdict.applicable
    assert "q2" in verdict.reason


def test_a_legacy_check_without_a_version_never_applies() -> None:
    verdict = check_applicability(
        _check(evaluated_question_version=None, input_digest=None),
        current_question_id="q1",
        current_question_version="v2",
        current_input_digest="digest-a",
    )
    assert not verdict.applicable
    assert "legacy" in verdict.reason


def test_a_check_whose_inputs_changed_does_not_apply() -> None:
    verdict = check_applicability(
        _check(),
        current_question_id="q1",
        current_question_version="v2",
        current_input_digest="digest-b",
    )
    assert not verdict.applicable
    assert "changed" in verdict.reason


def test_a_failed_check_does_not_apply() -> None:
    verdict = check_applicability(
        _check(status=ValidationStatus.FAILED),
        current_question_id="q1",
        current_question_version="v2",
        current_input_digest="digest-a",
    )
    assert not verdict.applicable
    assert "failed" in verdict.reason


def test_a_not_run_check_does_not_apply() -> None:
    verdict = check_applicability(
        _check(status=ValidationStatus.NOT_RUN),
        current_question_id="q1",
        current_question_version="v2",
        current_input_digest="digest-a",
    )
    assert not verdict.applicable


def test_a_disallowed_validator_version_does_not_apply() -> None:
    verdict = check_applicability(
        _check(),
        current_question_id="q1",
        current_question_version="v2",
        current_input_digest="digest-a",
        allowed_validator_versions={"validators.v2"},
    )
    assert not verdict.applicable
    assert "validator policy" in verdict.reason


def test_an_ancestor_version_applies_when_the_inputs_are_unchanged() -> None:
    verdict = check_applicability(
        _check(evaluated_question_version="v1"),
        current_question_id="q1",
        current_question_version="v2",
        current_input_digest="digest-a",
        ancestor_versions={"v1", "v2"},
    )
    assert verdict.applicable


def test_an_unrelated_version_does_not_apply() -> None:
    verdict = check_applicability(
        _check(evaluated_question_version="v9"),
        current_question_id="q1",
        current_question_version="v2",
        current_input_digest="digest-a",
        ancestor_versions={"v1", "v2"},
    )
    assert not verdict.applicable
    assert "ancestor" in verdict.reason


def test_applicable_checks_reports_why_each_one_was_excluded() -> None:
    applies, excluded = applicable_checks(
        [_check(), _check(check_id="c2", input_digest="other")],
        current_question_id="q1",
        current_question_version="v2",
        current_input_digest="digest-a",
    )
    assert len(applies) == 1
    assert len(excluded) == 1
    assert excluded[0][1].reason


# ---- version ancestry ----------------------------------------------------


def _question_with_chain(versions: list[tuple[str | None, str]]) -> object:
    """Build a minimal question-like object with a change chain."""
    from medical_qa_data_factory.domain.questions import QuestionSpec

    now = utc_now()
    changes = [
        ChangeRecord(
            change_id=new_id("change"),
            actor_type="system",
            operation="test",
            summary="link",
            created_at=now,
            parent_version=parent,
            new_version=new,
        )
        for parent, new in versions
    ]
    return QuestionSpec(
        question_id="q1",
        logical_question_id="l1",
        question_version=versions[-1][1],
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
        created_at=now,
        changes=changes,
    )


def test_version_ancestry_walks_the_parent_chain_backwards() -> None:
    question = _question_with_chain(
        [("v0", "v1"), ("v1", "v2"), ("v2", "v3")]
    )
    ancestry = version_ancestry(question)
    assert ancestry == {"v3", "v2", "v1", "v0"}


def test_version_ancestry_contains_the_current_version() -> None:
    question = _question_with_chain([("v0", "v1")])
    assert "v1" in version_ancestry(question)


# ---- dependency digest ---------------------------------------------------


def test_dependency_digest_is_stable_for_equal_payloads() -> None:
    assert dependency_digest({"a": 1, "b": 2}) == dependency_digest(
        {"b": 2, "a": 1}
    )


def test_dependency_digest_changes_with_the_payload() -> None:
    assert dependency_digest({"a": 1}) != dependency_digest({"a": 2})


def test_route_confirmation_does_not_invalidate_evidence_checks() -> None:
    """Confirming the route is not an evidence change.

    ``verified_route`` is the outcome of source ablation, so a human who
    confirms it must not invalidate the question's evidence checks.
    """
    from medical_qa_data_factory.domain.questions import Route

    question = _question_with_chain([("v0", "v1")])
    before = question_dependency_digest(question)
    confirmed = question.model_copy(update={"verified_route": Route.KG})
    assert question_dependency_digest(confirmed) == before


def test_editing_the_answer_invalidates_evidence_checks() -> None:
    question = _question_with_chain([("v0", "v1")])
    before = question_dependency_digest(question)
    edited = question.model_copy(
        update={"gold_answer": question.gold_answer.model_copy(
            update={"short_answer": "different"}
        )}
    )
    assert question_dependency_digest(edited) != before