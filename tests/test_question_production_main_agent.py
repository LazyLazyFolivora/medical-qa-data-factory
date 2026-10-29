"""Main-agent decisions are bounded by the deterministic dispatcher."""
from __future__ import annotations

import pytest

from medical_qa_data_factory.generation.main_agent import (
    IllegalMainAgentAction,
    MainAgentAction,
    validate_main_agent_decision,
)
from medical_qa_data_factory.generation.objective_question_contracts import (
    DomainOutcome,
    ExecutionStatus,
    MainAgentDecision,
    RunManifest,
    SubagentResult,
    VerificationVerdict,
    canonical_digest,
)
from medical_qa_data_factory.generation.objective_question_runtime import (
    ArtifactEnvelope,
    ObjectiveRuntimeRepository,
    RuntimeBudget,
)
from medical_qa_data_factory.generation.subagent_contracts import (
    SubagentRole,
    build_objective_subagent_task,
    default_objective_role_registry,
)
from medical_qa_data_factory.generation.subagent_dispatch import (
    DispatchOutcome,
    SubagentDispatcher,
)


def _manifest() -> RunManifest:
    return RunManifest(
        manifest_id="manifest-run-main",
        run_id="run-main",
        orchestrator_version="orchestrator-v1",
        main_agent_version="main-agent-v1",
        subagent_role_versions={"material_retrieval": "v1"},
        model_versions={"mock": "v1"},
        schema_versions={"contracts": "objective-question-v1"},
        validation_policy_version="policy-v1",
        data_snapshot_or_retrieval_time_policy="pinned",
        runtime_bundle_id="runtime-bundle-v1",
        runtime_bundle_digest="a" * 64,
    )


def _repo(tmp_path) -> ObjectiveRuntimeRepository:
    return ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=10),
    )


def _decision(
    *,
    snapshot,
    action: str = "retrieve_materials",
    role: str | None = "material_retrieval",
) -> MainAgentDecision:
    return MainAgentDecision(
        decision_id="decision-1",
        action=action,
        target_subagent_role=role,
        input_artifact_ids=[],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={"provider_calls": 1},
        expected_information_gain=0.5,
    )


def test_created_run_can_only_start_material_retrieval_or_stop(tmp_path) -> None:
    snapshot = _repo(tmp_path).load()
    validate_main_agent_decision(snapshot, _decision(snapshot=snapshot))

    with pytest.raises(IllegalMainAgentAction, match="VERIFY_ANSWER"):
        validate_main_agent_decision(
            snapshot,
            _decision(
                snapshot=snapshot,
                action=MainAgentAction.VERIFY_ANSWER.value,
                role="objective_answer_verification",
            ),
        )


def test_main_agent_cannot_publish_or_target_the_wrong_role(tmp_path) -> None:
    snapshot = _repo(tmp_path).load()
    with pytest.raises(IllegalMainAgentAction, match="publish"):
        validate_main_agent_decision(
            snapshot,
            _decision(snapshot=snapshot, action="publish"),
        )
    with pytest.raises(IllegalMainAgentAction, match="target_subagent_role"):
        validate_main_agent_decision(
            snapshot,
            _decision(
                snapshot=snapshot,
                role="objective_release_review",
            ),
        )


def test_finished_run_rejects_every_new_action(tmp_path) -> None:
    repo = _repo(tmp_path)
    current = repo.load()
    finished = repo.complete_without_hypotheses(
        expected_state_revision=current.state_revision,
        stop_reason="no_facts",
    )

    with pytest.raises(IllegalMainAgentAction, match="terminal"):
        validate_main_agent_decision(
            finished,
            _decision(snapshot=finished),
        )


def test_dispatcher_rejects_unregistered_or_cross_role_calls() -> None:
    dispatcher = SubagentDispatcher()
    capability = default_objective_role_registry()[
        SubagentRole.MATERIAL_RETRIEVAL
    ]
    task = build_objective_subagent_task(
        task_id="subtask-materials",
        role=SubagentRole.MATERIAL_RETRIEVAL,
        capability=capability,
        goal={
            "target_entity": "BRCA1",
            "allowed_providers": ["pubmed"],
        },
        input_artifact_ids=[],
    )

    with pytest.raises(KeyError, match="material_retrieval"):
        dispatcher.dispatch(task, payload={})

    def handler(request, payload):
        return DispatchOutcome(
            result=SubagentResult(
                subtask_id=request.subtask_id,
                execution_status=ExecutionStatus.SUCCEEDED,
                domain_outcome=DomainOutcome.SUPPORTED,
                verdict=VerificationVerdict.NOT_EMITTED,
                output_artifact_ids=["material-1"],
                evidence_need_ids=[],
                usage={},
                error_category=None,
                stop_reason="materials_collected",
            ),
            artifacts=[
                ArtifactEnvelope(
                    artifact_id="material-1",
                    artifact_type="evidence_material",
                    payload={"material_id": "material-1"},
                )
            ],
        )

    dispatcher.register(SubagentRole.MATERIAL_RETRIEVAL, handler)
    assert dispatcher.dispatch(task, payload={"query": "BRCA1"}).result.subtask_id == (
        "subtask-materials"
    )

    wrong_task = task.model_copy(
        update={"subagent_role": SubagentRole.FACT_UNDERSTANDING.value}
    )
    with pytest.raises(KeyError, match="fact_understanding"):
        dispatcher.dispatch(wrong_task, payload={})
