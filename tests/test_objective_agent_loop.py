"""End-to-end main-agent decision loop producing a gated release."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medical_qa_data_factory.generation.agent_loop import (
    ObjectiveQuestionAgentLoop,
    _advance_retrieval_streaks,
    _fact_claim_artifacts,
    _question_portfolio,
    _resolve_verification_hypothesis,
    _select_verification_results,
)
from medical_qa_data_factory.generation.answer_verification_subagent import (
    BlindSolveResult,
    CounterexampleReview,
    VerificationComparisonResult,
)
from medical_qa_data_factory.generation.evidence_provider import (
    EvidenceProviderCapability,
    EvidenceProviderResult,
    ProviderTerminalStatus,
    SideEffectClass,
)
from medical_qa_data_factory.generation.fact_exploration import CandidateClaimBatch
from medical_qa_data_factory.generation.main_agent import MainAgentAction
from medical_qa_data_factory.generation.objective_question_contracts import (
    AnswerCardinality,
    AnswerEvidenceAuditResult,
    CandidateClaim,
    EvidenceAnchor,
    GenerationGoal,
    HypothesisState,
    MainAgentDecision,
    ObjectiveVerdict,
    PublicQuestionAuditResult,
    QuestionHypothesis,
    RunManifest,
    canonical_digest,
    text_digest,
)
from medical_qa_data_factory.generation.objective_question_persistence import (
    ObjectiveQuestionPersistence,
    immutable_blob_ref,
)
from medical_qa_data_factory.generation.objective_question_realization import (
    ObjectiveQuestionDraft,
    QuestionSemanticReadback,
)
from medical_qa_data_factory.generation.objective_question_runtime import (
    ArtifactEnvelope,
    ObjectiveRuntimeRepository,
    RuntimeBudget,
)
from medical_qa_data_factory.generation.question_design import (
    QuestionHypothesisBatch,
)
from medical_qa_data_factory.generation.release_review_subagent import (
    release_evidence_digest,
)
from medical_qa_data_factory.generation.subagent_contracts import (
    RoleContractViolation,
)
from medical_qa_data_factory.generation.objective_question_release import (
    export_objective_release,
)
from medical_qa_data_factory.llm.fake import FakeStructuredModel


NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
QUESTION = "On which human chromosome is BRCA1 located?"


class FakeMaterialProvider:
    def __init__(self, provider_id: str, raw_text: str) -> None:
        self.capability = EvidenceProviderCapability(
            provider_id=provider_id,
            provider_version="v1",
            side_effect_class=SideEffectClass.READ_ONLY,
            supports_idempotency=True,
            supports_status_lookup=False,
            safe_to_repeat=True,
        )
        self.raw_text = raw_text
        self.calls = 0

    def retrieve(self, request):
        self.calls += 1
        material_id = f"material.{self.capability.provider_id}"
        anchor_id = f"anchor.{self.capability.provider_id}"
        material = {
            "material_id": material_id,
            "provider_id": self.capability.provider_id,
            "provider_version": "v1",
            "source_record_id": f"record.{self.capability.provider_id}",
            "retrieved_at": NOW.isoformat(),
            "content_hash": text_digest(self.raw_text),
            "raw_content_ref": immutable_blob_ref(text_digest(self.raw_text)),
            "material_type": "text",
            "retrieval_request_id": request.request_id,
            "terminal_status": "SUCCEEDED",
        }
        quote = self.raw_text[: max(1, min(80, len(self.raw_text)))]
        anchor = {
            "anchor_id": anchor_id,
            "material_id": material_id,
            "document_id": f"document.{self.capability.provider_id}",
            "section_or_field": "description",
            "start_offset": 0,
            "end_offset": len(quote),
            "quoted_text": quote,
            "quote_hash": text_digest(quote),
        }
        return EvidenceProviderResult(
            request=request,
            terminal_status=ProviderTerminalStatus.SUCCEEDED,
            material_ids=[material_id],
            anchor_ids=[anchor_id],
            materials=[material],
            anchors=[anchor],
            raw_content_by_material_id={material_id: self.raw_text},
        )

    def lookup_status(self, operation_key: str):
        return None


class ScriptedMainAgent:
    def __init__(self, actions: list[tuple[MainAgentAction, dict]]) -> None:
        self.actions = actions
        self.index = 0

    def decide(self, snapshot):
        action, parameters = self.actions[self.index]
        self.index += 1
        role = {
            MainAgentAction.RETRIEVE_MATERIALS: "material_retrieval",
            MainAgentAction.UNDERSTAND_FACTS: "fact_understanding",
            MainAgentAction.DESIGN_QUESTIONS: "objective_question_design",
            MainAgentAction.VERIFY_ANSWER: "objective_answer_verification",
            MainAgentAction.REALIZE_QUESTION: "objective_question_realization",
            MainAgentAction.REVIEW_RELEASE: "objective_release_review",
            MainAgentAction.STOP: None,
        }[action]
        return MainAgentDecision(
            decision_id=f"decision-{self.index}",
            action=action.value,
            target_subagent_role=role,
            input_artifact_ids=parameters.get("input_artifact_ids", []),
            state_digest=canonical_digest(snapshot.model_dump(mode="json")),
            requested_budget={},
            expected_information_gain=0.5,
            action_parameters=parameters,
            stop_reason=(
                parameters.get("stop_reason") if action is MainAgentAction.STOP else None
            ),
        )


def _manifest() -> RunManifest:
    return RunManifest(
        manifest_id="manifest-loop",
        run_id="run-loop",
        orchestrator_version="objective-loop-v1",
        main_agent_version="main-agent-v1",
        subagent_role_versions={"all": "v1"},
        model_versions={"fake": "fake-model-v1"},
        schema_versions={"contracts": "objective-question-v1"},
        validation_policy_version="objective-policy-v1",
        data_snapshot_or_retrieval_time_policy="pinned",
        runtime_bundle_id="runtime-bundle-loop",
        runtime_bundle_digest="a" * 64,
    )


def _goal() -> GenerationGoal:
    return GenerationGoal(
        run_id="run-loop",
        target_entity="BRCA1",
        target_entity_id="gene:672",
        target_entity_type="gene",
        question_count_upper_bound=1,
        difficulty_targets=["easy"],
        style_targets=["concise"],
        allowed_fact_types=["gene_location"],
        run_budget={"max_actions": 12},
        run_manifest_id="manifest-loop",
        runtime_bundle_id="runtime-bundle-loop",
    )


def _claim() -> CandidateClaim:
    return CandidateClaim(
        claim_id="claim.brca1.chr17",
        subject="BRCA1",
        predicate="located_on",
        object_or_value="human chromosome 17",
        qualifiers={"species": "Homo sapiens"},
        claim_strength="directional_fact",
        polarity="POSITIVE",
        supporting_anchor_ids=["anchor.primekg"],
        contradicting_anchor_ids=[],
        extraction_model_version="fake-model-v1",
    )


def _hypothesis(
    *,
    claim_id: str = "claim.brca1.chr17",
) -> QuestionHypothesis:
    return QuestionHypothesis(
        hypothesis_id="hypothesis.brca1.chr17",
        candidate_claim_ids=[claim_id],
        question_intent=QUESTION,
        proposed_public_scope="Homo sapiens reference genome",
        proposed_answer_type="entity",
        proposed_answer_values=["chromosome 17"],
        proposed_cardinality=AnswerCardinality.EXACTLY_ONE,
        discovery_anchor_ids=["anchor.primekg"],
    )


def _model() -> FakeStructuredModel:
    def respond(request):
        role = request.role
        stage = request.payload.get("stage")
        if role == "fact_understanding":
            return {"claims": [_claim().model_dump(mode="json")], "material_attributions": [
                {"material_id": m["material_id"], "status": "matched", "identity_basis": "BRCA1 identity fixture"}
                for m in request.payload["trust_boundary"]["materials"]]}
        if role == "objective_question_design":
            claim_id = request.payload["candidate_claims"][0]["claim_id"]
            return QuestionHypothesisBatch(
                hypotheses=[_hypothesis(claim_id=claim_id)]
            )
        if role == "objective_answer_verification":
            if stage in {"blind_solve", "final_blind"}:
                return BlindSolveResult(
                    query_id=request.payload["blind_answer_query"]["query_id"],
                    answer_values=["chromosome 17"],
                    uncertain=False,
                )
            if stage == "final_review":
                return dict(question_is_self_contained=True, answer_matches_blind=True, answer_scope_complete=True,
                    assessed_difficulty="easy", explanation="The independent record places BRCA1 on chromosome 17.",
                    conflict_found=False, directly_supporting_anchor_ids=["anchor.pubmed"], support_directly_entails_answer=True)
            if stage == "compare":
                return VerificationComparisonResult(
                    answer_matches_blind=True,
                    competing_answers=[],
                    conflict_found=False,
                )
            return CounterexampleReview(
                conflict_found=False,
                directly_supporting_anchor_ids=["anchor.pubmed"],
                contradicting_anchor_ids=[],
                support_directly_entails_answer=True,
            )
        if role == "objective_question_realization":
            contract = request.payload["frozen_question_contract"]
            return ObjectiveQuestionDraft(
                draft_id="question.brca1.chr17",
                contract_id=contract["contract_id"],
                question_text=QUESTION,
                answer_values=["chromosome 17"],
                concise_explanation=(
                    "BRCA1 maps to human chromosome 17."
                ),
                semantic_readback=QuestionSemanticReadback(
                    question_intent=contract["question_intent"],
                    public_scope=contract["public_scope"],
                    answer_type=contract["answer_type"],
                    answer_values=contract["answer_values"],
                    answer_cardinality=contract["answer_cardinality"],
                    claim_strength=contract["claim_strength"],
                    required_public_conditions=(
                        contract["required_public_conditions"]
                    ),
                ),
            )
        if role == "objective_release_review":
            if stage == "public_question_audit":
                return PublicQuestionAuditResult(
                    task_id="subtask-public",
                    public_question_digest=text_digest(QUESTION),
                    self_contained=True,
                    source_independent=True,
                    asks_underlying_fact=True,
                    entity_unambiguous=True,
                    cardinality_clear=True,
                    verdict=ObjectiveVerdict.PASS,
                    primary_reason="PASS",
                    secondary_reasons=[],
                )
            contract = request.payload["frozen_question_contract"]
            evidence_digest = release_evidence_digest(
                request.payload["independent_evidence"]
            )
            return AnswerEvidenceAuditResult(
                task_id="subtask-answer",
                public_question_digest=text_digest(QUESTION),
                question_answer_contract_digest=canonical_digest(contract),
                independent_evidence_digest=evidence_digest,
                independently_supported=True,
                unique_or_closed=True,
                contradiction_checked=True,
                relation_strength_preserved=True,
                verdict=ObjectiveVerdict.PASS,
                primary_reason="PASS",
                secondary_reasons=[],
            )
        raise AssertionError(f"unexpected role/stage: {role}/{stage}")

    return FakeStructuredModel(
        responses={
            "objective_workflow_planner": lambda request: {
                "action": "retrieve_materials", "rationale": "Fixture directed search",
                "provider_id": "pubmed" if request.payload["stage"] == "verification_gap" else "primekg",
                "query": {"term": "BRCA1 independent location"} if request.payload["stage"] == "verification_gap" else {"mention": "BRCA1"},
                "evidence_need": "Independent support and alternatives for the candidate" if request.payload["stage"] == "verification_gap" else "Discover gene location facts"},
            "fact_understanding": respond,
            "objective_question_design": respond,
            "objective_candidate_review": lambda request: {"reviews": [
                {"hypothesis_id": h["hypothesis_id"], "disposition": "KEEP", "rationale": "Clear, bounded lookup supported by the fixture."}
                for h in request.payload["hypotheses"]]},
            "objective_answer_verification": respond,
            "objective_difficulty_assessment": {
                "assessed_difficulty": "easy",
                "shortest_solution_steps": ["Read the authoritative chromosome field."],
                "rationale": "A direct lookup answers this question.",
            },
            "objective_question_realization": respond,
            "objective_release_review": respond,
        }
    )


def test_agent_loop_runs_and_exports_a_public_and_audit_package(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    runtime = ObjectiveRuntimeRepository.create(
        root,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=20),
        goal=_goal(),
    )
    decisions = [
        (
            MainAgentAction.RETRIEVE_MATERIALS,
            {
                "provider_id": "primekg",
                "purpose": "discovery",
                "query": {"mention": "BRCA1"},
            },
        ),
        (
            MainAgentAction.RETRIEVE_MATERIALS,
            {
                "provider_id": "pubmed",
                "purpose": "verification",
                "query": {"term": "BRCA1 chromosome 17"},
            },
        ),
        (MainAgentAction.UNDERSTAND_FACTS, {}),
        (MainAgentAction.DESIGN_QUESTIONS, {"question_count_upper_bound": 1}),
        (
            MainAgentAction.VERIFY_ANSWER,
            {"input_artifact_ids": ["anchor.primekg"]},
        ),
        (MainAgentAction.REALIZE_QUESTION, {}),
        (MainAgentAction.REVIEW_RELEASE, {}),
        (MainAgentAction.STOP, {"stop_reason": "objective_questions_complete"}),
    ]
    loop = ObjectiveQuestionAgentLoop(
        runtime=runtime,
        persistence=ObjectiveQuestionPersistence(root),
        goal=_goal(),
        main_agent=ScriptedMainAgent(decisions),
        model=_model(),
        providers={
            "primekg": FakeMaterialProvider(
                "primekg",
                "BRCA1 is located on human chromosome 17.",
            ),
            "pubmed": FakeMaterialProvider(
                "pubmed",
                "Independent report: BRCA1 maps to chromosome 17.",
            ),
        },
    )

    result = loop.run()
    release_dir = tmp_path / "release"
    export_objective_release(result, release_dir)

    assert result.run_state.value == "COMPLETED"
    assert len(result.published_questions) == 1
    assert result.published_questions[0].validation_report.release_allowed is True
    public_rows = [
        json.loads(line)
        for line in (release_dir / "public" / "questions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert public_rows[0]["question"] == QUESTION
    assert public_rows[0]["answer"] == ["chromosome 17"]
    suggestion = public_rows[0]["suggested_retrieval"]
    hypothesis_id = result.published_questions[0].resolution.hypothesis_id
    assert suggestion == result.runtime.artifacts[hypothesis_id].payload["suggested_retrieval"]
    assert suggestion["query"] == QUESTION
    assert suggestion["stop_condition"]
    assert (release_dir / "audit" / "runtime_artifacts.jsonl").is_file()
    assert (release_dir / "audit" / "validation_reports.jsonl").is_file()


def test_exhausted_context_failure_enters_recovery_instead_of_repeating(tmp_path, monkeypatch):
    from medical_qa_data_factory.llm.errors import ModelError, ModelErrorCode
    runtime = ObjectiveRuntimeRepository.create(tmp_path, manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=20), goal=_goal())
    main = ScriptedMainAgent([(MainAgentAction.RETRIEVE_MATERIALS, {})])
    loop = ObjectiveQuestionAgentLoop(runtime=runtime, persistence=ObjectiveQuestionPersistence(tmp_path),
        goal=_goal(), main_agent=main, model=_model(), providers={})
    def fail(*args):
        raise ModelError(ModelErrorCode.BAD_REQUEST, "maximum context length", retryable=False)
    monkeypatch.setattr(loop, "_retrieve_materials", fail)
    result = loop.run()
    assert result.run_state.value == "RECOVERY_REQUIRED"
    assert result.stop_reason == "model_request_requires_repair"
    assert main.index == 1
    assert any(a.artifact_type == "subagent_failure" for a in result.runtime.artifacts.values())


def test_agent_loop_executes_the_last_uncommitted_decision_on_resume(
    tmp_path: Path,
) -> None:
    goal = _goal().model_copy(
        update={"question_count_upper_bound": 0}
    )
    runtime = ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=20),
        goal=goal,
    )
    snapshot = runtime.load()
    pending = MainAgentDecision(
        decision_id="decision.pending-resume",
        action="retrieve_materials",
        target_subagent_role="material_retrieval",
        input_artifact_ids=[],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.8,
        action_parameters={
            "provider_id": "primekg",
            "purpose": "discovery",
            "query": {"mention": "BRCA1", "native_id": "672"},
        },
    )
    runtime.record_decision(
        expected_state_revision=snapshot.state_revision,
        decision=pending,
    )
    provider = FakeMaterialProvider(
        "primekg",
        "BRCA1 is located on human chromosome 17.",
    )
    main_agent = ScriptedMainAgent(
        [
            (
                MainAgentAction.STOP,
                {"stop_reason": "resume_complete"},
            )
        ]
    )

    result = ObjectiveQuestionAgentLoop(
        runtime=runtime,
        persistence=ObjectiveQuestionPersistence(tmp_path),
        goal=goal,
        main_agent=main_agent,
        model=_model(),
        providers={"primekg": provider},
        max_actions=4,
    ).run()

    assert provider.calls == 1
    assert main_agent.index == 1
    assert result.runtime.child_slots[
        "materials.decision.pending-resume"
    ].state == "COMMITTED"


def test_fact_claims_with_conflicting_short_ids_are_versioned(
    tmp_path: Path,
) -> None:
    runtime = ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=20),
        goal=_goal(),
    )
    snapshot = runtime.load()
    existing = _claim().model_copy(
        update={
            "claim_id": "claim_001",
            "object_or_value": "existing value",
        }
    )
    snapshot = snapshot.model_copy(
        update={
            "artifacts": {
                "claim_001": ArtifactEnvelope(
                    artifact_id="claim_001",
                    artifact_type="candidate_claim",
                    payload=existing.model_dump(mode="json"),
                    producer_role="fact_understanding",
                )
            }
        }
    )
    decision = MainAgentDecision(
        decision_id="decision.fact-collision",
        action="understand_facts",
        target_subagent_role="fact_understanding",
        input_artifact_ids=[],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.8,
        action_parameters={},
    )
    incoming = existing.model_copy(
        update={"object_or_value": "new value"}
    )

    artifacts, claim_ids = _fact_claim_artifacts(
        snapshot,
        [incoming],
        decision,
    )

    assert claim_ids[0] != "claim_001"
    assert claim_ids[0].startswith("claim.")
    assert artifacts[0].artifact_id == claim_ids[0]
    assert artifacts[0].payload["claim_id"] == claim_ids[0]
    assert artifacts[0].payload["object_or_value"] == "new value"


def test_fact_claims_never_trust_model_generated_ids(
    tmp_path: Path,
) -> None:
    runtime = ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=20),
        goal=_goal(),
    )
    snapshot = runtime.load()
    decision = MainAgentDecision(
        decision_id="decision.fact-independent",
        action="understand_facts",
        target_subagent_role="fact_understanding",
        input_artifact_ids=[],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.8,
        action_parameters={},
    )
    claim = _claim().model_copy(update={"claim_id": "claim_9796_1"})

    artifacts, claim_ids = _fact_claim_artifacts(
        snapshot,
        [claim],
        decision,
    )

    assert claim_ids[0] != "claim_9796_1"
    assert claim_ids[0].startswith("claim.")
    assert artifacts[0].metadata["model_claim_id"] == "claim_9796_1"


def test_fact_claim_ids_are_semantic_stable_and_versioned(
    tmp_path: Path,
) -> None:
    runtime = ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=20),
        goal=_goal(),
    )
    snapshot = runtime.load()
    decision = MainAgentDecision(
        decision_id="decision.fact-semantic-id",
        action="understand_facts",
        target_subagent_role="fact_understanding",
        input_artifact_ids=[],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.8,
        action_parameters={},
    )
    first = _claim().model_copy(update={"claim_id": "claim_1"})
    repeated = _claim().model_copy(update={"claim_id": "claim_9796_1"})
    next_version = repeated.model_copy(
        update={"extraction_model_version": "fake-model-v2"}
    )

    first_artifacts, first_ids = _fact_claim_artifacts(
        snapshot,
        [first],
        decision,
    )
    repeated_artifacts, repeated_ids = _fact_claim_artifacts(
        snapshot,
        [repeated],
        decision,
    )
    versioned_artifacts, versioned_ids = _fact_claim_artifacts(
        snapshot,
        [next_version],
        decision,
    )

    assert first_ids == repeated_ids
    assert first_artifacts[0].payload == repeated_artifacts[0].payload
    assert first_ids != versioned_ids
    assert (
        versioned_artifacts[0].metadata["model_claim_id"]
        == "claim_9796_1"
    )


def test_provider_failures_do_not_count_as_empty_evidence() -> None:
    empty_streak = 0
    failure_streak = 0
    for _ in range(3):
        empty_streak, failure_streak = _advance_retrieval_streaks(
            ProviderTerminalStatus.UNKNOWN,
            has_materials=False,
            empty_streak=empty_streak,
            provider_failure_streak=failure_streak,
        )

    assert empty_streak == 0
    assert failure_streak == 3


def test_only_successful_empty_results_advance_no_new_evidence() -> None:
    empty_streak = 2
    failure_streak = 2

    empty_streak, failure_streak = _advance_retrieval_streaks(
        ProviderTerminalStatus.EMPTY,
        has_materials=False,
        empty_streak=empty_streak,
        provider_failure_streak=failure_streak,
    )

    assert empty_streak == 3
    assert failure_streak == 0


def test_new_materials_reset_retrieval_failure_streaks() -> None:
    empty_streak, failure_streak = _advance_retrieval_streaks(
        ProviderTerminalStatus.SUCCEEDED,
        has_materials=True,
        empty_streak=2,
        provider_failure_streak=2,
    )

    assert empty_streak == 0
    assert failure_streak == 0


def test_verification_honors_the_declared_hypothesis(tmp_path: Path) -> None:
    runtime = ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=20),
        goal=_goal(),
    )
    first = _hypothesis(claim_id="claim.first").model_copy(
        update={"hypothesis_id": "hypothesis.first"}
    )
    second = _hypothesis(claim_id="claim.second").model_copy(
        update={"hypothesis_id": "hypothesis.second"}
    )
    snapshot = runtime.load().model_copy(
        update={
            "hypotheses": {
                first.hypothesis_id: HypothesisState.PROPOSED,
                second.hypothesis_id: HypothesisState.PROPOSED,
            }
        }
    )
    decision = MainAgentDecision(
        decision_id="decision.verify-declared",
        action="verify_answer",
        target_subagent_role="objective_answer_verification",
        input_artifact_ids=[],
        state_digest=canonical_digest(snapshot.model_dump(mode="json")),
        requested_budget={},
        expected_information_gain=0.8,
        action_parameters={"hypothesis_id": first.hypothesis_id},
    )

    selected = _resolve_verification_hypothesis(
        decision,
        hypotheses=[second, first],
        resolved_hypothesis_ids=set(),
    )

    assert selected.hypothesis_id == first.hypothesis_id


def test_verification_requires_a_target_when_several_are_active() -> None:
    first = _hypothesis(claim_id="claim.first").model_copy(
        update={"hypothesis_id": "hypothesis.first"}
    )
    second = _hypothesis(claim_id="claim.second").model_copy(
        update={"hypothesis_id": "hypothesis.second"}
    )
    decision = MainAgentDecision(
        decision_id="decision.verify-missing-target",
        action="verify_answer",
        target_subagent_role="objective_answer_verification",
        input_artifact_ids=[],
        state_digest="0" * 64,
        requested_budget={},
        expected_information_gain=0.8,
        action_parameters={},
    )

    with pytest.raises(
        RoleContractViolation,
        match="verification requires exactly one hypothesis target",
    ):
        _resolve_verification_hypothesis(
            decision,
            hypotheses=[first, second],
            resolved_hypothesis_ids=set(),
        )


def test_verification_evidence_is_bound_to_the_hypothesis() -> None:
    first_anchor = EvidenceAnchor(
        anchor_id="anchor.first",
        material_id="material.first",
        document_id="document.first",
        section_or_field="abstract",
        start_offset=0,
        end_offset=5,
        quoted_text="first",
        quote_hash=text_digest("first"),
    )
    second_anchor = EvidenceAnchor(
        anchor_id="anchor.second",
        material_id="material.second",
        document_id="document.second",
        section_or_field="abstract",
        start_offset=0,
        end_offset=6,
        quoted_text="second",
        quote_hash=text_digest("second"),
    )
    first_claim = _claim().model_copy(
        update={
            "claim_id": "claim.first",
            "supporting_anchor_ids": [first_anchor.anchor_id],
        }
    )
    second_claim = _claim().model_copy(
        update={
            "claim_id": "claim.second",
            "supporting_anchor_ids": [second_anchor.anchor_id],
        }
    )
    hypothesis = _hypothesis(claim_id=first_claim.claim_id)
    decision = MainAgentDecision(
        decision_id="decision.evidence-binding",
        action="verify_answer",
        target_subagent_role="objective_answer_verification",
        input_artifact_ids=[],
        state_digest="0" * 64,
        requested_budget={},
        expected_information_gain=0.8,
        action_parameters={"hypothesis_id": hypothesis.hypothesis_id},
    )
    first_result = ArtifactEnvelope(
        artifact_id="provider_result.first",
        artifact_type="provider_result",
        payload={
            "terminal_status": "SUCCEEDED",
            "material_ids": [first_anchor.material_id],
            "anchor_ids": [first_anchor.anchor_id],
        },
        metadata={"purpose": "verification"},
    )
    second_result = ArtifactEnvelope(
        artifact_id="provider_result.second",
        artifact_type="provider_result",
        payload={
            "terminal_status": "SUCCEEDED",
            "material_ids": [second_anchor.material_id],
            "anchor_ids": [second_anchor.anchor_id],
        },
        metadata={"purpose": "verification"},
    )

    selected = _select_verification_results(
        decision,
        hypothesis=hypothesis,
        claims=[first_claim, second_claim],
        anchors=[first_anchor, second_anchor],
        provider_results=[second_result, first_result],
    )

    assert [item.artifact_id for item in selected] == [
        "provider_result.first"
    ]


def test_verification_never_falls_back_to_unrelated_evidence() -> None:
    anchor = EvidenceAnchor(
        anchor_id="anchor.target",
        material_id="material.target",
        document_id="document.target",
        section_or_field="abstract",
        start_offset=0,
        end_offset=6,
        quoted_text="target",
        quote_hash=text_digest("target"),
    )
    claim = _claim().model_copy(
        update={
            "claim_id": "claim.target",
            "supporting_anchor_ids": [anchor.anchor_id],
        }
    )
    hypothesis = _hypothesis(claim_id=claim.claim_id)
    decision = MainAgentDecision(
        decision_id="decision.no-unrelated-fallback",
        action="verify_answer",
        target_subagent_role="objective_answer_verification",
        input_artifact_ids=[],
        state_digest="0" * 64,
        requested_budget={},
        expected_information_gain=0.8,
        action_parameters={"hypothesis_id": hypothesis.hypothesis_id},
    )
    unrelated = ArtifactEnvelope(
        artifact_id="provider_result.unrelated",
        artifact_type="provider_result",
        payload={
            "terminal_status": "SUCCEEDED",
            "material_ids": ["material.unrelated"],
            "anchor_ids": ["anchor.unrelated"],
        },
        metadata={"purpose": "verification"},
    )

    with pytest.raises(
        RoleContractViolation,
        match="no target-specific verification evidence",
    ):
        _select_verification_results(
            decision,
            hypothesis=hypothesis,
            claims=[claim],
            anchors=[anchor],
            provider_results=[unrelated],
        )


def test_rejected_claim_sets_are_not_reproposed_as_new(tmp_path: Path) -> None:
    runtime = ObjectiveRuntimeRepository.create(
        tmp_path,
        manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=20),
        goal=_goal(),
    )
    claim = _claim()
    hypothesis = _hypothesis(claim_id=claim.claim_id)
    snapshot = runtime.load().model_copy(
        update={
            "hypotheses": {
                hypothesis.hypothesis_id: HypothesisState.REJECTED
            }
        }
    )

    portfolio = _question_portfolio(
        snapshot,
        [hypothesis],
        [claim],
        [],
    )

    assert claim.claim_id in portfolio["used_claim_ids"]
    assert claim.claim_id not in portfolio["preferred_claim_ids"]


def test_user_pause_before_decision_is_resumable(tmp_path):
    runtime = ObjectiveRuntimeRepository.create(tmp_path, manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=20), goal=_goal())
    main = ScriptedMainAgent([])
    runtime.request_pause()
    loop = ObjectiveQuestionAgentLoop(runtime=runtime, persistence=ObjectiveQuestionPersistence(tmp_path),
        goal=_goal(), main_agent=main, model=_model(), providers={})
    result = loop.run()
    assert result.stop_reason == "user_paused"
    assert result.run_state.value == "RECOVERY_REQUIRED"
    assert main.index == 0
    assert not runtime.pause_requested
    resumed = runtime.resume_from_recovery()
    assert resumed.run_state.value == "CREATED"
    assert resumed.stop_reason is None


def test_pause_during_action_stops_even_at_action_budget(tmp_path, monkeypatch):
    runtime = ObjectiveRuntimeRepository.create(tmp_path, manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=20), goal=_goal())
    main = ScriptedMainAgent([(MainAgentAction.RETRIEVE_MATERIALS, {})])
    loop = ObjectiveQuestionAgentLoop(runtime=runtime, persistence=ObjectiveQuestionPersistence(tmp_path),
        goal=_goal(), main_agent=main, model=_model(), providers={}, max_actions=1)
    monkeypatch.setattr(loop, "_retrieve_materials", lambda *args: runtime.request_pause())
    result = loop.run()
    assert result.stop_reason == "user_paused"
    assert main.index == 1
    assert len(result.runtime.decisions) == 1


def test_usability_rejection_is_persisted_before_verification(tmp_path):
    runtime = ObjectiveRuntimeRepository.create(tmp_path, manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=20), goal=_goal())
    model = _model()
    model.set_response("objective_candidate_review", lambda request: {"reviews": [
        {"hypothesis_id": h["hypothesis_id"], "disposition": "REJECT", "rationale": "Unusable candidate in this test"}
        for h in request.payload["hypotheses"]]})
    loop = ObjectiveQuestionAgentLoop(runtime=runtime, persistence=ObjectiveQuestionPersistence(tmp_path),
        goal=_goal(), main_agent=ScriptedMainAgent([
            (MainAgentAction.RETRIEVE_MATERIALS, {"provider_id": "primekg", "purpose": "discovery", "query": {"mention": "BRCA1"}}),
            (MainAgentAction.UNDERSTAND_FACTS, {}), (MainAgentAction.DESIGN_QUESTIONS, {})]),
        model=model, providers={"primekg": FakeMaterialProvider("primekg", "BRCA1 is located on human chromosome 17.")},
        max_actions=3)
    result = loop.run()
    assert result.runtime.hypotheses
    assert all(state is HypothesisState.REJECTED for state in result.runtime.hypotheses.values())
    assert not model.calls_for("objective_answer_verification")
    assert any(a.artifact_type == "candidate_usability_review" for a in result.runtime.artifacts.values())
