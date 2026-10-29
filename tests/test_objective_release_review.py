"""Two-stage isolated release review and fail-closed report merger."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medical_qa_data_factory.generation.evidence_provider import (
    ProviderTerminalStatus,
)
from medical_qa_data_factory.generation.objective_question_contracts import (
    AnswerCardinality,
    AnswerEvidenceAuditResult,
    AnswerResolution,
    AnswerStatus,
    CompletenessBasis,
    FrozenQuestionContract,
    ObjectiveValidationReport,
    ObjectiveVerdict,
    PublicQuestionAuditResult,
    text_digest,
)
from medical_qa_data_factory.generation.release_review_subagent import (
    AnswerEvidenceAuditPackage,
    IndependentEvidenceAuditPackage,
    PublicQuestionPackage,
    ReleaseReviewSubagent,
    load_validation_skill,
    merge_objective_audits,
)
from medical_qa_data_factory.llm.fake import FakeStructuredModel


NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
QUESTION = "On which human chromosome is BRCA1 located?"


def _contract() -> FrozenQuestionContract:
    return FrozenQuestionContract(
        contract_id="contract-brca1",
        question_intent=QUESTION,
        public_scope="Homo sapiens reference genome",
        answer_type="entity",
        answer_values=["chromosome 17"],
        answer_cardinality=AnswerCardinality.EXACTLY_ONE,
        claim_strength="directional_fact",
        required_public_conditions=["Homo sapiens reference genome"],
        allowed_wording_constraints=["preserve answer cardinality"],
        forbidden_semantic_upgrades=["causal", "treatment"],
        resolution_id="resolution-brca1",
    )


def _resolution() -> AnswerResolution:
    return AnswerResolution(
        resolution_id="resolution-brca1",
        hypothesis_id="hypothesis-brca1",
        status=AnswerStatus.VERIFIED,
        verified_answer_values=["chromosome 17"],
        answer_cardinality=AnswerCardinality.EXACTLY_ONE,
        public_scope="Homo sapiens reference genome",
        supporting_anchor_ids=["anchor-verification"],
        contradicting_anchor_ids=[],
        competing_answers_checked=True,
        conflict_checked=True,
        completeness_basis=CompletenessBasis.CANONICAL_IDENTITY,
        verification_sources=["authority-b"],
        verification_policy_version="objective-answer-verification-v1",
        blind_answer_query_digest="a" * 64,
        verification_plan_id="plan-brca1",
        verification_coverage_report_id="coverage-brca1",
    )


def _public_package() -> PublicQuestionPackage:
    return PublicQuestionPackage(
        question_id="question-brca1",
        question_text=QUESTION,
    )


def _answer_package() -> AnswerEvidenceAuditPackage:
    return AnswerEvidenceAuditPackage(
        question=_public_package(),
        frozen_question_contract=_contract(),
        answer_resolution=_resolution(),
        independent_evidence=IndependentEvidenceAuditPackage(
            evidence_used=["authority-b:gene:BRCA1"],
            anchors=[
                {
                    "anchor_id": "anchor-verification",
                    "quoted_text": "BRCA1 maps to human chromosome 17.",
                }
            ],
        ),
    )


def _public_result(
    *,
    verdict: ObjectiveVerdict = ObjectiveVerdict.PASS,
) -> PublicQuestionAuditResult:
    return PublicQuestionAuditResult(
        task_id="subtask-public",
        public_question_digest=text_digest(QUESTION),
        self_contained=True,
        source_independent=True,
        asks_underlying_fact=True,
        entity_unambiguous=True,
        cardinality_clear=True,
        verdict=verdict,
        skill_digest="b" * 64,
        primary_reason=(
            "PASS" if verdict is ObjectiveVerdict.PASS else "MISSING_PUBLIC_CONTEXT"
        ),
        secondary_reasons=[],
    )


def _answer_result(
    *,
    verdict: ObjectiveVerdict = ObjectiveVerdict.PASS,
    relation_strength_preserved: bool = True,
) -> AnswerEvidenceAuditResult:
    contract = _contract()
    return AnswerEvidenceAuditResult(
        task_id="subtask-answer",
        public_question_digest=text_digest(QUESTION),
        question_answer_contract_digest=contract.digest(),
        independent_evidence_digest="c" * 64,
        independently_supported=True,
        unique_or_closed=True,
        contradiction_checked=True,
        relation_strength_preserved=relation_strength_preserved,
        verdict=verdict,
        skill_digest="b" * 64,
        primary_reason=(
            "PASS" if verdict is ObjectiveVerdict.PASS else "UNSUPPORTED_ANSWER"
        ),
        secondary_reasons=[],
    )


def _model(
    public_response=None,
    answer_response=None,
) -> FakeStructuredModel:
    def response(request):
        stage = request.payload["stage"]
        if stage == "public_question_audit":
            return public_response or _public_result()
        if stage == "answer_evidence_audit":
            return answer_response or _answer_result()
        raise AssertionError(stage)

    return FakeStructuredModel(
        responses={"objective_release_review": response}
    )


def test_public_stage_cannot_see_answer_contract_or_evidence(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Validator\n\nJudge the public question only.", encoding="utf-8")
    model = _model()

    ReleaseReviewSubagent(model).run(
        public_question=_public_package(),
        answer_evidence=_answer_package(),
        skill_path=skill,
    )

    first_payload = model.payloads_for("objective_release_review")[0]
    serialized = json.dumps(first_payload)
    assert first_payload["stage"] == "public_question_audit"
    assert QUESTION in serialized
    assert "chromosome 17" not in serialized
    assert "contract-brca1" not in serialized
    assert "anchor-verification" not in serialized


def test_second_stage_is_created_only_after_public_audit_is_frozen(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Validator", encoding="utf-8")
    model = _model()

    report = ReleaseReviewSubagent(model).run(
        public_question=_public_package(),
        answer_evidence=_answer_package(),
        skill_path=skill,
    )

    calls = model.payloads_for("objective_release_review")
    assert [item["stage"] for item in calls] == [
        "public_question_audit",
        "answer_evidence_audit",
    ]
    assert calls[1]["public_question_audit"]["task_id"] == "subtask-public"
    assert "chromosome 17" in json.dumps(calls[1])
    assert report.verdict is ObjectiveVerdict.PASS
    assert report.release_allowed is True
    assert report.validator_version.startswith(
        "objective-question-validator@sha256:"
    )


def test_public_rejection_stops_before_answer_audit_creation(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Validator", encoding="utf-8")
    model = _model(
        public_response=_public_result(verdict=ObjectiveVerdict.REJECT)
    )

    report = ReleaseReviewSubagent(model).run(
        public_question=_public_package(),
        answer_evidence=_answer_package(),
        skill_path=skill,
    )

    assert len(model.invocations) == 1
    assert report.verdict is ObjectiveVerdict.REJECT
    assert report.release_allowed is False


def test_a_pass_verdict_with_a_failed_hard_gate_cannot_release(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Validator", encoding="utf-8")
    model = _model(
        answer_response=_answer_result(relation_strength_preserved=False)
    )

    report = ReleaseReviewSubagent(model).run(
        public_question=_public_package(),
        answer_evidence=_answer_package(),
        skill_path=skill,
    )

    assert report.verdict is ObjectiveVerdict.REJECT
    assert report.release_allowed is False
    assert "relation_strength" in report.blocking_findings[0]


def test_invalid_structured_output_is_indeterminate(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Validator", encoding="utf-8")
    model = FakeStructuredModel(
        responses={"objective_release_review": "not-json"}
    )

    report = ReleaseReviewSubagent(model).run(
        public_question=_public_package(),
        answer_evidence=_answer_package(),
        skill_path=skill,
    )

    assert report.verdict is ObjectiveVerdict.INDETERMINATE
    assert report.release_allowed is False
    assert report.primary_reason == "MODEL_OUTPUT_INVALID"


def test_merge_detects_stale_skill_or_input_digest() -> None:
    public = _public_result()
    answer = _answer_result()

    stale_skill = merge_objective_audits(
        question_id="question-brca1",
        public_audit=public,
        answer_audit=answer,
        current_public_question_digest=text_digest(QUESTION),
        current_contract_digest=_contract().digest(),
        current_independent_evidence_digest="c" * 64,
        current_skill_digest="d" * 64,
        evidence_used=["authority-b:gene:BRCA1"],
    )
    stale_question = merge_objective_audits(
        question_id="question-brca1",
        public_audit=public,
        answer_audit=answer,
        current_public_question_digest=text_digest(QUESTION + " "),
        current_contract_digest=_contract().digest(),
        current_independent_evidence_digest="c" * 64,
        current_skill_digest="b" * 64,
        evidence_used=["authority-b:gene:BRCA1"],
    )

    assert stale_skill.verdict is ObjectiveVerdict.INDETERMINATE
    assert stale_skill.primary_reason == "STALE_SKILL"
    assert stale_question.verdict is ObjectiveVerdict.INDETERMINATE
    assert stale_question.primary_reason == "STALE_INPUT"


def test_validation_skill_loader_hashes_exact_content(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Validator\ncontent", encoding="utf-8")
    loaded = load_validation_skill(skill)

    assert loaded.content == "# Validator\ncontent"
    assert loaded.digest == text_digest("# Validator\ncontent")
    assert loaded.version.startswith("sha256:")
