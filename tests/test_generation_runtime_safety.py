"""Idempotency, stale input, recovery, and loop safety for objective runs."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from medical_qa_data_factory.generation.objective_question_contracts import (
    DomainOutcome,
    ExecutionStatus,
    MainAgentDecision,
    OperationRecord,
    RunManifest,
    RunState,
    VerificationVerdict,
    canonical_digest,
)
from medical_qa_data_factory.generation.agent_loop import (
    ObjectiveQuestionAgentLoop,
)
from medical_qa_data_factory.generation.objective_question_runtime import (
    CheckpointIntegrityError,
    LoopDetected,
    ObjectiveRuntimeRepository,
    OperationConflict,
    RuntimeBudget,
    RuntimeVersionIncompatible,
    SubagentLoopGuard,
    assess_runtime_bundle,
)
from medical_qa_data_factory.llm.errors import ModelError, ModelErrorCode


NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def _manifest() -> RunManifest:
    return RunManifest(
        manifest_id="manifest-run-1",
        run_id="run-1",
        orchestrator_version="orchestrator-v1",
        main_agent_version="main-agent-v1",
        subagent_role_versions={"fact_understanding": "v1"},
        model_versions={"mock": "v1"},
        schema_versions={"contracts": "objective-question-v1"},
        validation_policy_version="policy-v1",
        data_snapshot_or_retrieval_time_policy="pinned",
        runtime_bundle_id="runtime-bundle-v1",
        runtime_bundle_digest="a" * 64,
    )


def _operation(
    *,
    attempt_id: str = "attempt-1",
    input_digest: str = "c" * 64,
    dedup: str = "COMMITTED",
    execution: str = "SUCCEEDED",
    error_category: str | None = None,
    cost: float = 1.25,
) -> OperationRecord:
    return OperationRecord(
        operation_key="run-1|propose-facts|1",
        attempt_id=attempt_id,
        retry_ordinal=0 if attempt_id == "attempt-1" else 1,
        parent_subtask_id="subtask-facts",
        operation_type="llm_call",
        input_digest=input_digest,
        started_at=NOW,
        finished_at=NOW,
        deduplication_status=dedup,
        commit_sequence=1,
        execution_status=execution,
        domain_outcome=DomainOutcome.SUPPORTED,
        verdict=VerificationVerdict.NOT_EMITTED,
        output_artifact_ids=["artifact-1"],
        usage={"input_tokens": 10},
        cost=cost,
        latency_ms=20,
        error_category=error_category,
    )


def _repo(tmp_path) -> ObjectiveRuntimeRepository:
    return ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10.0, max_model_calls=20),
    )


def test_duplicate_committed_operation_reuses_result_without_new_charge(
    tmp_path,
) -> None:
    repo = _repo(tmp_path)
    before = repo.load()
    first = repo.commit_operation(
        expected_state_revision=before.state_revision,
        record=_operation(),
    )
    after_first = repo.load()
    second = repo.commit_operation(
        expected_state_revision=before.state_revision,
        record=_operation(),
    )
    after_second = repo.load()

    assert first.attempt_id == second.attempt_id
    assert after_second.state_revision == after_first.state_revision
    assert after_second.budget.charged_cost == pytest.approx(1.25)


def test_same_operation_key_with_changed_input_is_a_conflict(tmp_path) -> None:
    repo = _repo(tmp_path)
    snapshot = repo.load()
    repo.commit_operation(
        expected_state_revision=snapshot.state_revision,
        record=_operation(),
    )

    with pytest.raises(OperationConflict):
        repo.commit_operation(
            expected_state_revision=repo.load().state_revision,
            record=_operation(
                attempt_id="attempt-other",
                input_digest="d" * 64,
            ),
        )


def test_retryable_failure_and_success_are_separate_attempts(tmp_path) -> None:
    repo = _repo(tmp_path)
    snapshot = repo.load()
    repo.commit_operation(
        expected_state_revision=snapshot.state_revision,
        record=_operation(
            dedup="RETRYABLE_FAILED",
            execution="FAILED",
            error_category="RATE_LIMITED",
            cost=0.25,
        ),
    )
    snapshot = repo.load()
    repo.commit_operation(
        expected_state_revision=snapshot.state_revision,
        record=_operation(attempt_id="attempt-2", cost=1.0),
    )
    record = repo.load().operations["run-1|propose-facts|1"]

    assert [item.attempt_id for item in record] == ["attempt-1", "attempt-2"]
    assert repo.load().budget.charged_cost == pytest.approx(1.25)


def test_runtime_bundle_must_be_loadable_and_match_its_digest() -> None:
    manifest = _manifest()
    with pytest.raises(RuntimeVersionIncompatible):
        assess_runtime_bundle(manifest, available_bundles={})
    with pytest.raises(CheckpointIntegrityError):
        assess_runtime_bundle(
            manifest,
            available_bundles={"runtime-bundle-v1": "b" * 64},
        )
    assert assess_runtime_bundle(
        manifest,
        available_bundles={"runtime-bundle-v1": "a" * 64},
    )


def test_subagent_model_failure_becomes_a_recoverable_artifact(
    tmp_path,
) -> None:
    repo = _repo(tmp_path)
    snapshot = repo.load()
    decision = MainAgentDecision(
        decision_id="decision-search-failure",
        action="retrieve_materials",
        target_subagent_role="material_retrieval",
        input_artifact_ids=[],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.5,
        action_parameters={},
    )
    loop = object.__new__(ObjectiveQuestionAgentLoop)
    loop.runtime = repo

    loop._record_recoverable_failure(
        decision,
        "retrieve_materials",
        ModelError(
            ModelErrorCode.SCHEMA_ERROR,
            "model returned invalid JSON",
        ),
    )

    artifacts = repo.load().artifacts
    failure = artifacts["failure.decision-search-failure"]
    assert failure.artifact_type == "subagent_failure"
    assert failure.payload["action"] == "retrieve_materials"
    assert failure.payload["error_code"] == "model_schema_error"


def test_repeated_model_failures_keep_attempt_details(
    tmp_path,
) -> None:
    repo = _repo(tmp_path)
    snapshot = repo.load()
    decision = MainAgentDecision(
        decision_id="decision-repeated-failure",
        action="review_release",
        target_subagent_role="release_review",
        input_artifact_ids=[],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.5,
        action_parameters={},
    )
    loop = object.__new__(ObjectiveQuestionAgentLoop)
    loop.runtime = repo

    loop._record_recoverable_failure(
        decision,
        "review_release",
        ModelError(
            ModelErrorCode.BAD_REQUEST,
            "first HTTP 400",
            status_code=400,
            provider_response='{"error":"schema"}',
            attempts=2,
        ),
    )
    loop._record_recoverable_failure(
        decision,
        "review_release",
        ModelError(
            ModelErrorCode.BAD_REQUEST,
            "second HTTP 400",
            status_code=400,
            provider_response='{"error":"context"}',
            attempts=4,
        ),
    )

    failures = [
        artifact
        for artifact in repo.load().artifacts.values()
        if artifact.artifact_type == "subagent_failure"
    ]
    assert len(failures) == 2
    assert {item.payload["attempts"] for item in failures} == {2, 4}
    assert {
        item.payload["provider_response"] for item in failures
    } == {'{"error":"schema"}', '{"error":"context"}'}


def test_loop_guard_stops_repeated_decision_at_unchanged_state() -> None:
    decision = MainAgentDecision(
        decision_id="decision-1",
        action="understand_facts",
        target_subagent_role="fact_understanding",
        input_artifact_ids=["material-1"],
        state_digest="e" * 64,
        requested_budget={"model_calls": 1},
        expected_information_gain=0.1,
    )
    guard = SubagentLoopGuard(max_repeated_decisions=2)
    guard.observe(decision)
    guard.observe(decision.model_copy(update={"decision_id": "decision-2"}))

    with pytest.raises(LoopDetected):
        guard.observe(decision.model_copy(update={"decision_id": "decision-3"}))


def test_decision_fingerprint_ignores_only_decision_id() -> None:
    left = MainAgentDecision(
        decision_id="a",
        action="understand_facts",
        target_subagent_role="fact_understanding",
        input_artifact_ids=["x"],
        state_digest="e" * 64,
        requested_budget={},
        expected_information_gain=0.1,
    )
    right = left.model_copy(
        update={"decision_id": "b", "expected_information_gain": 0.2}
    )

    assert canonical_digest(
        left.model_dump(mode="json", exclude={"decision_id"})
    ) != canonical_digest(
        right.model_dump(mode="json", exclude={"decision_id"})
    )


def test_recovery_checkpoint_records_the_state_and_next_step(
    tmp_path,
) -> None:
    repo = _repo(tmp_path)
    snapshot = repo.load()
    repo.set_run_state(
        expected_state_revision=snapshot.state_revision,
        target=RunState.MATERIALS_COLLECTED,
    )
    snapshot = repo.load()
    recovered = repo.set_run_state(
        expected_state_revision=snapshot.state_revision,
        target=RunState.RECOVERY_REQUIRED,
        stop_reason="ModelError: model provider returned HTTP 400",
        recovery_error={
            "code": "model_bad_request",
            "message": "model provider rejected the request (HTTP 400)",
            "provider_response": "{\"error\":\"unsupported schema\"}",
            "attempts": 4,
        },
    )

    assert recovered.recovery_state is RunState.MATERIALS_COLLECTED
    assert recovered.resume_after_step == 1
    assert recovered.resume_count == 0
    assert recovered.recovery_events[-1]["code"] == "model_bad_request"
    assert recovered.recovery_events[-1]["attempts"] == 4


def test_resume_recovery_keeps_the_run_and_returns_to_the_previous_state(
    tmp_path,
) -> None:
    repo = _repo(tmp_path)
    snapshot = repo.load()
    repo.set_run_state(
        expected_state_revision=snapshot.state_revision,
        target=RunState.MATERIALS_COLLECTED,
    )
    snapshot = repo.load()
    repo.set_run_state(
        expected_state_revision=snapshot.state_revision,
        target=RunState.RECOVERY_REQUIRED,
        stop_reason="ModelError: model provider returned HTTP 400",
        recovery_error={"code": "model_bad_request", "attempts": 4},
    )

    resumed = repo.resume_from_recovery()

    assert resumed.run_id == "run-1"
    assert resumed.run_state is RunState.MATERIALS_COLLECTED
    assert resumed.stop_reason is None
    assert resumed.recovery_state is None
    assert resumed.resume_after_step is None
    assert resumed.resume_count == 1
    assert len(resumed.recovery_events) == 1


def test_resume_recovery_rejects_a_non_recovery_run(tmp_path) -> None:
    repo = _repo(tmp_path)

    with pytest.raises(ValueError, match="not waiting for recovery"):
        repo.resume_from_recovery()


def test_pending_decision_is_the_latest_uncommitted_action(tmp_path) -> None:
    repo = _repo(tmp_path)
    snapshot = repo.load()
    decision = MainAgentDecision(
        decision_id="decision.pending",
        action="retrieve_materials",
        target_subagent_role="material_retrieval",
        input_artifact_ids=[],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.5,
        action_parameters={"provider_id": "pubmed"},
    )
    repo.record_decision(
        expected_state_revision=snapshot.state_revision,
        decision=decision,
    )

    pending = repo.pending_decision()

    assert pending is not None
    assert pending.decision_id == "decision.pending"


def test_resume_stalled_keeps_the_current_state_and_records_audit(
    tmp_path,
) -> None:
    repo = _repo(tmp_path)
    snapshot = repo.load()
    repo.set_run_state(
        expected_state_revision=snapshot.state_revision,
        target=RunState.MATERIALS_COLLECTED,
    )

    resumed = repo.resume_stalled(reason="no progress for ten minutes")

    assert resumed.run_state is RunState.MATERIALS_COLLECTED
    assert resumed.resume_count == 1
    assert resumed.resume_events[-1]["reason"] == (
        "no progress for ten minutes"
    )
