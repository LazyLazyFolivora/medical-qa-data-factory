"""Main-agent decisions are bounded by the deterministic dispatcher."""
from __future__ import annotations

import pytest

from medical_qa_data_factory.generation.main_agent import (
    IllegalMainAgentAction,
    MainAgentDecisionProposal,
    MainAgentAction,
    StructuredMainAgent,
    validate_main_agent_decision,
)
from medical_qa_data_factory.generation.objective_question_contracts import (
    DomainOutcome,
    ExecutionStatus,
    GenerationGoal,
    HypothesisState,
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
from medical_qa_data_factory.llm.fake import FakeStructuredModel


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


def test_structured_agent_fills_a_concise_pubmed_query(tmp_path) -> None:
    repo = ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=10),
        goal=GenerationGoal(
            run_id="run-main",
            target_entity="BRCA1",
            target_entity_id="672",
            target_entity_type="gene",
            question_count_upper_bound=1,
            difficulty_targets=["easy"],
            style_targets=["concise"],
            allowed_fact_types=["gene_location"],
            run_budget={"max_actions": 8},
            run_manifest_id="manifest-run-main",
            runtime_bundle_id="runtime-bundle-v1",
        ),
    )
    snapshot = repo.load()
    model = FakeStructuredModel(
        responses={
            "objective_main_agent": MainAgentDecisionProposal(
                action="request_more_evidence",
                target_subagent_role="material_retrieval",
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0.5,
                action_parameters={
                    "purpose": "verification",
                    "focus": (
                        "Assess all proposed hypotheses and identify every "
                        "independent verification requirement"
                    ),
                },
            )
        }
    )
    decision = StructuredMainAgent(
        model,
        provider_ids=["primekg", "pubmed"],
    ).decide(snapshot)

    assert decision.action_parameters["provider_id"] == "pubmed"
    assert decision.action_parameters["query"] == {"term": "BRCA1"}


def test_gene_location_route_prefers_hgnc_then_ncbi_gene(tmp_path) -> None:
    repo = ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=10),
        goal=GenerationGoal(
            run_id="run-main",
            target_entity="ZRSR2",
            target_entity_id="8233",
            target_entity_type="gene",
            question_count_upper_bound=1,
            difficulty_targets=["easy"],
            style_targets=["concise"],
            allowed_fact_types=["gene_location"],
            run_budget={"max_actions": 8},
            run_manifest_id="manifest-run-main",
            runtime_bundle_id="runtime-bundle-v1",
        ),
    )
    snapshot = repo.load()
    proposals = iter(
        [
            MainAgentDecisionProposal(
                action="request_more_evidence",
                target_subagent_role="material_retrieval",
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0.5,
                action_parameters={
                    "purpose": "discovery",
                    "query": {
                        "primekg": {
                            "mention": "ZRSR2",
                            "native_id": "8233",
                        }
                    },
                },
            ),
            MainAgentDecisionProposal(
                action="request_more_evidence",
                target_subagent_role="material_retrieval",
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0.5,
                action_parameters={
                    "purpose": "verification",
                    "query": {
                        "primekg": {
                            "mention": "ZRSR2",
                            "native_id": "8233",
                        }
                    },
                },
            ),
        ]
    )
    model = FakeStructuredModel(
        responses={"objective_main_agent": lambda request: next(proposals)}
    )
    agent = StructuredMainAgent(
        model,
        provider_ids=[
            "hgnc",
            "ncbi_gene",
            "ensembl",
            "primekg",
            "pubmed",
        ],
    )

    discovery = agent.decide(snapshot)
    verification = agent.decide(snapshot)

    assert discovery.action_parameters["provider_id"] == "hgnc"
    assert discovery.action_parameters["query"]["symbol"] == "ZRSR2"
    assert verification.action_parameters["provider_id"] == "ncbi_gene"
    assert verification.action_parameters["query"]["symbol"] == "ZRSR2"


def test_rejected_hypothesis_allows_designing_a_replacement(tmp_path) -> None:
    snapshot = _repo(tmp_path).load()
    snapshot = snapshot.model_copy(
        update={
            "goal": GenerationGoal(
                run_id="run-main",
                target_entity="KIF15",
                target_entity_id="56992",
                target_entity_type="gene",
                question_count_upper_bound=1,
                difficulty_targets=["easy"],
                style_targets=["concise"],
                allowed_fact_types=[
                    "entity_identity",
                    "classification",
                    "gene_location",
                ],
                run_budget={"max_actions": 8},
                run_manifest_id="manifest-run-main",
                runtime_bundle_id="runtime-bundle-v1",
            ),
            "hypotheses": {
                "hypothesis.rejected": HypothesisState.REJECTED,
            },
            "artifacts": {
                "claim.kif15": ArtifactEnvelope(
                    artifact_id="claim.kif15",
                    artifact_type="candidate_claim",
                    payload={"claim_id": "claim.kif15"},
                ),
                "hypothesis.rejected": ArtifactEnvelope(
                    artifact_id="hypothesis.rejected",
                    artifact_type="question_hypothesis",
                    payload={"hypothesis_id": "hypothesis.rejected"},
                ),
            },
        }
    )
    decision = MainAgentDecision(
        decision_id="decision-redesign",
        action="design_questions",
        target_subagent_role="objective_question_design",
        input_artifact_ids=["claim.kif15"],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.7,
    )

    validate_main_agent_decision(snapshot, decision)
    stop = MainAgentDecision(
        decision_id="decision-stop",
        action="stop",
        target_subagent_role=None,
        input_artifact_ids=[],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.0,
        stop_reason="no candidate",
    )
    with pytest.raises(
        IllegalMainAgentAction,
        match="replacement design is still required",
    ):
        validate_main_agent_decision(snapshot, stop)


def test_structured_main_agent_retries_a_premature_stop(tmp_path) -> None:
    snapshot = _repo(tmp_path).load()
    snapshot = snapshot.model_copy(
        update={
            "goal": GenerationGoal(
                run_id="run-main",
                target_entity="KIF15",
                target_entity_id="56992",
                target_entity_type="gene",
                question_count_upper_bound=1,
                difficulty_targets=["easy"],
                style_targets=["concise"],
                allowed_fact_types=[
                    "entity_identity",
                    "classification",
                    "gene_location",
                ],
                run_budget={"max_actions": 8},
                run_manifest_id="manifest-run-main",
                runtime_bundle_id="runtime-bundle-v1",
            ),
            "hypotheses": {
                "hypothesis.rejected": HypothesisState.REJECTED,
            },
            "artifacts": {
                "claim.kif15": ArtifactEnvelope(
                    artifact_id="claim.kif15",
                    artifact_type="candidate_claim",
                    payload={"claim_id": "claim.kif15"},
                ),
                "hypothesis.rejected": ArtifactEnvelope(
                    artifact_id="hypothesis.rejected",
                    artifact_type="question_hypothesis",
                    payload={"hypothesis_id": "hypothesis.rejected"},
                    parent_decision_id="decision-first-design",
                ),
            },
        }
    )
    proposals = iter(
        [
            MainAgentDecisionProposal(
                action="stop",
                target_subagent_role=None,
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0.0,
                action_parameters={},
                stop_reason="no candidate",
            ),
            MainAgentDecisionProposal(
                action="design_questions",
                target_subagent_role="objective_question_design",
                input_artifact_ids=["claim.kif15"],
                requested_budget={},
                expected_information_gain=0.7,
                action_parameters={},
            ),
        ]
    )
    model = FakeStructuredModel(
        responses={"objective_main_agent": lambda request: next(proposals)}
    )

    decision = StructuredMainAgent(
        model,
        provider_ids=["hgnc", "ncbi_gene"],
    ).decide(snapshot)

    assert decision.action == "design_questions"


def test_old_resolution_does_not_block_new_hypothesis_verification(
    tmp_path,
) -> None:
    snapshot = _repo(tmp_path).load()
    snapshot = snapshot.model_copy(
        update={
            "hypotheses": {
                "hypothesis.old": HypothesisState.REJECTED,
                "hypothesis.new": HypothesisState.PROPOSED,
            },
            "artifacts": {
                "hypothesis.old": ArtifactEnvelope(
                    artifact_id="hypothesis.old",
                    artifact_type="question_hypothesis",
                    payload={"hypothesis_id": "hypothesis.old"},
                ),
                "hypothesis.new": ArtifactEnvelope(
                    artifact_id="hypothesis.new",
                    artifact_type="question_hypothesis",
                    payload={"hypothesis_id": "hypothesis.new"},
                ),
                "resolution.old": ArtifactEnvelope(
                    artifact_id="resolution.old",
                    artifact_type="answer_resolution",
                    payload={
                        "resolution_id": "resolution.old",
                        "hypothesis_id": "hypothesis.old",
                    },
                ),
                "provider.verification": ArtifactEnvelope(
                    artifact_id="provider.verification",
                    artifact_type="provider_result",
                    payload={"terminal_status": "SUCCEEDED"},
                    metadata={"purpose": "verification"},
                ),
            },
        }
    )
    decision = MainAgentDecision(
        decision_id="decision-verify-new",
        action="verify_answer",
        target_subagent_role="objective_answer_verification",
        input_artifact_ids=["hypothesis.new"],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.7,
    )

    validate_main_agent_decision(snapshot, decision)


def test_realization_selects_the_active_contract_with_historical_drafts(
    tmp_path,
) -> None:
    snapshot = _repo(tmp_path).load()
    snapshot = snapshot.model_copy(
        update={
            "hypotheses": {
                "hypothesis.old": HypothesisState.PUBLISHED,
                "hypothesis.new": HypothesisState.ANSWER_VERIFIED,
            },
            "artifacts": {
                "resolution.old": ArtifactEnvelope(
                    artifact_id="resolution.old",
                    artifact_type="answer_resolution",
                    payload={
                        "resolution_id": "resolution.old",
                        "hypothesis_id": "hypothesis.old",
                    },
                ),
                "contract.old": ArtifactEnvelope(
                    artifact_id="contract.old",
                    artifact_type="frozen_question_contract",
                    payload={
                        "contract_id": "contract.old",
                        "resolution_id": "resolution.old",
                    },
                ),
                "draft.old": ArtifactEnvelope(
                    artifact_id="draft.old",
                    artifact_type="objective_question_draft",
                    payload={
                        "draft_id": "draft.old",
                        "contract_id": "contract.old",
                    },
                ),
                "resolution.new": ArtifactEnvelope(
                    artifact_id="resolution.new",
                    artifact_type="answer_resolution",
                    payload={
                        "resolution_id": "resolution.new",
                        "hypothesis_id": "hypothesis.new",
                    },
                ),
                "contract.new": ArtifactEnvelope(
                    artifact_id="contract.new",
                    artifact_type="frozen_question_contract",
                    payload={
                        "contract_id": "contract.new",
                        "resolution_id": "resolution.new",
                    },
                ),
            },
        }
    )
    decision = MainAgentDecision(
        decision_id="decision-realize-new",
        action="realize_question",
        target_subagent_role="objective_question_realization",
        input_artifact_ids=["contract.new"],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.7,
    )

    validate_main_agent_decision(snapshot, decision)


def test_release_review_selects_the_unreviewed_active_draft(tmp_path) -> None:
    snapshot = _repo(tmp_path).load()
    snapshot = snapshot.model_copy(
        update={
            "hypotheses": {
                "hypothesis.old": HypothesisState.PUBLISHED,
                "hypothesis.new": HypothesisState.QUESTION_REALIZED,
            },
            "artifacts": {
                "resolution.old": ArtifactEnvelope(
                    artifact_id="resolution.old",
                    artifact_type="answer_resolution",
                    payload={
                        "resolution_id": "resolution.old",
                        "hypothesis_id": "hypothesis.old",
                    },
                ),
                "contract.old": ArtifactEnvelope(
                    artifact_id="contract.old",
                    artifact_type="frozen_question_contract",
                    payload={
                        "contract_id": "contract.old",
                        "resolution_id": "resolution.old",
                    },
                ),
                "draft.old": ArtifactEnvelope(
                    artifact_id="draft.old",
                    artifact_type="objective_question_draft",
                    payload={
                        "draft_id": "draft.old",
                        "contract_id": "contract.old",
                    },
                ),
                "report.old": ArtifactEnvelope(
                    artifact_id="report.old",
                    artifact_type="objective_validation_report",
                    payload={
                        "report_id": "report.old",
                        "question_id": "draft.old",
                    },
                ),
                "resolution.new": ArtifactEnvelope(
                    artifact_id="resolution.new",
                    artifact_type="answer_resolution",
                    payload={
                        "resolution_id": "resolution.new",
                        "hypothesis_id": "hypothesis.new",
                    },
                ),
                "contract.new": ArtifactEnvelope(
                    artifact_id="contract.new",
                    artifact_type="frozen_question_contract",
                    payload={
                        "contract_id": "contract.new",
                        "resolution_id": "resolution.new",
                    },
                ),
                "draft.new": ArtifactEnvelope(
                    artifact_id="draft.new",
                    artifact_type="objective_question_draft",
                    payload={
                        "draft_id": "draft.new",
                        "contract_id": "contract.new",
                    },
                ),
            },
        }
    )
    decision = MainAgentDecision(
        decision_id="decision-review-new",
        action="review_release",
        target_subagent_role="objective_release_review",
        input_artifact_ids=["draft.new"],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.7,
    )

    validate_main_agent_decision(snapshot, decision)
