"""Main-agent decisions are bounded by the deterministic dispatcher."""
from __future__ import annotations

import json

import pytest

from medical_qa_data_factory.generation.main_agent import (
    IllegalMainAgentAction,
    MainAgentDecisionProposal,
    MainAgentAction,
    StructuredMainAgent,
    _allowed_actions,
    validate_main_agent_decision,
)
from medical_qa_data_factory.generation.objective_question_contracts import (
    DomainOutcome,
    ExecutionStatus,
    GenerationGoal,
    HypothesisState,
    MainAgentDecision,
    RunState,
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


def _claims_ready_snapshot(tmp_path, *, has_claim=True):
    snapshot = _repo(tmp_path).load()
    artifacts = {
        "material.1": ArtifactEnvelope(artifact_id="material.1", artifact_type="evidence_material",
                                       payload={"material_id": "material.1"}),
        "anchor.1": ArtifactEnvelope(artifact_id="anchor.1", artifact_type="evidence_anchor",
                                     payload={"material_id": "material.1"}),
        "batch.1": ArtifactEnvelope(artifact_id="batch.1", artifact_type="fact_understanding_batch",
                                    payload={"material_ids": ["material.1"]}),
    }
    if has_claim:
        artifacts["claim.1"] = ArtifactEnvelope(artifact_id="claim.1", artifact_type="candidate_claim",
                                                 payload={"claim_id": "claim.1"})
    goal = GenerationGoal(
        run_id=snapshot.run_id, target_entity="PNMA1", target_entity_id="9240",
        target_entity_type="gene", question_count_upper_bound=1,
        difficulty_targets=["easy"], style_targets=["concise"], allowed_fact_types=["classification"],
        run_budget={"max_actions": 8}, run_manifest_id="manifest-run-main",
        runtime_bundle_id="runtime-bundle-v1",
    )
    return snapshot.model_copy(update={"goal": goal, "run_state": RunState.CLAIMS_PROPOSED,
                                       "artifacts": artifacts})


def _repeated_understanding_proposal():
    return MainAgentDecisionProposal(action="understand_facts", target_subagent_role="fact_understanding",
        input_artifact_ids=["material.1"], requested_budget={}, expected_information_gain=0.5,
        action_parameters={"stale_parameter": "must not survive recovery"})


def test_repeated_understanding_advances_processed_claims_to_design(tmp_path):
    snapshot = _claims_ready_snapshot(tmp_path)
    model = FakeStructuredModel(responses={"objective_main_agent": _repeated_understanding_proposal()})
    decision = StructuredMainAgent(model, provider_ids=["pubmed"]).decide(snapshot)
    assert decision.action == "design_questions"
    assert decision.target_subagent_role == "objective_question_design"
    assert decision.input_artifact_ids == ["claim.1"]
    assert "stale_parameter" not in decision.action_parameters
    assert decision.action_parameters["decision_recovery"]["original_action"] == "understand_facts"
    assert len(model.invocations) == 3
    validate_main_agent_decision(snapshot, decision)
    assert model.invocations[0].payload["fact_processing"]["unprocessed_material_ids"] == []


def test_main_agent_accepts_a_valid_third_attempt(tmp_path):
    snapshot = _claims_ready_snapshot(tmp_path)
    proposals = iter([_repeated_understanding_proposal(), _repeated_understanding_proposal(),
        MainAgentDecisionProposal(action="design_questions", expected_information_gain=0.5)])
    model = FakeStructuredModel(responses={"objective_main_agent": lambda request: next(proposals)})
    decision = StructuredMainAgent(model, provider_ids=["pubmed"]).decide(snapshot)
    assert decision.action == "design_questions"
    assert "decision_recovery" not in decision.action_parameters
    assert len(model.invocations) == 3


def test_repeated_understanding_cannot_design_without_claims(tmp_path):
    snapshot = _claims_ready_snapshot(tmp_path, has_claim=False)
    model = FakeStructuredModel(responses={"objective_main_agent": _repeated_understanding_proposal()})
    with pytest.raises(IllegalMainAgentAction, match="UNDERSTAND_FACTS"):
        StructuredMainAgent(model, provider_ids=["pubmed"]).decide(snapshot)
    assert len(model.invocations) == 3


def test_new_material_in_claims_proposed_still_runs_understanding(tmp_path):
    snapshot = _claims_ready_snapshot(tmp_path)
    snapshot = snapshot.model_copy(update={"artifacts": {
        **snapshot.artifacts,
        "material.new": ArtifactEnvelope(artifact_id="material.new", artifact_type="evidence_material",
                                         payload={"material_id": "material.new"}),
        "anchor.new": ArtifactEnvelope(artifact_id="anchor.new", artifact_type="evidence_anchor",
                                       payload={"material_id": "material.new"}),
    }})
    model = FakeStructuredModel(responses={"objective_main_agent": _repeated_understanding_proposal()})
    decision = StructuredMainAgent(model, provider_ids=["pubmed"]).decide(snapshot)
    assert decision.action == "understand_facts"
    assert "decision_recovery" not in decision.action_parameters
    assert len(model.invocations) == 1
    assert model.invocations[0].payload["fact_processing"]["unprocessed_material_ids"] == ["material.new"]


@pytest.mark.parametrize("phase,expected", [
    ("needs_evidence", "request_more_evidence"),
    ("can_verify", "verify_answer"),
    ("verified", "realize_question"),
    ("drafted", "review_release"),
    ("published", "stop"),
])
def test_processed_facts_recovery_continues_active_candidate(tmp_path, phase, expected):
    snapshot = _claims_ready_snapshot(tmp_path)
    artifacts = dict(snapshot.artifacts)
    artifacts["hypothesis.active"] = ArtifactEnvelope(
        artifact_id="hypothesis.active", artifact_type="question_hypothesis",
        payload={"hypothesis_id": "hypothesis.active", "question_intent": "PNMA1 属于哪个家族？",
                 "proposed_answer_values": ["hidden-answer-must-not-be-a-query"]})
    state = HypothesisState.PROPOSED
    if phase == "can_verify":
        artifacts["provider_result.1"] = ArtifactEnvelope(
            artifact_id="provider_result.1", artifact_type="provider_result",
            payload={"terminal_status": "SUCCEEDED"}, metadata={"purpose": "verification"})
    if phase in {"verified", "drafted"}:
        state = HypothesisState.ANSWER_VERIFIED
        artifacts["resolution.active"] = ArtifactEnvelope(
            artifact_id="resolution.active", artifact_type="answer_resolution",
            payload={"resolution_id": "resolution.active", "hypothesis_id": "hypothesis.active"})
        artifacts["contract.active"] = ArtifactEnvelope(
            artifact_id="contract.active", artifact_type="frozen_question_contract",
            payload={"contract_id": "contract.active", "resolution_id": "resolution.active"})
    if phase == "drafted":
        state = HypothesisState.QUESTION_REALIZED
        artifacts["draft.active"] = ArtifactEnvelope(
            artifact_id="draft.active", artifact_type="objective_question_draft",
            payload={"draft_id": "draft.active", "contract_id": "contract.active"})
    if phase == "published":
        state = HypothesisState.PUBLISHED
    snapshot = snapshot.model_copy(update={"run_state": RunState.CANDIDATES_PROCESSING,
        "artifacts": artifacts, "hypotheses": {"hypothesis.active": state}})
    model = FakeStructuredModel(responses={"objective_main_agent": _repeated_understanding_proposal()})
    decision = StructuredMainAgent(model, provider_ids=["pubmed"]).decide(snapshot)
    assert decision.action == expected
    assert decision.action_parameters["decision_recovery"]["original_action"] == "understand_facts"
    assert "stale_parameter" not in decision.action_parameters
    validate_main_agent_decision(snapshot, decision)
    if phase == "needs_evidence":
        assert decision.action_parameters["purpose"] == "verification"
        assert decision.action_parameters["verification_focus"] == "comprehensive"
        assert decision.action_parameters["hypothesis_id"] == "hypothesis.active"
        assert decision.action_parameters["provider_id"] == "pubmed"
        assert decision.action_parameters["query"] == {"term": "PNMA1"}
        assert "hidden-answer" not in json.dumps(decision.model_dump())


def test_main_agent_response_schema_lists_only_available_actions(tmp_path):
    snapshot = _claims_ready_snapshot(tmp_path)
    class InspectingModel(FakeStructuredModel):
        def generate_structured(self, request, response_model):
            choices = response_model.model_json_schema()["properties"]["action"]["enum"]
            assert set(choices) == {item["action"] for item in request.payload["available_actions"]}
            assert "understand_facts" not in choices
            return super().generate_structured(request, response_model)
    model = InspectingModel(responses={"objective_main_agent":
        MainAgentDecisionProposal(action="design_questions", expected_information_gain=0.5)})
    assert StructuredMainAgent(model, provider_ids=["pubmed"]).decide(snapshot).action == "design_questions"


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


def test_structured_agent_owns_the_subagent_role_mapping(tmp_path) -> None:
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
    model = FakeStructuredModel(
        responses={
            "objective_main_agent": MainAgentDecisionProposal(
                action="request_more_evidence",
                target_subagent_role="objective_question_design",
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0.5,
                action_parameters={
                    "provider_id": "pubmed",
                    "purpose": "verification",
                    "query": {"term": "BRCA1"},
                },
            )
        }
    )

    decision = StructuredMainAgent(
        model,
        provider_ids=["pubmed"],
    ).decide(repo.load())

    assert decision.target_subagent_role == "material_retrieval"
    assert len(model.payloads_for("objective_main_agent")) == 1


def test_main_agent_receives_bounded_material_content_summary(
    tmp_path,
) -> None:
    repo = ObjectiveRuntimeRepository.create(
        tmp_path / "content-summary",
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
            allowed_fact_types=["observation_association"],
            run_budget={"max_actions": 8},
            run_manifest_id="manifest-run-main",
            runtime_bundle_id="runtime-bundle-v1",
        ),
    )
    snapshot = repo.load().model_copy(
        update={
            "artifacts": {
                "provider.pubmed": ArtifactEnvelope(
                    artifact_id="provider.pubmed",
                    artifact_type="provider_result",
                    payload={
                        "terminal_status": "SUCCEEDED",
                        "request": {"query": {"term": "ZRSR2 splicing"}},
                        "material_ids": ["material.pubmed.123"],
                        "materials": [
                            {
                                "material_id": "material.pubmed.123",
                                "provider_id": "pubmed",
                                "material_type": "literature_abstract",
                                "source_record_id": "pmid:123",
                            }
                        ],
                        "raw_content_by_material_id": {
                            "material.pubmed.123": (
                                "ZRSR2 loss impaired minor intron splicing."
                            )
                        },
                        "usage": {
                            "search_rationale": "direct mechanism evidence"
                        },
                    },
                )
            },
        }
    )
    model = FakeStructuredModel(
        responses={
            "objective_main_agent": MainAgentDecisionProposal(
                action="retrieve_materials",
                target_subagent_role="material_retrieval",
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0.5,
                action_parameters={
                    "provider_id": "pubmed",
                    "purpose": "discovery",
                    "query": {"term": "ZRSR2 splicing"},
                },
            )
        }
    )

    StructuredMainAgent(
        model,
        provider_ids=["pubmed"],
    ).decide(snapshot)

    payload = model.payloads_for("objective_main_agent")[0]
    summary = payload["artifact_summaries"][0]["content_summary"]
    assert summary["material_previews"][0]["material_id"] == (
        "material.pubmed.123"
    )
    assert "minor intron splicing" in (
        summary["material_previews"][0]["preview"]
    )


def test_fact_understanding_remains_available_for_new_materials(
    tmp_path,
) -> None:
    snapshot = _repo(tmp_path).load().model_copy(
        update={
            "artifacts": {
                "material.old": ArtifactEnvelope(
                    artifact_id="material.old",
                    artifact_type="evidence_material",
                    payload={"material_id": "material.old"},
                ),
                "anchor.old": ArtifactEnvelope(
                    artifact_id="anchor.old",
                    artifact_type="evidence_anchor",
                    payload={"anchor_id": "anchor.old"},
                ),
                "claim.old": ArtifactEnvelope(
                    artifact_id="claim.old",
                    artifact_type="candidate_claim",
                    payload={"claim_id": "claim.old"},
                ),
                "fact-batch.old": ArtifactEnvelope(
                    artifact_id="fact-batch.old",
                    artifact_type="fact_understanding_batch",
                    payload={"material_ids": ["material.old"]},
                ),
                "material.new": ArtifactEnvelope(
                    artifact_id="material.new",
                    artifact_type="evidence_material",
                    payload={"material_id": "material.new"},
                ),
                "anchor.new": ArtifactEnvelope(
                    artifact_id="anchor.new",
                    artifact_type="evidence_anchor",
                    payload={"anchor_id": "anchor.new"},
                ),
            },
        }
    )

    assert MainAgentAction.UNDERSTAND_FACTS in _allowed_actions(snapshot)
    assert MainAgentAction.DESIGN_QUESTIONS not in _allowed_actions(snapshot)


def test_duplicate_material_preserves_discovery_and_verification_purpose(
    tmp_path,
) -> None:
    repo = _repo(tmp_path)
    payload = {
        "material_id": "material.gene",
        "provider_id": "ncbi_gene",
        "source_record_id": "gene:1",
        "content_hash": "a" * 64,
    }
    discovery = ArtifactEnvelope(
        artifact_id="material.gene",
        artifact_type="evidence_material",
        payload=payload,
        metadata={"purpose": "discovery"},
    )
    verification = ArtifactEnvelope(
        artifact_id="material.gene",
        artifact_type="evidence_material",
        payload=payload,
        metadata={"purpose": "verification"},
    )
    snapshot = repo.load().model_copy(
        update={"artifacts": {"material.gene": discovery}}
    )

    merged = ObjectiveRuntimeRepository._merge_artifacts(
        snapshot,
        [verification],
    )

    assert merged["material.gene"].metadata["purposes"] == [
        "discovery",
        "verification",
    ]


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
        match="STOP is premature",
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
            "goal": GenerationGoal(
                run_id="run-main",
                target_entity="KIF15",
                target_entity_id="56992",
                target_entity_type="gene",
                question_count_upper_bound=3,
                difficulty_targets=["easy"],
                style_targets=["concise"],
                allowed_fact_types=["entity_identity", "classification"],
                run_budget={"max_actions": 24},
                run_manifest_id="manifest-run-main",
                runtime_bundle_id="runtime-bundle-v1",
            ),
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
    stop = MainAgentDecision(
        decision_id="decision-stop-before-realization",
        action="stop",
        target_subagent_role=None,
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.0,
        stop_reason="Requested upper bound reached",
    )
    with pytest.raises(
        IllegalMainAgentAction,
        match="required target",
    ):
        validate_main_agent_decision(snapshot, stop)


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


def test_published_question_does_not_block_designing_unused_claims(
    tmp_path,
) -> None:
    snapshot = _repo(tmp_path).load()
    snapshot = snapshot.model_copy(
        update={
            "goal": GenerationGoal(
                run_id="run-main",
                target_entity="KIF15",
                target_entity_id="56992",
                target_entity_type="gene",
                question_count_upper_bound=3,
                difficulty_targets=["easy"],
                style_targets=["concise"],
                allowed_fact_types=[
                    "entity_identity",
                    "classification",
                    "gene_location",
                ],
                run_budget={"max_actions": 24},
                run_manifest_id="manifest-run-main",
                runtime_bundle_id="runtime-bundle-v1",
            ),
            "hypotheses": {
                "hypothesis.published": HypothesisState.PUBLISHED,
            },
            "artifacts": {
                "claim.used": ArtifactEnvelope(
                    artifact_id="claim.used",
                    artifact_type="candidate_claim",
                    payload={"claim_id": "claim.used"},
                ),
                "claim.unused": ArtifactEnvelope(
                    artifact_id="claim.unused",
                    artifact_type="candidate_claim",
                    payload={"claim_id": "claim.unused"},
                ),
                "hypothesis.published": ArtifactEnvelope(
                    artifact_id="hypothesis.published",
                    artifact_type="question_hypothesis",
                    payload={
                        "hypothesis_id": "hypothesis.published",
                        "candidate_claim_ids": ["claim.used"],
                    },
                ),
            },
            "decisions": {
                "decision.first-design": MainAgentDecision(
                    decision_id="decision.first-design",
                    action="design_questions",
                    target_subagent_role="objective_question_design",
                    state_digest="a" * 64,
                    requested_budget={},
                    expected_information_gain=0.5,
                )
            },
        }
    )
    design = MainAgentDecision(
        decision_id="decision-more-design",
        action="design_questions",
        target_subagent_role="objective_question_design",
        input_artifact_ids=["claim.unused"],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.7,
    )
    stop = MainAgentDecision(
        decision_id="decision-premature-stop",
        action="stop",
        target_subagent_role=None,
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.0,
        stop_reason="no objective candidate remains",
    )

    validate_main_agent_decision(snapshot, design)
    with pytest.raises(IllegalMainAgentAction, match="STOP is premature"):
        validate_main_agent_decision(snapshot, stop)


def test_target_count_blocks_stop_after_rejected_design_retries(
    tmp_path,
) -> None:
    snapshot = _repo(tmp_path).load().model_copy(
        update={
            "goal": GenerationGoal(
                run_id="run-main",
                target_entity="PHYHIP",
                target_entity_id="9796",
                target_entity_type="gene",
                question_count_upper_bound=3,
                difficulty_targets=["easy"],
                style_targets=["concise"],
                allowed_fact_types=["entity_identity"],
                run_budget={"max_actions": 100},
                run_manifest_id="manifest-run-main",
                runtime_bundle_id="runtime-bundle-v1",
            ),
            "hypotheses": {
                "hypothesis.one": HypothesisState.PUBLISHED,
                "hypothesis.two": HypothesisState.PUBLISHED,
                "hypothesis.rejected": HypothesisState.REJECTED,
            },
            "artifacts": {
                "claim.identity": ArtifactEnvelope(
                    artifact_id="claim.identity",
                    artifact_type="candidate_claim",
                    payload={"claim_id": "claim.identity"},
                ),
                "hypothesis.rejected": ArtifactEnvelope(
                    artifact_id="hypothesis.rejected",
                    artifact_type="question_hypothesis",
                    payload={
                        "hypothesis_id": "hypothesis.rejected",
                        "candidate_claim_ids": ["claim.identity"],
                    },
                ),
            },
            "decisions": {
                f"decision.design.{index}": MainAgentDecision(
                    decision_id=f"decision.design.{index}",
                    action="design_questions",
                    target_subagent_role="objective_question_design",
                    state_digest=str(index) * 64,
                    requested_budget={},
                    expected_information_gain=0.5,
                )
                for index in range(1, 4)
            },
        }
    )
    design = MainAgentDecision(
        decision_id="decision.design.replacement",
        action="design_questions",
        target_subagent_role="objective_question_design",
        input_artifact_ids=["claim.identity"],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.5,
    )
    stop = MainAgentDecision(
        decision_id="decision.stop.short",
        action="stop",
        target_subagent_role=None,
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.0,
        stop_reason="no objective candidate remains",
    )

    assert MainAgentAction.DESIGN_QUESTIONS in _allowed_actions(snapshot)
    validate_main_agent_decision(snapshot, design)
    with pytest.raises(IllegalMainAgentAction, match="required target"):
        validate_main_agent_decision(snapshot, stop)


def test_main_agent_payload_is_bounded_for_large_runs(tmp_path) -> None:
    artifacts = {}
    for index in range(35):
        material_id = f"material.pubmed.{index}"
        artifacts[f"provider.{index}"] = ArtifactEnvelope(
            artifact_id=f"provider.{index}",
            artifact_type="provider_result",
            payload={
                "terminal_status": "SUCCEEDED",
                "request": {"query": {"term": f"PHYHIP query {index}"}},
                "material_ids": [material_id],
                "materials": [
                    {
                        "material_id": material_id,
                        "provider_id": "pubmed",
                        "material_type": "literature_abstract",
                        "source_record_id": f"pmid:{index}",
                    }
                ],
                "raw_content_by_material_id": {
                    material_id: "long evidence " * 800
                },
                "usage": {
                    "search_rationale": "long search rationale " * 800,
                    "search_trace": [
                        {
                            "query": {"term": f"query {round_no}"},
                            "terminal_status": "SUCCEEDED",
                            "material_count": 1,
                        }
                        for round_no in range(8)
                    ],
                },
            },
        )
    snapshot = _repo(tmp_path).load().model_copy(
        update={
            "artifacts": artifacts,
            "goal": GenerationGoal(
                run_id="run-main",
                target_entity="PHYHIP",
                target_entity_id="9796",
                target_entity_type="gene",
                question_count_upper_bound=3,
                difficulty_targets=["easy"],
                style_targets=["concise"],
                allowed_fact_types=["entity_identity"],
                run_budget={"max_actions": 20},
                run_manifest_id="manifest-run-main",
                runtime_bundle_id="runtime-bundle-v1",
            ),
        }
    )
    model = FakeStructuredModel(
        responses={
            "objective_main_agent": MainAgentDecisionProposal(
                action="retrieve_materials",
                target_subagent_role="material_retrieval",
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0.5,
                action_parameters={
                    "provider_id": "pubmed",
                    "purpose": "discovery",
                    "query": {"term": "PHYHIP"},
                },
            )
        }
    )

    StructuredMainAgent(model, provider_ids=["pubmed"]).decide(snapshot)
    payload = model.payloads_for("objective_main_agent")[0]

    assert len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    ) < 80_000
    assert len(payload["retrieval_history"]) <= 12
    assert (
        sum(
            item["artifact_type"] == "provider_result"
            for item in payload["artifact_summaries"]
        )
        <= 20
    )
