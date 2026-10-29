"""Step 1 contracts for the evidence-first objective-question pipeline."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from medical_qa_data_factory.generation.objective_question_contracts import (
    AnswerCardinality,
    AnswerResolution,
    AnswerStatus,
    BlindAnswerQuery,
    CompletenessBasis,
    DomainOutcome,
    ExecutionStatus,
    HypothesisState,
    ObjectiveValidationReport,
    ObjectiveVerdict,
    OperationRecord,
    RunState,
    SubagentResult,
    VerificationCoverageReport,
    VerificationVerdict,
    aggregate_generation_run,
    transition_hypothesis,
)


NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def test_blind_answer_query_cannot_carry_candidate_answer_or_discovery() -> None:
    query = BlindAnswerQuery(
        query_id="blind-1",
        question_intent="Identify the chromosome containing BRCA1.",
        public_scope="human reference genome",
        answer_type="entity",
        answer_cardinality=AnswerCardinality.EXACTLY_ONE,
        required_relation_strength="directional_fact",
    )

    assert query.input_digest()
    with pytest.raises(ValidationError):
        BlindAnswerQuery(
            **query.model_dump(),
            proposed_answer_values=["chromosome 17"],
        )
    with pytest.raises(ValidationError):
        BlindAnswerQuery(
            **query.model_dump(),
            discovery_anchor_ids=["anchor-1"],
        )


def test_verified_resolution_requires_a_completeness_basis() -> None:
    with pytest.raises(ValidationError, match="completeness_basis"):
        AnswerResolution(
            resolution_id="resolution-1",
            hypothesis_id="hypothesis-1",
            status=AnswerStatus.VERIFIED,
            verified_answer_values=["chromosome 17"],
            answer_cardinality=AnswerCardinality.EXACTLY_ONE,
            public_scope="human reference genome",
            supporting_anchor_ids=["anchor-2"],
            contradicting_anchor_ids=[],
            competing_answers_checked=True,
            conflict_checked=True,
            completeness_basis=CompletenessBasis.NOT_ESTABLISHED,
            verification_sources=["authority-a"],
            verification_policy_version="objective-policy-v1",
            blind_answer_query_digest="a" * 64,
            verification_plan_id="plan-1",
            verification_coverage_report_id="coverage-1",
        )


def test_verified_resolution_requires_independent_support_and_complete_coverage() -> None:
    base = dict(
        resolution_id="resolution-1",
        hypothesis_id="hypothesis-1",
        status=AnswerStatus.VERIFIED,
        verified_answer_values=["chromosome 17"],
        answer_cardinality=AnswerCardinality.EXACTLY_ONE,
        public_scope="human reference genome",
        supporting_anchor_ids=["anchor-2"],
        contradicting_anchor_ids=[],
        competing_answers_checked=True,
        conflict_checked=True,
        completeness_basis=CompletenessBasis.CANONICAL_IDENTITY,
        verification_sources=["authority-a"],
        verification_policy_version="objective-policy-v1",
        blind_answer_query_digest="a" * 64,
        verification_plan_id="plan-1",
        verification_coverage_report_id="coverage-1",
    )

    with pytest.raises(ValidationError, match="supporting_anchor_ids"):
        AnswerResolution(**{**base, "supporting_anchor_ids": []})
    with pytest.raises(ValidationError, match="competing_answers_checked"):
        AnswerResolution(**{**base, "competing_answers_checked": False})
    with pytest.raises(ValidationError, match="conflict_checked"):
        AnswerResolution(**{**base, "conflict_checked": False})


@pytest.mark.parametrize(
    "payload",
    [
        {
            "expected_attempt_ids": ["a1", "a2"],
            "terminal_attempt_ids": ["a1"],
            "successful_attempt_ids": ["a1"],
            "failed_attempt_ids": [],
            "unknown_attempt_ids": [],
            "budget_exhausted": False,
            "competing_answer_queries": 1,
            "contradiction_queries": 1,
            "coverage_status": "COMPLETE",
        },
        {
            "expected_attempt_ids": ["a1"],
            "terminal_attempt_ids": ["a1"],
            "successful_attempt_ids": [],
            "failed_attempt_ids": ["a1"],
            "unknown_attempt_ids": [],
            "budget_exhausted": False,
            "competing_answer_queries": 1,
            "contradiction_queries": 1,
            "coverage_status": "COMPLETE",
        },
        {
            "expected_attempt_ids": ["a1"],
            "terminal_attempt_ids": ["a1"],
            "successful_attempt_ids": ["a1"],
            "failed_attempt_ids": [],
            "unknown_attempt_ids": [],
            "budget_exhausted": True,
            "competing_answer_queries": 1,
            "contradiction_queries": 1,
            "coverage_status": "COMPLETE",
        },
    ],
)
def test_complete_coverage_requires_all_successful_terminal_attempts(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        VerificationCoverageReport(
            coverage_report_id="coverage-1",
            **payload,
        )


def test_subagent_result_keeps_execution_and_domain_outcomes_orthogonal() -> None:
    failed = SubagentResult(
        subtask_id="subtask-1",
        execution_status=ExecutionStatus.FAILED,
        domain_outcome=DomainOutcome.INSUFFICIENT,
        verdict=VerificationVerdict.INDETERMINATE,
        output_artifact_ids=[],
        evidence_need_ids=[],
        usage={"provider_calls": 1},
        error_category="RATE_LIMITED",
        stop_reason="retryable_provider_failure",
    )
    assert failed.execution_status is ExecutionStatus.FAILED
    assert failed.domain_outcome is DomainOutcome.INSUFFICIENT

    with pytest.raises(ValidationError, match="error_category"):
        SubagentResult(
            subtask_id="subtask-2",
            execution_status=ExecutionStatus.SUCCEEDED,
            domain_outcome=DomainOutcome.SUPPORTED,
            verdict=VerificationVerdict.VERIFIED,
            output_artifact_ids=["claim-1"],
            evidence_need_ids=[],
            usage={},
            error_category="RATE_LIMITED",
            stop_reason="complete",
        )


def test_publication_requires_pass_and_release_allowed_both_be_true() -> None:
    with pytest.raises(ValidationError, match="release_allowed"):
        ObjectiveValidationReport(
            report_id="report-1",
            question_id="question-1",
            verdict=ObjectiveVerdict.REJECT,
            release_allowed=True,
            primary_reason="CAUSAL_OVERCLAIM",
            secondary_reasons=[],
            public_question_audit_id="public-audit-1",
            answer_audit_id="answer-audit-1",
            blocking_findings=[],
            evidence_used=["anchor-1"],
            validator_version="objective-question-validator-v1",
            input_digest="b" * 64,
        )


def test_zero_question_completion_is_distinct_from_recovery() -> None:
    assert (
        aggregate_generation_run([HypothesisState.REJECTED])
        is RunState.COMPLETED_WITH_ZERO_QUESTIONS
    )
    assert (
        aggregate_generation_run([HypothesisState.INDETERMINATE])
        is RunState.COMPLETED_WITH_ZERO_QUESTIONS
    )
    assert aggregate_generation_run([], no_hypotheses=True) is (
        RunState.COMPLETED_WITH_ZERO_QUESTIONS
    )
    assert aggregate_generation_run([], recovery_required=True) is (
        RunState.RECOVERY_REQUIRED
    )
    assert aggregate_generation_run([HypothesisState.PUBLISHED]) is RunState.COMPLETED


def test_hypothesis_state_machine_rejects_skipped_verification() -> None:
    assert (
        transition_hypothesis(HypothesisState.PROPOSED, HypothesisState.VERIFYING)
        is HypothesisState.VERIFYING
    )
    with pytest.raises(ValueError, match="PROPOSED"):
        transition_hypothesis(
            HypothesisState.PROPOSED,
            HypothesisState.PUBLISHED,
        )


def test_operation_record_requires_error_category_only_for_failed_execution() -> None:
    base = dict(
        operation_key="run-1|verify|hypothesis-1",
        attempt_id="attempt-1",
        retry_ordinal=0,
        parent_subtask_id="subtask-1",
        operation_type="llm_call",
        input_digest="c" * 64,
        started_at=NOW,
        finished_at=NOW,
        deduplication_status="COMMITTED",
        execution_status="SUCCEEDED",
        domain_outcome="SUPPORTED",
        verdict="VERIFIED",
        output_artifact_ids=["resolution-1"],
        usage={},
        cost=0.0,
        latency_ms=10,
    )
    assert OperationRecord(**base).error_category is None
    with pytest.raises(ValidationError, match="error_category"):
        OperationRecord(
            **{**base, "execution_status": "FAILED", "error_category": None}
        )
