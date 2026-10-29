"""Independent, three-stage objective-answer verification."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from medical_qa_data_factory.generation.answer_verification_subagent import (
    AnswerVerificationSubagent,
    BlindSolveResult,
    CounterexampleReview,
    VerificationComparisonResult,
    VerificationEvidenceBundle,
    build_blind_answer_query,
)
from medical_qa_data_factory.generation.evidence_provider import (
    ProviderTerminalStatus,
)
from medical_qa_data_factory.generation.objective_question_contracts import (
    AnswerCardinality,
    AnswerStatus,
    CandidateClaim,
    CompletenessBasis,
    CoverageStatus,
    EvidenceAnchor,
    QuestionHypothesis,
    SubagentTask,
    VerificationAttempt,
    VerificationCoverageReport,
    VerificationPlan,
    VerificationVerdict,
    text_digest,
)
from medical_qa_data_factory.generation.subagent_contracts import (
    RoleContractViolation,
    SubagentRole,
    build_objective_subagent_task,
    default_objective_role_registry,
)
from medical_qa_data_factory.llm.fake import FakeStructuredModel


NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def _claim() -> CandidateClaim:
    return CandidateClaim(
        claim_id="claim-brca1-chr17",
        subject="BRCA1",
        predicate="located_on",
        object_or_value="human chromosome 17",
        qualifiers={"species": "Homo sapiens"},
        claim_strength="directional_fact",
        polarity="POSITIVE",
        supporting_anchor_ids=["anchor-discovery"],
        contradicting_anchor_ids=[],
        extraction_model_version="fake-model-v1",
    )


def _hypothesis() -> QuestionHypothesis:
    return QuestionHypothesis(
        hypothesis_id="hypothesis-brca1",
        candidate_claim_ids=["claim-brca1-chr17"],
        question_intent="On which human chromosome is BRCA1 located?",
        proposed_public_scope="Homo sapiens reference genome",
        proposed_answer_type="entity",
        proposed_answer_values=["chromosome 17"],
        proposed_cardinality=AnswerCardinality.EXACTLY_ONE,
        discovery_anchor_ids=["anchor-discovery"],
    )


def _anchor(anchor_id: str, quote: str) -> EvidenceAnchor:
    return EvidenceAnchor(
        anchor_id=anchor_id,
        material_id=f"material-{anchor_id}",
        document_id=f"document-{anchor_id}",
        section_or_field="description",
        start_offset=0,
        end_offset=len(quote),
        quoted_text=quote,
        quote_hash=text_digest(quote),
    )


def _plan(*attempt_ids: str) -> VerificationPlan:
    query = build_blind_answer_query(_hypothesis(), [_claim()])
    return VerificationPlan(
        plan_id="verification-plan-brca1",
        hypothesis_id=_hypothesis().hypothesis_id,
        blind_answer_query_digest=query.input_digest(),
        expected_attempt_ids=list(attempt_ids),
        allowed_provider_ids=["authority-b", "authority-c"],
        competing_answer_strategy="query authoritative identity records",
        contradiction_strategy="search contradictory locations",
        budget={"max_provider_attempts": len(attempt_ids)},
        stop_conditions=["all_attempts_terminal", "budget_exhausted"],
    )


def _attempt(
    attempt_id: str,
    *,
    provider_id: str,
    anchor_id: str,
    status: ProviderTerminalStatus = ProviderTerminalStatus.SUCCEEDED,
) -> VerificationAttempt:
    return VerificationAttempt(
        attempt_id=attempt_id,
        plan_id="verification-plan-brca1",
        query=f"query {attempt_id}",
        provider_id=provider_id,
        started_at=NOW,
        finished_at=NOW,
        terminal_status=status,
        error_category=(
            "PROVIDER_FAILURE"
            if status
            in {ProviderTerminalStatus.FAILED, ProviderTerminalStatus.UNKNOWN}
            else None
        ),
        material_ids=[f"material-{anchor_id}"],
        anchor_ids=[anchor_id],
    )


def _complete_coverage(*attempt_ids: str) -> VerificationCoverageReport:
    return VerificationCoverageReport(
        coverage_report_id="coverage-brca1",
        expected_attempt_ids=list(attempt_ids),
        terminal_attempt_ids=list(attempt_ids),
        successful_attempt_ids=list(attempt_ids),
        failed_attempt_ids=[],
        unknown_attempt_ids=[],
        budget_exhausted=False,
        competing_answer_queries=1,
        contradiction_queries=1,
        coverage_status=CoverageStatus.COMPLETE,
    )


def _bundle(
    *,
    attempts: list[VerificationAttempt],
    coverage: VerificationCoverageReport,
    supporting_anchor_ids: list[str] | None = None,
    contradiction_anchor_ids: list[str] | None = None,
    completeness_basis: CompletenessBasis = CompletenessBasis.CANONICAL_IDENTITY,
) -> VerificationEvidenceBundle:
    return VerificationEvidenceBundle(
        plan=_plan(*(item.attempt_id for item in attempts)),
        attempts=attempts,
        coverage=coverage,
        anchors=[
            _anchor("anchor-discovery", "BRCA1 is on chromosome 17."),
            _anchor("anchor-verification", "BRCA1 maps to chromosome 17."),
            _anchor("anchor-contradiction", "BRCA1 maps elsewhere."),
        ],
        supporting_anchor_ids=(
            supporting_anchor_ids
            if supporting_anchor_ids is not None
            else ["anchor-verification"]
        ),
        contradicting_anchor_ids=contradiction_anchor_ids or [],
        completeness_basis=completeness_basis,
    )


def _task() -> SubagentTask:
    capability = default_objective_role_registry()[
        SubagentRole.ANSWER_VERIFICATION
    ]
    return build_objective_subagent_task(
        task_id="subtask-answer-verification",
        role=SubagentRole.ANSWER_VERIFICATION,
        capability=capability,
        goal={"blind_answer_query": build_blind_answer_query(_hypothesis(), [_claim()])},
        input_artifact_ids=["hypothesis-brca1", "claim-brca1-chr17"],
    )


def _model() -> FakeStructuredModel:
    def response(request):
        stage = request.payload["stage"]
        if stage == "blind_solve":
            return BlindSolveResult(
                query_id=request.payload["blind_answer_query"]["query_id"],
                answer_values=["chromosome 17"],
                uncertain=False,
            )
        if stage == "compare":
            return VerificationComparisonResult(
                answer_matches_blind=True,
                competing_answers=[],
                conflict_found=False,
            )
        if stage == "counterexample_review":
            return CounterexampleReview(
                conflict_found=False,
                contradicting_anchor_ids=[],
            )
        raise AssertionError(stage)

    return FakeStructuredModel(
        responses={"objective_answer_verification": response}
    )


def test_blind_stage_cannot_see_proposed_answer_or_discovery_anchor() -> None:
    model = _model()
    subagent = AnswerVerificationSubagent(model)
    attempts = [
        _attempt(
            "verification-1",
            provider_id="authority-b",
            anchor_id="anchor-verification",
        )
    ]

    subagent.run(
        _task(),
        hypothesis=_hypothesis(),
        claims=[_claim()],
        evidence=_bundle(
            attempts=attempts,
            coverage=_complete_coverage("verification-1"),
        ),
    )

    blind_payload = model.payloads_for("objective_answer_verification")[0]
    serialized = json.dumps(blind_payload)
    assert blind_payload["stage"] == "blind_solve"
    assert "proposed_answer_values" not in serialized
    assert "anchor-discovery" not in serialized
    later_payloads = model.payloads_for("objective_answer_verification")[1:]
    assert any("proposed_answer_values" in json.dumps(item) for item in later_payloads)


def test_consistent_independent_answer_is_verified_and_frozen() -> None:
    attempts = [
        _attempt(
            "verification-1",
            provider_id="authority-b",
            anchor_id="anchor-verification",
        )
    ]
    outcome = AnswerVerificationSubagent(_model()).run(
        _task(),
        hypothesis=_hypothesis(),
        claims=[_claim()],
        evidence=_bundle(
            attempts=attempts,
            coverage=_complete_coverage("verification-1"),
        ),
    )

    assert outcome.resolution.status is AnswerStatus.VERIFIED
    assert outcome.resolution.verified_answer_values == ["chromosome 17"]
    assert outcome.result.verdict is VerificationVerdict.VERIFIED
    assert outcome.frozen_contract is not None
    assert outcome.frozen_contract.answer_values == ["chromosome 17"]
    assert outcome.result.output_artifact_ids == [
        "resolution-brca1",
        "contract-brca1",
    ]


def test_blind_answer_disagreement_rejects_the_hypothesis() -> None:
    def response(request):
        if request.payload["stage"] == "blind_solve":
            return BlindSolveResult(
                query_id=request.payload["blind_answer_query"]["query_id"],
                answer_values=["chromosome 13"],
                uncertain=False,
            )
        if request.payload["stage"] == "compare":
            return VerificationComparisonResult(
                answer_matches_blind=False,
                competing_answers=[],
                conflict_found=False,
            )
        return CounterexampleReview(
            conflict_found=False,
            contradicting_anchor_ids=[],
        )

    attempts = [
        _attempt(
            "verification-1",
            provider_id="authority-b",
            anchor_id="anchor-verification",
        )
    ]
    outcome = AnswerVerificationSubagent(
        FakeStructuredModel(
            responses={"objective_answer_verification": response}
        )
    ).run(
        _task(),
        hypothesis=_hypothesis(),
        claims=[_claim()],
        evidence=_bundle(
            attempts=attempts,
            coverage=_complete_coverage("verification-1"),
        ),
    )

    assert outcome.resolution.status is AnswerStatus.REJECTED
    assert outcome.result.stop_reason == "ANSWER_MISMATCH"
    assert outcome.frozen_contract is None


def test_empty_provider_result_is_indeterminate_not_a_negative_answer() -> None:
    attempts = [
        _attempt(
            "verification-empty",
            provider_id="authority-b",
            anchor_id="anchor-verification",
            status=ProviderTerminalStatus.EMPTY,
        )
    ]
    coverage = VerificationCoverageReport(
        coverage_report_id="coverage-brca1",
        expected_attempt_ids=["verification-empty"],
        terminal_attempt_ids=["verification-empty"],
        successful_attempt_ids=[],
        failed_attempt_ids=[],
        unknown_attempt_ids=[],
        budget_exhausted=False,
        competing_answer_queries=1,
        contradiction_queries=1,
        coverage_status=CoverageStatus.INCOMPLETE,
    )
    outcome = AnswerVerificationSubagent(_model()).run(
        _task(),
        hypothesis=_hypothesis(),
        claims=[_claim()],
        evidence=_bundle(attempts=attempts, coverage=coverage),
    )

    assert outcome.resolution.status is AnswerStatus.INDETERMINATE
    assert outcome.result.stop_reason == "INCOMPLETE_VERIFICATION_COVERAGE"


def test_discovery_only_evidence_cannot_verify_the_answer() -> None:
    attempts = [
        _attempt(
            "verification-1",
            provider_id="authority-b",
            anchor_id="anchor-verification",
        )
    ]
    outcome = AnswerVerificationSubagent(_model()).run(
        _task(),
        hypothesis=_hypothesis(),
        claims=[_claim()],
        evidence=_bundle(
            attempts=attempts,
            coverage=_complete_coverage("verification-1"),
            supporting_anchor_ids=["anchor-discovery"],
        ),
    )

    assert outcome.resolution.status is AnswerStatus.INDETERMINATE
    assert outcome.result.stop_reason == "INSUFFICIENT_VALIDATION_EVIDENCE"


@pytest.mark.parametrize(
    ("comparison", "review", "reason"),
    [
        (
            VerificationComparisonResult(
                answer_matches_blind=True,
                competing_answers=["chromosome 13"],
                conflict_found=False,
            ),
            CounterexampleReview(
                conflict_found=False,
                contradicting_anchor_ids=[],
            ),
            "ANSWER_NOT_UNIQUE",
        ),
        (
            VerificationComparisonResult(
                answer_matches_blind=True,
                competing_answers=[],
                conflict_found=False,
            ),
            CounterexampleReview(
                conflict_found=True,
                contradicting_anchor_ids=["anchor-contradiction"],
            ),
            "CONFLICTING_EVIDENCE",
        ),
    ],
)
def test_competition_or_conflict_rejects_the_answer(
    comparison: VerificationComparisonResult,
    review: CounterexampleReview,
    reason: str,
) -> None:
    def response(request):
        if request.payload["stage"] == "blind_solve":
            return BlindSolveResult(
                query_id=request.payload["blind_answer_query"]["query_id"],
                answer_values=["chromosome 17"],
                uncertain=False,
            )
        if request.payload["stage"] == "compare":
            return comparison
        return review

    attempts = [
        _attempt(
            "verification-1",
            provider_id="authority-b",
            anchor_id="anchor-verification",
        )
    ]
    outcome = AnswerVerificationSubagent(
        FakeStructuredModel(
            responses={"objective_answer_verification": response}
        )
    ).run(
        _task(),
        hypothesis=_hypothesis(),
        claims=[_claim()],
        evidence=_bundle(
            attempts=attempts,
            coverage=_complete_coverage("verification-1"),
        ),
    )

    assert outcome.resolution.status is AnswerStatus.REJECTED
    assert outcome.result.stop_reason == reason


def test_plan_and_coverage_must_reference_the_same_attempts() -> None:
    attempts = [
        _attempt(
            "verification-1",
            provider_id="authority-b",
            anchor_id="anchor-verification",
        )
    ]
    evidence = _bundle(
        attempts=attempts,
        coverage=_complete_coverage("different-attempt"),
    )

    with pytest.raises(RoleContractViolation, match="coverage"):
        AnswerVerificationSubagent(_model()).run(
            _task(),
            hypothesis=_hypothesis(),
            claims=[_claim()],
            evidence=evidence,
        )
