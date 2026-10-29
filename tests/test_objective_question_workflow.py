"""Deterministic state ownership for the objective-question workflow."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from medical_qa_data_factory.generation.objective_question_contracts import (
    DomainOutcome,
    ExecutionStatus,
    HypothesisState,
    RunManifest,
    SubagentResult,
    VerificationVerdict,
)
from medical_qa_data_factory.generation.objective_question_runtime import (
    ArtifactEnvelope,
    ObjectiveRuntimeRepository,
    RuntimeBudget,
    StaleStateRevision,
)
from medical_qa_data_factory.generation.subagent_contracts import SubagentRole
from medical_qa_data_factory.generation.agent_loop import (
    ObjectiveQuestionAgentLoop,
)
from medical_qa_data_factory.generation.objective_question_persistence import (
    ObjectiveQuestionPersistence,
)
from medical_qa_data_factory.generation.objective_question_contracts import (
    GenerationGoal,
    MainAgentDecision,
    canonical_digest,
)
from medical_qa_data_factory.generation.evidence_provider import (
    EvidenceProviderCapability,
    EvidenceProviderResult,
    ProviderTerminalStatus,
)
from test_objective_agent_loop import FakeMaterialProvider


def _manifest() -> RunManifest:
    return RunManifest(
        manifest_id="manifest-run-1",
        run_id="run-1",
        orchestrator_version="objective-orchestrator-v1",
        main_agent_version="main-agent-v1",
        subagent_role_versions={
            "fact_understanding": "v1",
            "objective_question_design": "v1",
        },
        model_versions={"mock": "v1"},
        schema_versions={"objective_question_contracts": "objective-question-v1"},
        validation_policy_version="objective-policy-v1",
        data_snapshot_or_retrieval_time_policy="pinned-per-run",
        runtime_bundle_id="runtime-bundle-v1",
        runtime_bundle_digest="a" * 64,
    )


def _result(subtask_id: str) -> SubagentResult:
    return SubagentResult(
        subtask_id=subtask_id,
        execution_status=ExecutionStatus.SUCCEEDED,
        domain_outcome=DomainOutcome.SUPPORTED,
        verdict=VerificationVerdict.NOT_EMITTED,
        output_artifact_ids=[f"artifact.{subtask_id}"],
        evidence_need_ids=[],
        usage={"provider_calls": 1},
        error_category=None,
        stop_reason="complete",
    )


def _artifact(artifact_id: str) -> ArtifactEnvelope:
    return ArtifactEnvelope(
        artifact_id=artifact_id,
        artifact_type="candidate_claim",
        payload={"artifact_id": artifact_id, "status": "PROPOSED"},
    )


def test_initial_runtime_checkpoint_has_one_atomic_sequence(tmp_path) -> None:
    repo = ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10.0, max_model_calls=20),
    )
    snapshot = repo.load()
    checkpoint = repo.checkpoint()

    assert snapshot.state_revision == 1
    assert snapshot.commit_sequence == 1
    assert checkpoint.state_revision == snapshot.state_revision
    assert checkpoint.last_committed_sequence == snapshot.commit_sequence
    assert repo.verify_integrity() is True


def test_stale_state_revision_is_rejected(tmp_path) -> None:
    repo = ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10.0, max_model_calls=20),
    )
    initial = repo.load()
    repo.set_hypothesis_state(
        expected_state_revision=initial.state_revision,
        hypothesis_id="hypothesis-1",
        target=HypothesisState.PROPOSED,
    )

    with pytest.raises(StaleStateRevision):
        repo.set_hypothesis_state(
            expected_state_revision=initial.state_revision,
            hypothesis_id="hypothesis-2",
            target=HypothesisState.PROPOSED,
        )


def test_child_slots_accept_results_after_run_revision_moves(tmp_path) -> None:
    repo = ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10.0, max_model_calls=20),
    )
    initial = repo.load()
    repo.register_child_slot(
        expected_state_revision=initial.state_revision,
        slot_id="slot-a",
        parent_decision_id="decision-parent",
        subtask_revision=0,
    )
    current = repo.load()
    repo.register_child_slot(
        expected_state_revision=current.state_revision,
        slot_id="slot-b",
        parent_decision_id="decision-parent",
        subtask_revision=0,
    )

    def commit(slot_id: str) -> None:
        repo.commit_child_result(
            slot_id=slot_id,
            expected_subtask_revision=0,
            result=_result(f"subtask-{slot_id}"),
            artifacts=[_artifact(f"artifact.subtask-{slot_id}")],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(commit, ["slot-a", "slot-b"]))

    snapshot = repo.load()
    assert snapshot.child_slots["slot-a"].state == "COMMITTED"
    assert snapshot.child_slots["slot-b"].state == "COMMITTED"
    first = repo.aggregate_child_results(
        expected_state_revision=snapshot.state_revision,
        parent_decision_id="decision-parent",
    )
    second = repo.aggregate_child_results(
        expected_state_revision=snapshot.state_revision,
        parent_decision_id="decision-parent",
    )
    assert first == second
    assert repo.load().aggregation_commits["decision-parent"] == first


def test_empty_candidate_set_completes_with_zero_questions(tmp_path) -> None:
    repo = ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10.0, max_model_calls=20),
    )
    snapshot = repo.load()
    updated = repo.complete_without_hypotheses(
        expected_state_revision=snapshot.state_revision,
        stop_reason="no_objective_fact_candidates",
    )

    assert updated.run_state.value == "COMPLETED_WITH_ZERO_QUESTIONS"
    assert updated.stop_reason == "no_objective_fact_candidates"


def test_runtime_role_defaults_cover_all_subagent_roles() -> None:
    assert {role.value for role in SubagentRole} >= {
        "material_retrieval",
        "fact_understanding",
        "objective_question_design",
        "objective_question_realization",
        "objective_answer_verification",
        "objective_release_review",
    }


def test_agent_loop_stops_after_three_retrievals_without_new_material(
    tmp_path,
) -> None:
    goal = GenerationGoal(
        run_id="run-1",
        target_entity="ZRSR2",
        target_entity_id="8233",
        target_entity_type="gene",
        question_count_upper_bound=1,
        difficulty_targets=["easy"],
        style_targets=["concise"],
        allowed_fact_types=["gene_location"],
        run_budget={"max_actions": 8},
        run_manifest_id="manifest-run-1",
        runtime_bundle_id="runtime-bundle-v1",
    )
    repo = ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10.0, max_model_calls=20),
        goal=goal,
    )

    class EmptyProvider:
        capability = EvidenceProviderCapability.read_only(provider_id="pubmed")

        def retrieve(self, request):
            return EvidenceProviderResult(
                request=request,
                terminal_status=ProviderTerminalStatus.EMPTY,
                material_ids=[],
                anchor_ids=[],
            )

        def lookup_status(self, operation_key):
            return None

    class AlwaysRetrieve:
        def decide(self, snapshot):
            return MainAgentDecision(
                decision_id=f"decision-{len(snapshot.decisions) + 1}",
                action="request_more_evidence",
                target_subagent_role="material_retrieval",
                input_artifact_ids=[],
                state_digest=canonical_digest(
                    snapshot.model_dump(mode="json")
                ),
                requested_budget={},
                expected_information_gain=0.5,
                action_parameters={
                    "provider_id": "pubmed",
                    "purpose": "verification",
                    "query": {"term": "ZRSR2"},
                },
            )

    result = ObjectiveQuestionAgentLoop(
        runtime=repo,
        persistence=ObjectiveQuestionPersistence(tmp_path),
        goal=goal,
        main_agent=AlwaysRetrieve(),
        model=object(),
        providers={"pubmed": EmptyProvider()},
        max_actions=8,
    ).run()

    assert result.run_state.value == "COMPLETED_WITH_ZERO_QUESTIONS"
    assert result.stop_reason == "no_new_evidence"
    assert len(result.runtime.decisions) == 3
