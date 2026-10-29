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
    VerificationVerdict,
    canonical_digest,
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


def test_checkpoint_tampering_is_detected(tmp_path) -> None:
    repo = _repo(tmp_path)
    commit_path = max(
        (tmp_path / "objective_runtime" / "commits").glob("*.json"),
        key=lambda item: item.stem,
    )
    payload = json.loads(commit_path.read_text(encoding="utf-8"))
    payload["snapshot"]["state_revision"] += 1
    commit_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(CheckpointIntegrityError):
        repo.verify_integrity()


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
