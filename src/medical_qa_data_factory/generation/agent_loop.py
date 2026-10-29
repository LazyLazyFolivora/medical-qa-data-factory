"""Main-agent decision loop for the objective-question pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .answer_verification_subagent import (
    AnswerVerificationSubagent,
    VerificationEvidenceBundle,
    build_blind_answer_query,
)
from .evidence_provider import (
    EvidenceProviderPort,
    EvidenceProviderResult,
    ProviderTerminalStatus,
)
from .fact_exploration import FactUnderstandingSubagent
from .main_agent import (
    MainAgentAction,
    validate_main_agent_decision,
)
from .material_retrieval_subagent import MaterialRetrievalSubagent
from .objective_question_contracts import (
    AnswerCardinality,
    AnswerResolution,
    AnswerStatus,
    CandidateClaim,
    CompletenessBasis,
    CoverageStatus,
    EntityResolution,
    EntityResolutionStatus,
    EvidenceAnchor,
    EvidenceMaterial,
    FrozenQuestionContract,
    GenerationGoal,
    HypothesisState,
    MainAgentDecision,
    ObjectiveValidationReport,
    ObjectiveVerdict,
    QuestionHypothesis,
    RunState,
    VerificationAttempt,
    VerificationCoverageReport,
    VerificationPlan,
    canonical_digest,
)
from .objective_question_persistence import ObjectiveQuestionPersistence
from .objective_question_realization import (
    ObjectiveQuestionDraft,
    ObjectiveQuestionRealizationSubagent,
)
from .objective_question_runtime import (
    ArtifactEnvelope,
    ObjectiveRuntimeRepository,
    ObjectiveRuntimeSnapshot,
    SubagentLoopGuard,
)
from .question_design import ObjectiveQuestionDesignSubagent
from .release_review_subagent import (
    AnswerEvidenceAuditPackage,
    IndependentEvidenceAuditPackage,
    PublicQuestionPackage,
    ReleaseReviewSubagent,
)
from .subagent_contracts import (
    SubagentRole,
    build_objective_subagent_task,
    default_objective_role_registry,
)


class MainAgentPort(Protocol):
    def decide(
        self, snapshot: ObjectiveRuntimeSnapshot
    ) -> MainAgentDecision: ...


@dataclass(frozen=True)
class PublishedObjectiveQuestion:
    draft: ObjectiveQuestionDraft
    frozen_contract: FrozenQuestionContract
    resolution: AnswerResolution
    validation_report: ObjectiveValidationReport


@dataclass(frozen=True)
class ObjectiveAgentLoopResult:
    run_id: str
    run_state: RunState
    stop_reason: str | None
    published_questions: list[PublishedObjectiveQuestion]
    runtime: ObjectiveRuntimeSnapshot


class ObjectiveQuestionAgentLoop:
    """A bounded observe-decide-dispatch-commit loop."""

    def __init__(
        self,
        *,
        runtime: ObjectiveRuntimeRepository,
        persistence: ObjectiveQuestionPersistence,
        goal: GenerationGoal,
        main_agent: MainAgentPort,
        model: Any,
        providers: Mapping[str, EvidenceProviderPort],
        max_actions: int = 20,
    ) -> None:
        self.runtime = runtime
        self.persistence = persistence
        self.goal = goal
        self.main_agent = main_agent
        self.model = model
        self.providers = dict(providers)
        self.max_actions = max_actions
        self.loop_guard = SubagentLoopGuard(max_repeated_decisions=3)

    def run(self) -> ObjectiveAgentLoopResult:
        stop_reason: str | None = None
        for _ in range(self.max_actions):
            snapshot = self.runtime.load()
            decision = self.main_agent.decide(snapshot)
            validate_main_agent_decision(snapshot, decision)
            self.loop_guard.observe(decision)
            snapshot = self.runtime.record_decision(
                expected_state_revision=snapshot.state_revision,
                decision=decision,
            )
            action = MainAgentAction(decision.action)
            if action is MainAgentAction.STOP:
                stop_reason = decision.stop_reason or "main_agent_stop"
                self._finish(stop_reason)
                break
            if action in {
                MainAgentAction.RETRIEVE_MATERIALS,
                MainAgentAction.REQUEST_MORE_EVIDENCE,
            }:
                self._retrieve_materials(snapshot, decision)
            elif action is MainAgentAction.UNDERSTAND_FACTS:
                self._understand_facts(snapshot, decision)
            elif action is MainAgentAction.DESIGN_QUESTIONS:
                self._design_questions(snapshot, decision)
            elif action is MainAgentAction.VERIFY_ANSWER:
                self._verify_answer(snapshot, decision)
            elif action is MainAgentAction.REALIZE_QUESTION:
                self._realize_question(snapshot, decision)
            elif action is MainAgentAction.REVIEW_RELEASE:
                self._review_release(snapshot, decision)
            else:  # pragma: no cover - enum and validator close this set
                raise AssertionError(action)
        else:
            stop_reason = "max_agent_actions"
            self._finish(stop_reason)

        runtime = self.runtime.load()
        return ObjectiveAgentLoopResult(
            run_id=runtime.run_id,
            run_state=runtime.run_state,
            stop_reason=runtime.stop_reason,
            published_questions=self._published_questions(runtime),
            runtime=runtime,
        )

    # ---- action implementations ---------------------------------------

    def _retrieve_materials(
        self,
        snapshot: ObjectiveRuntimeSnapshot,
        decision: MainAgentDecision,
    ) -> None:
        provider_id = str(
            decision.action_parameters.get("provider_id", "")
        )
        provider = self.providers.get(provider_id)
        if provider is None:
            raise ValueError(
                f"main agent selected unavailable provider {provider_id!r}"
            )
        purpose = str(
            decision.action_parameters.get("purpose", "discovery")
        )
        capability = default_objective_role_registry()[
            SubagentRole.MATERIAL_RETRIEVAL
        ]
        task = build_objective_subagent_task(
            task_id=f"materials.{decision.decision_id}",
            role=SubagentRole.MATERIAL_RETRIEVAL,
            capability=capability,
            goal={
                "target_entity": self.goal.target_entity,
                "evidence_need": purpose,
                "allowed_providers": [provider_id],
                "material_limit": 10,
            },
            input_artifact_ids=[],
            allowed_tools=[provider_id],
        )
        outcome = MaterialRetrievalSubagent(self.persistence).run(
            task,
            provider=provider,
            query=dict(decision.action_parameters.get("query", {})),
        )
        artifacts = [
            _artifact(
                item,
                "evidence_material",
                decision,
                SubagentRole.MATERIAL_RETRIEVAL,
                metadata={"purpose": purpose},
            )
            for item in outcome.materials
        ]
        artifacts.extend(
            _artifact(
                item,
                "evidence_anchor",
                decision,
                SubagentRole.MATERIAL_RETRIEVAL,
                metadata={"purpose": purpose},
            )
            for item in outcome.anchors
        )
        artifacts.append(
            ArtifactEnvelope(
                artifact_id=f"provider_result.{decision.decision_id}",
                artifact_type="provider_result",
                payload=outcome.provider_result.model_dump(mode="json"),
                producer_role=SubagentRole.MATERIAL_RETRIEVAL.value,
                parent_decision_id=decision.decision_id,
                metadata={"purpose": purpose},
            )
        )
        self._commit_child(
            snapshot,
            decision,
            task.subtask_id,
            outcome.result,
            artifacts,
        )
        latest = self.runtime.load()
        if latest.run_state is RunState.CREATED and outcome.materials:
            self.runtime.set_run_state(
                expected_state_revision=latest.state_revision,
                target=RunState.MATERIALS_COLLECTED,
            )

    def _understand_facts(
        self,
        snapshot: ObjectiveRuntimeSnapshot,
        decision: MainAgentDecision,
    ) -> None:
        materials = self._models(snapshot, "evidence_material", EvidenceMaterial)
        anchors = self._models(snapshot, "evidence_anchor", EvidenceAnchor)
        if not materials or not anchors:
            raise ValueError("fact understanding requires persisted materials")
        resolution = EntityResolution(
            resolution_id=f"entity_resolution.{decision.decision_id}",
            mention=self.goal.target_entity,
            namespace="objective-question",
            canonical_id=self.goal.target_entity_id or self.goal.target_entity,
            canonical_name=self.goal.target_entity,
            species="Homo sapiens",
            authority="task-scoped target",
            resolution_status=EntityResolutionStatus.RESOLVED,
        )
        capability = default_objective_role_registry()[
            SubagentRole.FACT_UNDERSTANDING
        ]
        task = build_objective_subagent_task(
            task_id=f"facts.{decision.decision_id}",
            role=SubagentRole.FACT_UNDERSTANDING,
            capability=capability,
            goal={
                "target_entity": self.goal.target_entity,
                "allowed_fact_types": self.goal.allowed_fact_types,
                "materials": [item.material_id for item in materials],
                "anchors": [item.anchor_id for item in anchors],
                "entity_resolutions": [resolution.resolution_id],
            },
            input_artifact_ids=[
                *[item.material_id for item in materials],
                *[item.anchor_id for item in anchors],
                resolution.resolution_id,
            ],
        )
        outcome = FactUnderstandingSubagent(self.model).run(
            task,
            materials=materials,
            anchors=anchors,
            entity_resolutions=[resolution],
            raw_content_by_material_id={
                item.material_id: self.persistence.load_raw_content(item).decode(
                    "utf-8"
                )
                for item in materials
            },
        )
        artifacts = [
            _artifact(
                claim,
                "candidate_claim",
                decision,
                SubagentRole.FACT_UNDERSTANDING,
            )
            for claim in outcome.claims
        ]
        artifacts.append(
            _artifact(
                resolution,
                "entity_resolution",
                decision,
                SubagentRole.FACT_UNDERSTANDING,
            )
        )
        self._commit_child(
            snapshot,
            decision,
            task.subtask_id,
            outcome.result,
            artifacts,
        )
        latest = self.runtime.load()
        self.runtime.set_run_state(
            expected_state_revision=latest.state_revision,
            target=RunState.CLAIMS_PROPOSED,
        )

    def _design_questions(
        self,
        snapshot: ObjectiveRuntimeSnapshot,
        decision: MainAgentDecision,
    ) -> None:
        claims = self._models(snapshot, "candidate_claim", CandidateClaim)
        anchors = self._models(snapshot, "evidence_anchor", EvidenceAnchor)
        capability = default_objective_role_registry()[
            SubagentRole.QUESTION_DESIGN
        ]
        task = build_objective_subagent_task(
            task_id=f"questions.{decision.decision_id}",
            role=SubagentRole.QUESTION_DESIGN,
            capability=capability,
            goal={
                "target_entity": self.goal.target_entity,
                "allowed_fact_types": self.goal.allowed_fact_types,
                "question_count_upper_bound": (
                    self.goal.question_count_upper_bound
                ),
                "difficulty_targets": self.goal.difficulty_targets,
                "style_targets": self.goal.style_targets,
                "candidate_claims": [claim.claim_id for claim in claims],
            },
            input_artifact_ids=[claim.claim_id for claim in claims],
        )
        outcome = ObjectiveQuestionDesignSubagent(self.model).run(
            task,
            claims=claims,
            anchors=anchors,
        )
        artifacts = [
            _artifact(
                hypothesis,
                "question_hypothesis",
                decision,
                SubagentRole.QUESTION_DESIGN,
            )
            for hypothesis in outcome.hypotheses
        ]
        self._commit_child(
            snapshot,
            decision,
            task.subtask_id,
            outcome.result,
            artifacts,
        )
        latest = self.runtime.load()
        latest = self.runtime.set_run_state(
            expected_state_revision=latest.state_revision,
            target=RunState.HYPOTHESES_PROPOSED,
        )
        for hypothesis in outcome.hypotheses:
            latest = self.runtime.set_hypothesis_state(
                expected_state_revision=latest.state_revision,
                hypothesis_id=hypothesis.hypothesis_id,
                target=HypothesisState.PROPOSED,
            )

    def _verify_answer(
        self,
        snapshot: ObjectiveRuntimeSnapshot,
        decision: MainAgentDecision,
    ) -> None:
        hypotheses = self._models(
            snapshot, "question_hypothesis", QuestionHypothesis
        )
        claims = self._models(snapshot, "candidate_claim", CandidateClaim)
        if not hypotheses:
            raise ValueError("answer verification requires a hypothesis")
        hypothesis = hypotheses[0]
        verification_results = [
            artifact
            for artifact in snapshot.artifacts.values()
            if artifact.artifact_type == "provider_result"
            and artifact.metadata.get("purpose") == "verification"
        ]
        if not verification_results:
            raise ValueError(
                "answer verification requires independent provider evidence"
            )
        query = build_blind_answer_query(hypothesis, claims)
        provider_results = [
            EvidenceProviderResult.model_validate(artifact.payload)
            for artifact in verification_results
        ]
        attempt_ids = [
            f"verification.{artifact.artifact_id}"
            for artifact in verification_results
        ]
        plans = VerificationPlan(
            plan_id=f"verification_plan.{hypothesis.hypothesis_id}",
            hypothesis_id=hypothesis.hypothesis_id,
            blind_answer_query_digest=query.input_digest(),
            expected_attempt_ids=attempt_ids,
            allowed_provider_ids=sorted(
                {item.request.provider_id for item in provider_results}
            ),
            competing_answer_strategy="search independent records",
            contradiction_strategy="inspect conflicting evidence",
            budget={"max_provider_attempts": len(provider_results)},
            stop_conditions=["all_attempts_terminal"],
        )
        attempts: list[VerificationAttempt] = []
        for attempt_id, provider_result in zip(
            attempt_ids, provider_results, strict=True
        ):
            attempts.append(
                VerificationAttempt(
                    attempt_id=attempt_id,
                    plan_id=plans.plan_id,
                    query=str(provider_result.request.query),
                    provider_id=provider_result.request.provider_id,
                    started_at=provider_result.request.requested_at,
                    finished_at=provider_result.request.requested_at,
                    terminal_status=provider_result.terminal_status,
                    error_category=provider_result.error_category,
                    material_ids=provider_result.material_ids,
                    anchor_ids=provider_result.anchor_ids,
                )
            )
        all_terminal = all(
            item.terminal_status is not None for item in attempts
        )
        successful = all(
            item.terminal_status is ProviderTerminalStatus.SUCCEEDED
            for item in attempts
        )
        coverage = VerificationCoverageReport(
            coverage_report_id=f"coverage.{hypothesis.hypothesis_id}",
            expected_attempt_ids=attempt_ids,
            terminal_attempt_ids=attempt_ids if all_terminal else [],
            successful_attempt_ids=attempt_ids if successful else [],
            failed_attempt_ids=[
                item.attempt_id
                for item in attempts
                if item.terminal_status is ProviderTerminalStatus.FAILED
            ],
            unknown_attempt_ids=[
                item.attempt_id
                for item in attempts
                if item.terminal_status is ProviderTerminalStatus.UNKNOWN
            ],
            budget_exhausted=False,
            competing_answer_queries=1,
            contradiction_queries=1,
            coverage_status=(
                CoverageStatus.COMPLETE
                if successful
                else CoverageStatus.INCOMPLETE
            ),
        )
        verification_anchor_ids = {
            anchor_id
            for item in attempts
            for anchor_id in item.anchor_ids
        }
        anchors = [
            anchor
            for anchor in self._models(
                snapshot, "evidence_anchor", EvidenceAnchor
            )
            if anchor.anchor_id in verification_anchor_ids
        ]
        bundle = VerificationEvidenceBundle(
            plan=plans,
            attempts=attempts,
            coverage=coverage,
            anchors=anchors,
            supporting_anchor_ids=sorted(verification_anchor_ids),
            contradicting_anchor_ids=[],
            completeness_basis=CompletenessBasis.CANONICAL_IDENTITY,
        )
        capability = default_objective_role_registry()[
            SubagentRole.ANSWER_VERIFICATION
        ]
        task = build_objective_subagent_task(
            task_id=f"verification.{decision.decision_id}",
            role=SubagentRole.ANSWER_VERIFICATION,
            capability=capability,
            goal={
                "blind_answer_query": query.model_dump(mode="json"),
                "verification_plan": plans.model_dump(mode="json"),
                "allowed_providers": plans.allowed_provider_ids,
            },
            input_artifact_ids=[
                hypothesis.hypothesis_id,
                *verification_anchor_ids,
            ],
        )
        outcome = AnswerVerificationSubagent(self.model).run(
            task,
            hypothesis=hypothesis,
            claims=claims,
            evidence=bundle,
        )
        artifacts = [
            _artifact(
                outcome.resolution,
                "answer_resolution",
                decision,
                SubagentRole.ANSWER_VERIFICATION,
            )
        ]
        if outcome.frozen_contract is not None:
            artifacts.append(
                _artifact(
                    outcome.frozen_contract,
                    "frozen_question_contract",
                    decision,
                    SubagentRole.ANSWER_VERIFICATION,
                )
            )
        self._commit_child(
            snapshot,
            decision,
            task.subtask_id,
            outcome.result,
            artifacts,
        )
        self._advance_hypothesis(
            hypothesis.hypothesis_id,
            (
                [
                    HypothesisState.VERIFICATION_PLANNED,
                    HypothesisState.VERIFYING,
                    HypothesisState.ANSWER_VERIFIED,
                ]
                if outcome.resolution.status is AnswerStatus.VERIFIED
                else [HypothesisState.REJECTED]
            ),
        )

    def _realize_question(
        self,
        snapshot: ObjectiveRuntimeSnapshot,
        decision: MainAgentDecision,
    ) -> None:
        contracts = self._models(
            snapshot, "frozen_question_contract", FrozenQuestionContract
        )
        resolutions = self._models(
            snapshot, "answer_resolution", AnswerResolution
        )
        if not contracts:
            raise ValueError("question realization requires a frozen contract")
        contract = contracts[0]
        resolution = next(
            (
                item
                for item in resolutions
                if item.resolution_id == contract.resolution_id
            ),
            None,
        )
        if resolution is None:
            raise ValueError("contract references an unknown answer resolution")
        capability = default_objective_role_registry()[
            SubagentRole.QUESTION_REALIZATION
        ]
        task = build_objective_subagent_task(
            task_id=f"realization.{decision.decision_id}",
            role=SubagentRole.QUESTION_REALIZATION,
            capability=capability,
            goal={
                "frozen_question_contract": contract.model_dump(mode="json")
            },
            input_artifact_ids=[contract.contract_id, contract.resolution_id],
        )
        outcome = ObjectiveQuestionRealizationSubagent(self.model).run(
            task,
            contract=contract,
        )
        self._commit_child(
            snapshot,
            decision,
            task.subtask_id,
            outcome.result,
            [
                _artifact(
                    outcome.draft,
                    "objective_question_draft",
                    decision,
                    SubagentRole.QUESTION_REALIZATION,
                )
            ],
        )
        self._advance_hypothesis(
            resolution.hypothesis_id,
            [HypothesisState.QUESTION_REALIZED],
        )

    def _review_release(
        self,
        snapshot: ObjectiveRuntimeSnapshot,
        decision: MainAgentDecision,
    ) -> None:
        drafts = self._models(
            snapshot, "objective_question_draft", ObjectiveQuestionDraft
        )
        contracts = self._models(
            snapshot, "frozen_question_contract", FrozenQuestionContract
        )
        resolutions = self._models(
            snapshot, "answer_resolution", AnswerResolution
        )
        if len(drafts) != 1 or len(contracts) != 1 or len(resolutions) != 1:
            raise ValueError(
                "release review currently requires one complete question"
            )
        draft, contract, resolution = drafts[0], contracts[0], resolutions[0]
        verification_anchors = [
            artifact.payload
            for artifact in snapshot.artifacts.values()
            if artifact.artifact_type == "evidence_anchor"
            and artifact.metadata.get("purpose") == "verification"
        ]
        providers = sorted(
            {
                artifact.payload["request"]["provider_id"]
                for artifact in snapshot.artifacts.values()
                if artifact.artifact_type == "provider_result"
                and artifact.metadata.get("purpose") == "verification"
            }
        )
        answer_package = AnswerEvidenceAuditPackage(
            question=PublicQuestionPackage(
                question_id=draft.draft_id,
                question_text=draft.question_text,
            ),
            frozen_question_contract=contract,
            answer_resolution=resolution,
            independent_evidence=IndependentEvidenceAuditPackage(
                evidence_used=providers,
                anchors=verification_anchors,
            ),
        )
        report = ReleaseReviewSubagent(self.model).run(
            public_question=answer_package.question,
            answer_evidence=answer_package,
        )
        self._commit_child(
            snapshot,
            decision,
            f"release.{decision.decision_id}",
            _success_result(
                f"release.{decision.decision_id}",
                [report.report_id],
                "release_reviewed",
            ),
            [
                _artifact(
                    report,
                    "objective_validation_report",
                    decision,
                    SubagentRole.RELEASE_REVIEW,
                )
            ],
        )
        if report.verdict is ObjectiveVerdict.PASS:
            self._advance_hypothesis(
                resolution.hypothesis_id,
                [
                    HypothesisState.RELEASE_REVIEWED,
                    HypothesisState.PUBLISHED,
                ],
            )

    # ---- shared helpers ------------------------------------------------

    def _commit_child(
        self,
        snapshot: ObjectiveRuntimeSnapshot,
        decision: MainAgentDecision,
        slot_id: str,
        result,
        artifacts: list[ArtifactEnvelope],
    ) -> None:
        self.runtime.register_child_slot(
            expected_state_revision=snapshot.state_revision,
            slot_id=slot_id,
            parent_decision_id=decision.decision_id,
            subtask_revision=0,
        )
        self.runtime.commit_child_result(
            slot_id=slot_id,
            expected_subtask_revision=0,
            result=result,
            artifacts=artifacts,
        )

    def _models(self, snapshot, artifact_type: str, model: type):
        return [
            model.model_validate(artifact.payload)
            for artifact in snapshot.artifacts.values()
            if artifact.artifact_type == artifact_type
        ]

    def _advance_hypothesis(
        self,
        hypothesis_id: str,
        states: list[HypothesisState],
    ) -> None:
        for target in states:
            snapshot = self.runtime.load()
            self.runtime.set_hypothesis_state(
                expected_state_revision=snapshot.state_revision,
                hypothesis_id=hypothesis_id,
                target=target,
            )

    def _finish(self, stop_reason: str) -> None:
        snapshot = self.runtime.load()
        target = (
            RunState.COMPLETED
            if any(
                ObjectiveValidationReport.model_validate(
                    artifact.payload
                ).release_allowed
                for artifact in snapshot.artifacts.values()
                if artifact.artifact_type == "objective_validation_report"
            )
            else RunState.COMPLETED_WITH_ZERO_QUESTIONS
        )
        self.runtime.set_run_state(
            expected_state_revision=snapshot.state_revision,
            target=target,
            stop_reason=stop_reason,
        )

    def _published_questions(
        self,
        snapshot: ObjectiveRuntimeSnapshot,
    ) -> list[PublishedObjectiveQuestion]:
        drafts = self._models(
            snapshot, "objective_question_draft", ObjectiveQuestionDraft
        )
        contracts = self._models(
            snapshot, "frozen_question_contract", FrozenQuestionContract
        )
        resolutions = self._models(
            snapshot, "answer_resolution", AnswerResolution
        )
        reports = [
            ObjectiveValidationReport.model_validate(artifact.payload)
            for artifact in snapshot.artifacts.values()
            if artifact.artifact_type == "objective_validation_report"
            and ObjectiveValidationReport.model_validate(
                artifact.payload
            ).release_allowed
        ]
        if not drafts or not contracts or not resolutions or not reports:
            return []
        return [
            PublishedObjectiveQuestion(
                draft=drafts[0],
                frozen_contract=contracts[0],
                resolution=resolutions[0],
                validation_report=reports[0],
            )
        ]


def _artifact(
    model: Any,
    artifact_type: str,
    decision: MainAgentDecision,
    role: SubagentRole,
    *,
    metadata: dict[str, Any] | None = None,
) -> ArtifactEnvelope:
    payload = (
        model.model_dump(mode="json")
        if hasattr(model, "model_dump")
        else dict(model)
    )
    artifact_id = _artifact_id(artifact_type, payload)
    return ArtifactEnvelope(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        payload=payload,
        producer_role=role.value,
        parent_decision_id=decision.decision_id,
        metadata=dict(metadata or {}),
    )


def _artifact_id(artifact_type: str, payload: dict[str, Any]) -> str:
    preferred_key = {
        "evidence_material": "material_id",
        "evidence_anchor": "anchor_id",
        "candidate_claim": "claim_id",
        "question_hypothesis": "hypothesis_id",
        "answer_resolution": "resolution_id",
        "frozen_question_contract": "contract_id",
        "objective_question_draft": "draft_id",
        "objective_validation_report": "report_id",
    }.get(artifact_type)
    candidates = (
        preferred_key,
        "material_id",
        "anchor_id",
        "claim_id",
        "hypothesis_id",
        "resolution_id",
        "contract_id",
        "draft_id",
        "report_id",
        "resolution_id",
        "question_id",
        "artifact_id",
    )
    for key in candidates:
        if key is None:
            continue
        value = payload.get(key)
        if value:
            return str(value)
    return f"{artifact_type}.{canonical_digest(payload)[:24]}"


def _success_result(subtask_id: str, artifact_ids: list[str], stop_reason: str):
    from .objective_question_contracts import (
        DomainOutcome,
        ExecutionStatus,
        SubagentResult,
        VerificationVerdict,
    )

    return SubagentResult(
        subtask_id=subtask_id,
        execution_status=ExecutionStatus.SUCCEEDED,
        domain_outcome=DomainOutcome.NOT_APPLICABLE,
        verdict=VerificationVerdict.NOT_EMITTED,
        output_artifact_ids=artifact_ids,
        evidence_need_ids=[],
        usage={},
        error_category=None,
        stop_reason=stop_reason,
    )
