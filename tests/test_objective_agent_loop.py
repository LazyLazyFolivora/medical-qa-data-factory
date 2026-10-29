"""End-to-end main-agent decision loop producing a gated release."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from medical_qa_data_factory.generation.agent_loop import (
    ObjectiveQuestionAgentLoop,
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
    GenerationGoal,
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
    ObjectiveRuntimeRepository,
    RuntimeBudget,
)
from medical_qa_data_factory.generation.question_design import (
    QuestionHypothesisBatch,
)
from medical_qa_data_factory.generation.release_review_subagent import (
    release_evidence_digest,
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
            input_artifact_ids=[],
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


def _hypothesis() -> QuestionHypothesis:
    return QuestionHypothesis(
        hypothesis_id="hypothesis.brca1.chr17",
        candidate_claim_ids=["claim.brca1.chr17"],
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
            return CandidateClaimBatch(claims=[_claim()])
        if role == "objective_question_design":
            return QuestionHypothesisBatch(hypotheses=[_hypothesis()])
        if role == "objective_answer_verification":
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
            return CounterexampleReview(
                conflict_found=False,
                contradicting_anchor_ids=[],
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
            "fact_understanding": respond,
            "objective_question_design": respond,
            "objective_answer_verification": respond,
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
        (MainAgentAction.VERIFY_ANSWER, {}),
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
    assert (release_dir / "audit" / "runtime_artifacts.jsonl").is_file()
    assert (release_dir / "audit" / "validation_reports.jsonl").is_file()
