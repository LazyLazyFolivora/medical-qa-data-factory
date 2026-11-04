"""Main-agent decision loop for the objective-question pipeline."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
from typing import Any, Mapping, Protocol

from pydantic import ValidationError

from ..llm.errors import ModelError, ModelErrorCode
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
from .verification_evidence_view import MAX_VIEW_CALLS
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
    ExecutionStatus,
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
    SubagentResult,
    VerificationAttempt,
    VerificationCoverageReport,
    VerificationPlan,
    VerificationVerdict,
    DomainOutcome,
    canonical_digest,
)
from .objective_question_persistence import ObjectiveQuestionPersistence
from .objective_question_realization import (
    ObjectiveQuestionDraft,
    QuestionSemanticReadback,
    ObjectiveQuestionRealizationSubagent,
)
from .objective_question_runtime import (
    ArtifactEnvelope,
    ObjectiveRuntimeRepository,
    ObjectiveRuntimeSnapshot,
    SubagentLoopGuard,
)
from .question_design import ObjectiveQuestionDesignSubagent
from .candidate_review import review_candidates
from .workflow_scheduler import FINAL_QUESTION_WORKFLOW, is_hierarchical, MAX_EVIDENCE_ROUNDS, target_question_count
from .release_review_subagent import (
    AnswerEvidenceAuditPackage,
    IndependentEvidenceAuditPackage,
    PublicQuestionPackage,
    ReleaseReviewSubagent,
)
from .subagent_contracts import (
    RoleContractViolation,
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
        max_actions: int = 100,
        max_consecutive_empty_retrievals: int = 3,
        max_consecutive_provider_failures: int = 3,
    ) -> None:
        self.runtime = runtime
        self.persistence = persistence
        self.goal = goal
        self.main_agent = main_agent
        self.model = model
        self.providers = dict(providers)
        self.max_actions = max_actions
        self.max_consecutive_empty_retrievals = (
            max_consecutive_empty_retrievals
        )
        self.max_consecutive_provider_failures = max(
            1,
            max_consecutive_provider_failures,
        )
        self._empty_retrieval_streak = 0
        self._provider_failure_streak = 0
        self.loop_guard = SubagentLoopGuard(max_repeated_decisions=3)

    def run(self) -> ObjectiveAgentLoopResult:
        stop_reason: str | None = None
        for _ in range(self.max_actions):
            if self.runtime.pause_at_boundary():
                break
            snapshot = self.runtime.load()
            decision = self.runtime.pending_decision()
            if decision is None:
                decision = self.main_agent.decide(snapshot)
                validate_main_agent_decision(snapshot, decision)
                snapshot = self.runtime.record_decision(
                    expected_state_revision=snapshot.state_revision,
                    decision=decision,
                )
            if self.runtime.pause_at_boundary():
                break
            self.loop_guard.observe(decision)
            action = MainAgentAction(decision.action)
            if action is MainAgentAction.STOP:
                stop_reason = decision.stop_reason or "main_agent_stop"
                self._finish(stop_reason, completed=is_hierarchical(decision))
                break
            try:
                if action is MainAgentAction.CLOSE_CANDIDATE:
                    self._close_candidate(snapshot, decision)
                elif action in {
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
            except (ModelError, RoleContractViolation, ValidationError) as exc:
                self._record_recoverable_failure(
                    decision,
                    action.value,
                    exc,
                )
                if (isinstance(exc, ModelError) and exc.code is ModelErrorCode.BAD_REQUEST
                        and not exc.retryable):
                    latest = self.runtime.load()
                    self.runtime.set_run_state(
                        expected_state_revision=latest.state_revision,
                        target=RunState.RECOVERY_REQUIRED,
                        stop_reason="model_request_requires_repair",
                        recovery_error=exc.as_dict(),
                    )
                    break
            if self.runtime.pause_at_boundary():
                break
            if (
                not is_hierarchical(decision)
                and self._empty_retrieval_streak
                >= self.max_consecutive_empty_retrievals
            ):
                stop_reason = "no_new_evidence"
                self._finish(stop_reason)
                break
            if (
                not is_hierarchical(decision)
                and self._provider_failure_streak
                >= self.max_consecutive_provider_failures
            ):
                stop_reason = "provider_unavailable"
                self._finish(stop_reason)
                break
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
        verification_focus = (
            str(
                decision.action_parameters.get(
                    "verification_focus", "comprehensive"
                )
            )
            if purpose == "verification"
            else None
        )
        artifact_metadata = {"purpose": purpose}
        if is_hierarchical(decision) and decision.action_parameters.get("hypothesis_id"):
            artifact_metadata["hypothesis_id"] = decision.action_parameters["hypothesis_id"]
        if verification_focus is not None:
            artifact_metadata["verification_focus"] = verification_focus
        capability = default_objective_role_registry()[
            SubagentRole.MATERIAL_RETRIEVAL
        ]
        task = build_objective_subagent_task(
            task_id=f"materials.{decision.decision_id}",
            role=SubagentRole.MATERIAL_RETRIEVAL,
            capability=capability,
            goal={
                "target_entity": self.goal.target_entity,
                "evidence_need": str(
                    decision.action_parameters.get("evidence_need")
                    or f"{purpose}: {self.goal.target_entity}; "
                    f"query={decision.action_parameters.get('query', {})}"
                ),
                "allowed_providers": [provider_id],
                "material_limit": max(
                    1,
                    min(
                        50,
                        int(
                            decision.action_parameters.get(
                                "material_limit",
                                10,
                            )
                        ),
                    ),
                ),
                "max_search_rounds": max(
                    1,
                    min(
                        8,
                        int(
                            decision.action_parameters.get(
                                "max_search_rounds",
                                4,
                            )
                        ),
                    ),
                ),
                "max_queries_per_round": max(
                    1,
                    min(
                        4,
                        int(
                            decision.action_parameters.get(
                                "max_queries_per_round",
                                2,
                            )
                        ),
                    ),
                ),
                "preview_chars": max(
                    500,
                    min(
                        6000,
                        int(
                            decision.action_parameters.get(
                                "preview_chars",
                                2000,
                            )
                        ),
                    ),
                ),
            },
            input_artifact_ids=[],
            allowed_tools=[provider_id],
        )
        outcome = MaterialRetrievalSubagent(
            self.persistence,
            model=self.model,
        ).run(
            task,
            provider=provider,
            query=dict(decision.action_parameters.get("query", {})),
        )
        (
            self._empty_retrieval_streak,
            self._provider_failure_streak,
        ) = _advance_retrieval_streaks(
            outcome.provider_result.terminal_status,
            has_materials=bool(outcome.materials),
            empty_streak=self._empty_retrieval_streak,
            provider_failure_streak=self._provider_failure_streak,
        )
        artifacts = [
            _artifact(
                item,
                "evidence_material",
                decision,
                SubagentRole.MATERIAL_RETRIEVAL,
                metadata=artifact_metadata,
            )
            for item in outcome.materials
        ]
        artifacts.extend(
            _artifact(
                item,
                "evidence_anchor",
                decision,
                SubagentRole.MATERIAL_RETRIEVAL,
                metadata=artifact_metadata,
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
                metadata=artifact_metadata,
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
        all_materials = self._models(
            snapshot,
            "evidence_material",
            EvidenceMaterial,
        )
        processed_material_ids = _processed_fact_material_ids(snapshot)
        materials = (
            [
                material
                for material in all_materials
                if material.material_id not in processed_material_ids
            ]
            if processed_material_ids
            else all_materials
        )
        if is_hierarchical(decision):
            selected_ids = set(decision.action_parameters.get("processing_ids", []))
            materials = [m for m in materials if m.material_id in selected_ids]
        selected_material_ids = {
            material.material_id for material in materials
        }
        anchors = [
            anchor
            for anchor in self._models(
                snapshot,
                "evidence_anchor",
                EvidenceAnchor,
            )
            if anchor.material_id in selected_material_ids
        ]
        if not materials or not anchors:
            raise ValueError("fact understanding requires persisted materials")
        canonical_name = self.goal.target_entity
        authority = "task-scoped target"
        target_id = (self.goal.target_entity_id or "").removeprefix("gene:")
        for material in all_materials:
            if (not target_id.isdigit() or material.material_type != "ncbi_gene_record"
                    or material.provider_id != "ncbi_gene" or material.source_record_id != f"gene:{target_id}"):
                continue
            record = json.loads(self.persistence.load_raw_content(material)).get("summary", {}).get("record", {})
            symbol = record.get("nomenclaturesymbol") or record.get("name") or ""
            if (symbol.casefold() == self.goal.target_entity.casefold()
                    and str(record.get("organism", {}).get("taxid")) == "9606"
                    and record.get("nomenclaturename")):
                canonical_name = record["nomenclaturename"]
                authority = f"NCBI Gene {target_id} (human, matching symbol)"
                break
        resolution = EntityResolution(
            resolution_id=f"entity_resolution.{decision.decision_id}",
            mention=self.goal.target_entity,
            namespace="objective-question",
            canonical_id=self.goal.target_entity_id or self.goal.target_entity,
            canonical_name=canonical_name,
            species="Homo sapiens",
            authority=authority,
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
        artifacts, claim_ids = _fact_claim_artifacts(
            snapshot,
            outcome.claims,
            decision,
        )
        artifacts.append(
            _artifact(
                {
                    "batch_id": f"fact_batch.{decision.decision_id}",
                    "material_ids": sorted(selected_material_ids),
                    "anchor_ids": [anchor.anchor_id for anchor in anchors],
                    "claim_ids": claim_ids,
                    "stop_reason": outcome.result.stop_reason,
                    "domain_outcome": outcome.result.domain_outcome.value,
                },
                "fact_understanding_batch",
                decision,
                SubagentRole.FACT_UNDERSTANDING,
            )
        )
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
            outcome.result.model_copy(
                update={"output_artifact_ids": claim_ids}
            ),
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
        hypotheses = self._models(
            snapshot,
            "question_hypothesis",
            QuestionHypothesis,
        )
        remaining_question_count = max(
            0,
            self.goal.question_count_upper_bound - target_question_count(snapshot),
        )
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
                    remaining_question_count
                ),
                "difficulty_targets": self.goal.difficulty_targets,
                "style_targets": self.goal.style_targets,
                "language": self.goal.language,
                "planner_environment": self.goal.planner_environment,
                "candidate_claims": [claim.claim_id for claim in claims],
                "claim_groups": _claim_evidence_groups(claims, anchors),
                "evidence_excerpts": _claim_evidence_excerpts(
                    claims,
                    anchors,
                ),
                "question_portfolio": _question_portfolio(
                    snapshot,
                    hypotheses,
                    claims,
                    anchors,
                ),
            },
            input_artifact_ids=[
                *[claim.claim_id for claim in claims],
                *sorted(
                    {
                        anchor_id
                        for claim in claims
                        for anchor_id in claim.supporting_anchor_ids
                    }
                ),
            ],
        )
        outcome = ObjectiveQuestionDesignSubagent(self.model).run(
            task,
            claims=claims,
            anchors=anchors,
        )
        original_candidates = [h.model_dump(mode="json") for h in outcome.hypotheses]
        reviews, review_history = [], []
        if snapshot.manifest.orchestrator_version != FINAL_QUESTION_WORKFLOW:
            reviewed, reviews, review_history = review_candidates(
                self.model, outcome.hypotheses, claims, anchors, operation_id=task.operation_key)
            outcome = replace(outcome, hypotheses=reviewed)
        rejected_ids = {r.hypothesis_id for r in reviews if r.disposition == "REJECT"}
        artifacts = [
            _artifact(
                hypothesis,
                "question_hypothesis",
                decision,
                SubagentRole.QUESTION_DESIGN,
            )
            for hypothesis in outcome.hypotheses
        ]
        if reviews:
            artifacts.append(_artifact(
                {"artifact_id": f"candidate_review.{decision.decision_id}",
                 "reviews": [r.model_dump(mode="json") for r in reviews],
                 "review_history": review_history,
                 "original_candidates": original_candidates},
                "candidate_usability_review", decision, SubagentRole.QUESTION_DESIGN,
            ))
        if outcome.fact_selections:
            artifacts.append(_artifact(
                {"batch_id": f"design_notes.{decision.decision_id}",
                 "method": "fact-pair-v1-production-adaptation-1",
                 "fact_selections": [note.model_dump(mode="json") for note in outcome.fact_selections]},
                "question_design_notes", decision, SubagentRole.QUESTION_DESIGN,
            ))
        if not outcome.hypotheses or is_hierarchical(decision):
            artifacts.append(_artifact(
                {"batch_id": f"design_batch.{decision.decision_id}",
                 "claim_ids": [claim.claim_id for claim in claims],
                 "stop_reason": outcome.result.stop_reason,
                 "domain_outcome": outcome.result.domain_outcome.value},
                "question_design_batch", decision, SubagentRole.QUESTION_DESIGN,
            ))
        self._commit_child(
            snapshot,
            decision,
            task.subtask_id,
            outcome.result,
            artifacts,
        )
        latest = self.runtime.load()
        if not outcome.hypotheses:
            return
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
            if hypothesis.hypothesis_id in rejected_ids:
                latest = self.runtime.set_hypothesis_state(
                    expected_state_revision=latest.state_revision,
                    hypothesis_id=hypothesis.hypothesis_id,
                    target=HypothesisState.REJECTED,
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
        active_states = {
            HypothesisState.PROPOSED,
            HypothesisState.VERIFICATION_PLANNED,
            HypothesisState.VERIFYING,
        }
        resolved_hypothesis_ids = {
            resolution.hypothesis_id
            for resolution in self._models(
                snapshot, "answer_resolution", AnswerResolution
            )
        }
        active_hypotheses = [
            item
            for item in reversed(hypotheses)
            if snapshot.hypotheses.get(item.hypothesis_id) in active_states
            and item.hypothesis_id not in resolved_hypothesis_ids
        ]
        hypothesis = _resolve_verification_hypothesis(
            decision,
            hypotheses=active_hypotheses,
            resolved_hypothesis_ids=resolved_hypothesis_ids,
        )
        all_verification_results = [
            artifact
            for artifact in snapshot.artifacts.values()
            if artifact.artifact_type == "provider_result"
            and _artifact_has_purpose(artifact, "verification")
            and artifact.payload.get("terminal_status") == "SUCCEEDED"
        ]
        verification_results = _select_verification_results(
            decision,
            hypothesis=hypothesis,
            claims=claims,
            anchors=self._models(
                snapshot,
                "evidence_anchor",
                EvidenceAnchor,
            ),
            provider_results=all_verification_results,
        )
        final_wording = snapshot.manifest.orchestrator_version == FINAL_QUESTION_WORKFLOW
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
        successful_attempt_ids = [
            item.attempt_id
            for item in attempts
            if item.terminal_status is ProviderTerminalStatus.SUCCEEDED
        ]
        successful = len(successful_attempt_ids) == len(attempts)
        successful_focuses = {
            str(
                artifact.metadata.get(
                    "verification_focus", "comprehensive"
                )
            )
            for artifact, attempt in zip(
                verification_results, attempts, strict=True
            )
            if attempt.terminal_status is ProviderTerminalStatus.SUCCEEDED
        }
        competing_answer_queries = sum(
            focus in {"competing_answers", "comprehensive"}
            for focus in successful_focuses
        )
        contradiction_queries = sum(
            focus in {"contradictions", "comprehensive"}
            for focus in successful_focuses
        )
        coverage = VerificationCoverageReport(
            coverage_report_id=f"coverage.{hypothesis.hypothesis_id}",
            expected_attempt_ids=attempt_ids,
            terminal_attempt_ids=attempt_ids if all_terminal else [],
            successful_attempt_ids=successful_attempt_ids,
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
            competing_answer_queries=competing_answer_queries,
            contradiction_queries=contradiction_queries,
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
            contradicting_anchor_ids=[],
            completeness_basis=_completeness_basis_for_goal(self.goal),
        )
        capability = default_objective_role_registry()[
            SubagentRole.ANSWER_VERIFICATION
        ]
        task = build_objective_subagent_task(
            task_id=f"verification.{decision.decision_id}",
            role=SubagentRole.ANSWER_VERIFICATION,
            budget={"max_model_calls": 2 * MAX_VIEW_CALLS + 1, "max_provider_calls": 0},
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
            final_wording=final_wording,
            design_requirements={"style_targets": self.goal.style_targets,
                                 "language": self.goal.language, "planner_environment": self.goal.planner_environment},
            discovery_document_ids=frozenset(snapshot.artifacts[aid].payload.get("document_id", "")
                for aid in hypothesis.discovery_anchor_ids if aid in snapshot.artifacts),
        )
        audit = None
        if outcome.final_review:
            audit = _artifact({"artifact_id": f"final_review.{decision.decision_id}",
                "hypothesis_id": hypothesis.hypothesis_id, "question": hypothesis.question_intent,
                "blind_result": outcome.blind_result.model_dump(mode="json"),
                "review": outcome.final_review.model_dump(mode="json")},
                "final_question_review", decision, SubagentRole.ANSWER_VERIFICATION)
        if (is_hierarchical(decision)
                and outcome.resolution.status is AnswerStatus.INDETERMINATE
                and (outcome.evidence_need or "").strip()
                and decision.action_parameters.get("verification_round", 1) < MAX_EVIDENCE_ROUNDS):
            gap = {"artifact_id": f"verification_gap.{decision.decision_id}",
                   "hypothesis_id": hypothesis.hypothesis_id,
                   "round": decision.action_parameters.get("verification_round", 1),
                   "reason": outcome.result.stop_reason,
                   "evidence_need": outcome.evidence_need,
                   "question": hypothesis.question_intent,
                   "resolution": outcome.resolution.model_dump(mode="json"),
                   "used_anchor_ids": sorted(verification_anchor_ids)}
            if audit:
                gap["missing_evidence_kind"] = outcome.final_review.missing_evidence_kind
            gap_artifacts = [_artifact(gap, "verification_gap", decision, SubagentRole.ANSWER_VERIFICATION)]
            if audit:
                gap_artifacts.append(audit)
            gap_result = outcome.result.model_copy(update={"stop_reason": "verification_gap",
                "output_artifact_ids": [item.artifact_id for item in gap_artifacts]})
            self._commit_child(snapshot, decision, task.subtask_id, gap_result, gap_artifacts)
            return
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
        if audit:
            artifacts.append(audit)
            states = [HypothesisState.REJECTED if outcome.resolution.status is AnswerStatus.REJECTED
                      else HypothesisState.INDETERMINATE]
            if outcome.frozen_contract:
                draft, report = _publish_reviewed_wording(outcome, audit)
                artifacts.extend([_artifact(draft, "objective_question_draft", decision, SubagentRole.ANSWER_VERIFICATION),
                                  _artifact(report, "objective_validation_report", decision, SubagentRole.ANSWER_VERIFICATION)])
                path = [HypothesisState.PROPOSED, HypothesisState.VERIFICATION_PLANNED, HypothesisState.VERIFYING,
                        HypothesisState.ANSWER_VERIFIED, HypothesisState.QUESTION_REALIZED,
                        HypothesisState.RELEASE_REVIEWED, HypothesisState.PUBLISHED]
                states = path[path.index(snapshot.hypotheses[hypothesis.hypothesis_id]) + 1:]
            result = outcome.result.model_copy(update={"output_artifact_ids": [a.artifact_id for a in artifacts]})
            self._commit_child(snapshot, decision, task.subtask_id, result, artifacts,
                               hypothesis_transition=(hypothesis.hypothesis_id, states))
            return
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
                else (
                    [HypothesisState.INDETERMINATE]
                    if outcome.resolution.status
                    is AnswerStatus.INDETERMINATE
                    else [HypothesisState.REJECTED]
                )
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
        resolutions_by_id = {
            resolution.resolution_id: resolution
            for resolution in resolutions
        }
        drafted_contract_ids = {
            draft.contract_id
            for draft in self._models(
                snapshot, "objective_question_draft", ObjectiveQuestionDraft
            )
        }
        selected_contract_ids = set(decision.input_artifact_ids)
        repairing = is_hierarchical(decision) and bool(decision.action_parameters.get("repair_feedback"))
        candidates = [
            (contract, resolutions_by_id.get(contract.resolution_id))
            for contract in reversed(contracts)
            if (contract.contract_id not in drafted_contract_ids or repairing)
            and resolutions_by_id.get(contract.resolution_id) is not None
            and snapshot.hypotheses.get(
                resolutions_by_id[contract.resolution_id].hypothesis_id
            )
            in ({HypothesisState.QUESTION_REALIZED} if repairing else {HypothesisState.ANSWER_VERIFIED})
        ]
        if selected_contract_ids:
            candidates.sort(
                key=lambda item: item[0].contract_id in selected_contract_ids,
                reverse=True,
            )
        if not candidates:
            raise ValueError(
                "question realization requires an active verified contract"
            )
        contract, resolution = candidates[0]
        if resolution is None:  # pragma: no cover - filtered above
            raise ValueError(
                "question realization requires an answer resolution"
            )
        capability = default_objective_role_registry()[
            SubagentRole.QUESTION_REALIZATION
        ]
        task = build_objective_subagent_task(
            task_id=f"realization.{decision.decision_id}",
            role=SubagentRole.QUESTION_REALIZATION,
            capability=capability,
            goal={
                "frozen_question_contract": contract.model_dump(mode="json"),
                "design_requirements": {
                    "language": self.goal.language,
                    "difficulty_targets": self.goal.difficulty_targets,
                    "style_targets": self.goal.style_targets,
                    "repair_feedback": decision.action_parameters.get("repair_feedback"),
                },
            },
            input_artifact_ids=[contract.contract_id, contract.resolution_id],
        )
        try:
            outcome = ObjectiveQuestionRealizationSubagent(self.model).run(
                task,
                contract=contract,
            )
        except RoleContractViolation as exc:
            self._record_recoverable_failure(decision, decision.action, exc)
            self._advance_hypothesis(
                resolution.hypothesis_id,
                [HypothesisState.REJECTED],
            )
            return
        if repairing:
            revised = outcome.draft.model_copy(update={"draft_id": f"{outcome.draft.draft_id}.revision1"})
            outcome = replace(outcome, draft=revised,
                result=outcome.result.model_copy(update={"output_artifact_ids": [revised.draft_id]}))
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
        if not repairing:
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
        contracts_by_id = {
            contract.contract_id: contract for contract in contracts
        }
        resolutions_by_id = {
            resolution.resolution_id: resolution
            for resolution in resolutions
        }
        reviewed_draft_ids = {
            report.question_id
            for report in self._models(
                snapshot,
                "objective_validation_report",
                ObjectiveValidationReport,
            )
        }
        selected_draft_ids = set(decision.input_artifact_ids)
        candidates = []
        for draft in reversed(drafts):
            contract = contracts_by_id.get(draft.contract_id)
            if contract is None:
                continue
            resolution = resolutions_by_id.get(contract.resolution_id)
            if (
                resolution is not None
                and draft.draft_id not in reviewed_draft_ids
                and snapshot.hypotheses.get(resolution.hypothesis_id)
                is HypothesisState.QUESTION_REALIZED
            ):
                candidates.append((draft, contract, resolution))
        if selected_draft_ids:
            candidates.sort(
                key=lambda item: item[0].draft_id in selected_draft_ids,
                reverse=True,
            )
        if not candidates:
            raise ValueError(
                "release review requires an unreviewed realized question"
            )
        draft, contract, resolution = candidates[0]
        additional = set(decision.action_parameters.get("additional_anchor_ids", []))
        verification_anchors = [
            artifact.payload
            for artifact in snapshot.artifacts.values()
            if artifact.artifact_type == "evidence_anchor"
            and (_artifact_has_purpose(artifact, "verification") or artifact.artifact_id in additional)
            and (
                not resolution.supporting_anchor_ids
                or artifact.artifact_id
                in {
                    *resolution.supporting_anchor_ids,
                    *resolution.contradicting_anchor_ids,
                    *additional,
                }
            )
        ]
        providers = sorted(
            set(resolution.verification_sources)
            or {
                artifact.payload["request"]["provider_id"]
                for artifact in snapshot.artifacts.values()
                if artifact.artifact_type == "provider_result"
                and _artifact_has_purpose(artifact, "verification")
            }
        )
        providers = sorted(set(providers) | {a.payload["provider_id"] for a in snapshot.artifacts.values()
                           if a.artifact_type == "evidence_material" and any(
                               snapshot.artifacts[key].payload["material_id"] == a.artifact_id for key in additional)})
        answer_package = AnswerEvidenceAuditPackage(
            question=PublicQuestionPackage(
                question_id=draft.draft_id,
                question_text=draft.question_text,
            ),
            frozen_question_contract=contract,
            answer_resolution=resolution,
            design_requirements={
                "language": self.goal.language,
                "difficulty_targets": self.goal.difficulty_targets,
                "style_targets": self.goal.style_targets,
            },
            planner_environment=self.goal.planner_environment,
            concise_explanation=draft.concise_explanation,
            independent_evidence=IndependentEvidenceAuditPackage(
                evidence_used=providers,
                anchors=verification_anchors,
            ),
        )
        reviewer = ReleaseReviewSubagent(self.model)
        review_outcome = reviewer.run_with_stages(
            public_question=answer_package.question, answer_evidence=answer_package,
        )
        report = review_outcome.report
        if (is_hierarchical(decision) and not additional and review_outcome.answer_audit is not None
                and report.verdict is not ObjectiveVerdict.PASS
                and report.primary_reason in {"INSUFFICIENT_VALIDATION_EVIDENCE", "UNSUPPORTED_ANSWER"}):
            hid = resolution.hypothesis_id
            used = {a["document_id"] for a in verification_anchors}
            used.update(snapshot.artifacts[aid].payload["document_id"]
                        for aid in snapshot.artifacts[hid].payload["discovery_anchor_ids"])
            gap = {"artifact_id": f"release_gap.{decision.decision_id}", "kind": "release_evidence_gap",
                   "draft_id": draft.draft_id, "hypothesis_id": hid, "used_documents": sorted(used),
                   "evidence_need": review_outcome.answer_audit.evidence_need or
                       f"Find independent evidence directly identifying the subject and relation in: {draft.question_text}",
                   "initial_report": report.model_dump(mode="json")}
            self._commit_child(snapshot, decision, f"release.{decision.decision_id}",
                _success_result(f"release.{decision.decision_id}", [gap["artifact_id"]], "release_evidence_gap"),
                [_artifact(gap, "release_evidence_gap", decision, SubagentRole.RELEASE_REVIEW)])
            return
        can_repair_wording = (
            is_hierarchical(decision) and not draft.draft_id.endswith(".revision1")
            and review_outcome.public_audit is not None
            and review_outcome.public_audit.verdict is not ObjectiveVerdict.PASS
            and review_outcome.answer_audit is None
        )
        repair_artifacts = []
        if can_repair_wording:
            repair_artifacts.append(_artifact(
                {"artifact_id": f"wording_repair.{decision.decision_id}",
                 "hypothesis_id": resolution.hypothesis_id, "draft_id": draft.draft_id,
                 "previous_question": draft.question_text,
                 "feedback": review_outcome.public_audit.model_dump(mode="json")},
                "wording_repair_request", decision, SubagentRole.RELEASE_REVIEW))
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
                ), *repair_artifacts,
            ],
        )
        if can_repair_wording:
            return
        if report.verdict is ObjectiveVerdict.PASS:
            self._advance_hypothesis(
                resolution.hypothesis_id,
                [
                    HypothesisState.RELEASE_REVIEWED,
                    HypothesisState.PUBLISHED,
                ],
            )
        else:
            self._advance_hypothesis(
                resolution.hypothesis_id,
                [
                    HypothesisState.REJECTED
                    if report.verdict is ObjectiveVerdict.REJECT
                    else HypothesisState.INDETERMINATE
                ],
            )

    # ---- shared helpers ------------------------------------------------

    def _close_candidate(self, snapshot, decision):
        hid = decision.action_parameters["hypothesis_id"]
        artifact = _artifact(
            {"artifact_id": f"candidate_closed.{decision.decision_id}",
             "hypothesis_id": hid, "reason": decision.action_parameters.get("closure_reason")},
            "candidate_closure", decision, SubagentRole.ANSWER_VERIFICATION)
        self._commit_child(snapshot, decision, f"close.{decision.decision_id}",
            _success_result(f"close.{decision.decision_id}", [artifact.artifact_id], "candidate_closed"), [artifact])
        self._advance_hypothesis(hid, [HypothesisState.INDETERMINATE])

    def _commit_child(
        self,
        snapshot: ObjectiveRuntimeSnapshot,
        decision: MainAgentDecision,
        slot_id: str,
        result,
        artifacts: list[ArtifactEnvelope],
        *, hypothesis_transition=None,
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
            hypothesis_transition=hypothesis_transition,
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

    def _finish(self, stop_reason: str, *, completed: bool = False) -> None:
        snapshot = self.runtime.load()
        published_count = sum(
            state is HypothesisState.PUBLISHED
            for state in snapshot.hypotheses.values()
        )
        target = (
            RunState.COMPLETED
            if target_question_count(snapshot) >= self.goal.question_count_upper_bound
            else (RunState.COMPLETED if published_count else RunState.COMPLETED_WITH_ZERO_QUESTIONS)
            if completed
            else RunState.RECOVERY_REQUIRED
        )
        self.runtime.set_run_state(
            expected_state_revision=snapshot.state_revision,
            target=target,
            stop_reason=stop_reason,
        )

    def _record_recoverable_failure(
        self,
        decision: MainAgentDecision,
        action: str,
        exc: Exception,
    ) -> None:
        snapshot = self.runtime.load()
        raw_code = getattr(exc, "code", type(exc).__name__)
        error_code = str(getattr(raw_code, "value", raw_code))
        error_details = (
            exc.as_dict()
            if isinstance(exc, ModelError)
            else {}
        )
        base_failure_id = f"failure.{decision.decision_id}"
        failure_payload = {
            "action": action,
            "error_code": error_code,
            "error_message": str(exc),
            "retryable": bool(getattr(exc, "retryable", False)),
            **{
                key: value
                for key, value in error_details.items()
                if value is not None
            },
        }
        failure_id = base_failure_id
        existing = snapshot.artifacts.get(base_failure_id)
        if (
            existing is not None
            and {
                key: value
                for key, value in existing.payload.items()
                if key != "failure_id"
            }
            != failure_payload
        ):
            failure_id = (
                base_failure_id
                + "."
                + canonical_digest(failure_payload)[:12]
            )
        artifact = ArtifactEnvelope(
            artifact_id=failure_id,
            artifact_type="subagent_failure",
            payload={
                "failure_id": failure_id,
                **failure_payload,
            },
            producer_role=decision.target_subagent_role,
            parent_decision_id=decision.decision_id,
            metadata={
                "action": action,
                "error_code": error_code,
                "retryable": bool(getattr(exc, "retryable", False)),
                "status_code": error_details.get("status_code"),
                "attempts": error_details.get("attempts"),
            },
        )
        self._commit_child(
            snapshot,
            decision,
            failure_id,
            SubagentResult(
                subtask_id=failure_id,
                execution_status=ExecutionStatus.FAILED,
                domain_outcome=DomainOutcome.INSUFFICIENT,
                verdict=VerificationVerdict.NOT_EMITTED,
                output_artifact_ids=[failure_id],
                evidence_need_ids=[],
                usage={},
                error_category=error_code,
                stop_reason="recoverable_subagent_error",
            ),
            [artifact],
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
        drafts_by_id = {draft.draft_id: draft for draft in drafts}
        contracts_by_id = {
            contract.contract_id: contract for contract in contracts
        }
        resolutions_by_id = {
            resolution.resolution_id: resolution
            for resolution in resolutions
        }
        published = []
        for report in reports:
            draft = drafts_by_id.get(report.question_id)
            if draft is None:
                continue
            contract = contracts_by_id.get(draft.contract_id)
            if contract is None:
                continue
            resolution = resolutions_by_id.get(contract.resolution_id)
            if resolution is None:
                continue
            published.append(
                PublishedObjectiveQuestion(
                    draft=draft,
                    frozen_contract=contract,
                    resolution=resolution,
                    validation_report=report,
                )
            )
        return published


def _publish_reviewed_wording(outcome, audit):
    contract = outcome.frozen_contract
    review = outcome.final_review
    readback = QuestionSemanticReadback.model_validate(
        contract.model_dump(include=set(QuestionSemanticReadback.model_fields)))
    draft = ObjectiveQuestionDraft(draft_id=f"draft.{contract.contract_id}", contract_id=contract.contract_id,
        question_text=contract.question_intent, answer_values=contract.answer_values,
        concise_explanation=review.explanation, semantic_readback=readback)
    report = ObjectiveValidationReport(report_id=f"validation.{draft.draft_id}", question_id=draft.draft_id,
        verdict=ObjectiveVerdict.PASS, release_allowed=True, primary_reason="PASS",
        assessed_difficulty=review.assessed_difficulty, public_question_audit_id=audit.artifact_id,
        answer_audit_id=audit.artifact_id, evidence_used=outcome.resolution.verification_sources,
        validator_version="final-question-v2", input_digest=canonical_digest(
            [draft.model_dump(mode="json"), outcome.resolution.digest(), audit.digest]))
    return draft, report


def _processed_fact_material_ids(
    snapshot: ObjectiveRuntimeSnapshot,
) -> set[str]:
    return {
        str(material_id)
        for artifact in snapshot.artifacts.values()
        if artifact.artifact_type == "fact_understanding_batch"
        for material_id in artifact.payload.get("material_ids", [])
    }


def _resolve_verification_hypothesis(
    decision: MainAgentDecision,
    *,
    hypotheses: list[QuestionHypothesis],
    resolved_hypothesis_ids: set[str],
) -> QuestionHypothesis:
    available = [
        hypothesis
        for hypothesis in hypotheses
        if hypothesis.hypothesis_id not in resolved_hypothesis_ids
    ]
    if not available:
        raise RoleContractViolation(
            "answer verification requires an unresolved hypothesis"
        )

    declared_ids = _hypothesis_ids_from_action(decision)
    input_ids = {
        artifact_id
        for artifact_id in decision.input_artifact_ids
        if artifact_id.startswith("hypothesis.")
    }
    selected_ids = declared_ids or input_ids
    if declared_ids and input_ids and declared_ids != input_ids:
        raise RoleContractViolation(
            "verification target disagrees with input hypothesis artifacts"
        )
    if len(selected_ids) > 1:
        raise RoleContractViolation(
            "verification requires exactly one hypothesis target"
        )

    if selected_ids:
        selected_id = next(iter(selected_ids))
    elif len(available) == 1:
        selected_id = available[0].hypothesis_id
    else:
        raise RoleContractViolation(
            "verification requires exactly one hypothesis target"
        )

    selected = next(
        (
            hypothesis
            for hypothesis in available
            if hypothesis.hypothesis_id == selected_id
        ),
        None,
    )
    if selected is None:
        raise RoleContractViolation(
            f"verification target is not active: {selected_id}"
        )
    return selected


def _hypothesis_ids_from_action(
    decision: MainAgentDecision,
) -> set[str]:
    values: list[Any] = []
    for key in (
        "hypothesis_id",
        "target_hypothesis_id",
        "target_hypothesis",
        "artifact_id",
        "target_artifact_id",
        "hypothesis_ids",
        "target_hypothesis_ids",
        "artifact_ids",
    ):
        value = decision.action_parameters.get(key)
        if isinstance(value, list):
            values.extend(value)
        elif value is not None:
            values.append(value)
    return {
        str(value)
        for value in values
        if str(value).startswith("hypothesis.")
    }


def _select_verification_results(
    decision: MainAgentDecision,
    *,
    hypothesis: QuestionHypothesis,
    claims: list[CandidateClaim],
    anchors: list[EvidenceAnchor],
    provider_results: list[ArtifactEnvelope],
) -> list[ArtifactEnvelope]:
    claim_by_id = {claim.claim_id: claim for claim in claims}
    unknown_claims = sorted(
        set(hypothesis.candidate_claim_ids) - set(claim_by_id)
    )
    if unknown_claims:
        raise RoleContractViolation(
            f"hypothesis references unknown claims: {unknown_claims}"
        )

    target_anchor_ids = {
        anchor_id
        for claim_id in hypothesis.candidate_claim_ids
        for anchor_id in claim_by_id[claim_id].supporting_anchor_ids
    }
    target_anchor_ids.update(hypothesis.discovery_anchor_ids)
    target_anchor_ids.update(
        str(anchor_id)
        for anchor_id in decision.action_parameters.get(
            "evidence_anchor_ids",
            [],
        )
    )
    anchor_by_id = {anchor.anchor_id: anchor for anchor in anchors}
    target_material_ids = {
        anchor_by_id[anchor_id].material_id
        for anchor_id in target_anchor_ids
        if anchor_id in anchor_by_id
    }
    target_material_ids.update(
        str(material_id)
        for material_id in decision.action_parameters.get(
            "evidence_material_ids",
            [],
        )
    )

    explicit_provider_result_ids = {
        artifact_id
        for artifact_id in decision.input_artifact_ids
        if artifact_id.startswith("provider_result.")
    }
    explicit_provider_result_ids.update(
        str(artifact_id)
        for artifact_id in decision.action_parameters.get(
            "provider_result_ids",
            [],
        )
    )

    if explicit_provider_result_ids:
        selected = [
            artifact
            for artifact in provider_results
            if artifact.artifact_id in explicit_provider_result_ids
        ]
        if not selected:
            raise RoleContractViolation(
                "verification references unknown provider results"
            )
        return selected

    selected = [
        artifact
        for artifact in provider_results
        if (
            set(artifact.payload.get("anchor_ids", []))
            & target_anchor_ids
            or set(artifact.payload.get("material_ids", []))
            & target_material_ids
        )
    ]
    if not selected:
        target_text = " ".join(
            [
                hypothesis.question_intent,
                hypothesis.proposed_public_scope,
                *hypothesis.proposed_answer_values,
                *[
                    " ".join(
                        [
                            claim_by_id[claim_id].predicate,
                            claim_by_id[claim_id].object_or_value,
                            *claim_by_id[claim_id].qualifiers.values(),
                        ]
                    )
                    for claim_id in hypothesis.candidate_claim_ids
                ],
            ]
        )
        target_terms = _verification_terms(target_text)
        scored: list[tuple[int, str, ArtifactEnvelope]] = []
        for artifact in provider_results:
            payload = artifact.payload
            result_text = " ".join(
                [
                    str(payload.get("request", {}).get("query", "")),
                    str(
                        payload.get("usage", {}).get(
                            "search_rationale",
                            "",
                        )
                    ),
                    *[
                        str(content)
                        for content in payload.get(
                            "raw_content_by_material_id",
                            {},
                        ).values()
                    ],
                ]
            )
            score = len(
                target_terms & _verification_terms(result_text)
            )
            if score:
                scored.append(
                    (score, artifact.artifact_id, artifact)
                )
        selected = [
            item[2]
            for item in sorted(
                scored,
                key=lambda item: (-item[0], item[1]),
            )[:3]
        ]
    if not selected:
        raise RoleContractViolation(
            "no target-specific verification evidence"
        )
    return selected


def _verification_terms(value: str) -> set[str]:
    stopwords = {
        "and",
        "are",
        "does",
        "for",
        "from",
        "gene",
        "human",
        "identify",
        "into",
        "list",
        "specific",
        "that",
        "the",
        "which",
        "with",
    }
    terms: set[str] = set()
    for token in re.findall(
        r"[a-z0-9]+(?:[._-][a-z0-9]+)*",
        value.casefold(),
    ):
        terms.add(token)
        terms.update(
            part
            for part in re.split(r"[._-]+", token)
            if len(part) >= 3
        )
    return {
        term
        for term in terms
        if len(term) >= 2 and term not in stopwords
    }


def _advance_retrieval_streaks(
    terminal_status: ProviderTerminalStatus,
    *,
    has_materials: bool,
    empty_streak: int,
    provider_failure_streak: int,
) -> tuple[int, int]:
    if has_materials:
        return 0, 0
    if terminal_status in {
        ProviderTerminalStatus.FAILED,
        ProviderTerminalStatus.UNKNOWN,
    }:
        return 0, provider_failure_streak + 1
    return empty_streak + 1, 0


def _artifact_has_purpose(artifact: ArtifactEnvelope, purpose: str) -> bool:
    return (
        artifact.metadata.get("purpose") == purpose
        or purpose in artifact.metadata.get("purposes", [])
    )


def _completeness_basis_for_goal(
    goal: GenerationGoal,
) -> CompletenessBasis:
    identity_types = {
        "entity_identity",
        "gene_identity",
        "gene_location",
        "classification",
    }
    if goal.allowed_fact_types and set(goal.allowed_fact_types) <= identity_types:
        return CompletenessBasis.CANONICAL_IDENTITY
    return CompletenessBasis.AUTHORITATIVE_DEFINITION


def _claim_evidence_excerpts(
    claims: list[CandidateClaim],
    anchors: list[EvidenceAnchor],
    *,
    max_excerpts: int = 12,
    max_chars: int = 1800,
) -> list[dict[str, Any]]:
    anchor_by_id = {anchor.anchor_id: anchor for anchor in anchors}
    anchor_ids = list(
        dict.fromkeys(
            anchor_id
            for claim in claims
            for anchor_id in claim.supporting_anchor_ids
        )
    )
    excerpts = []
    for anchor_id in anchor_ids[:max_excerpts]:
        anchor = anchor_by_id.get(anchor_id)
        if anchor is None:
            continue
        text = anchor.quoted_text
        excerpts.append(
            {
                "anchor_id": anchor.anchor_id,
                "material_id": anchor.material_id,
                "document_id": anchor.document_id,
                "section_or_field": anchor.section_or_field,
                "quoted_text": (
                    text
                    if len(text) <= max_chars
                    else f"{text[:max_chars]}..."
                ),
            }
        )
    return excerpts


def _claim_evidence_groups(
    claims: list[CandidateClaim],
    anchors: list[EvidenceAnchor],
    *,
    max_excerpts_per_group: int = 8,
    max_chars: int = 1800,
) -> list[dict[str, Any]]:
    anchor_by_id = {anchor.anchor_id: anchor for anchor in anchors}
    grouped_claim_ids: dict[str, list[str]] = {}
    grouped_anchor_ids: dict[str, list[str]] = {}
    claim_by_id = {claim.claim_id: claim for claim in claims}
    for claim in claims:
        material_ids = []
        for anchor_id in claim.supporting_anchor_ids:
            anchor = anchor_by_id.get(anchor_id)
            if anchor is None:
                continue
            material_ids.append(anchor.material_id)
            grouped_anchor_ids.setdefault(anchor.material_id, []).append(
                anchor_id
            )
        for material_id in dict.fromkeys(material_ids):
            grouped_claim_ids.setdefault(material_id, []).append(
                claim.claim_id
            )
    groups = []
    for material_id in sorted(grouped_claim_ids):
        claim_ids = list(dict.fromkeys(grouped_claim_ids[material_id]))
        anchor_ids = list(
            dict.fromkeys(grouped_anchor_ids.get(material_id, []))
        )
        excerpts = []
        for anchor_id in anchor_ids[:max_excerpts_per_group]:
            anchor = anchor_by_id.get(anchor_id)
            if anchor is None:
                continue
            excerpts.append(
                {
                    "anchor_id": anchor.anchor_id,
                    "document_id": anchor.document_id,
                    "section_or_field": anchor.section_or_field,
                    "quoted_text": (
                        anchor.quoted_text
                        if len(anchor.quoted_text) <= max_chars
                        else f"{anchor.quoted_text[:max_chars]}..."
                    ),
                }
            )
        groups.append(
            {
                "group_id": (
                    "claim_group."
                    + canonical_digest(
                        {
                            "material_id": material_id,
                            "claim_ids": claim_ids,
                        }
                    )[:24]
                ),
                "material_id": material_id,
                "claim_ids": claim_ids,
                "claims": [
                    {
                        "claim_id": claim_id,
                        "subject": claim_by_id[claim_id].subject,
                        "predicate": claim_by_id[claim_id].predicate,
                        "object_or_value": (
                            claim_by_id[claim_id].object_or_value
                        ),
                        "claim_strength": (
                            claim_by_id[claim_id].claim_strength
                        ),
                    }
                    for claim_id in claim_ids
                ],
                "excerpts": excerpts,
            }
        )
    return groups


def _question_portfolio(
    snapshot: ObjectiveRuntimeSnapshot,
    hypotheses: list[QuestionHypothesis],
    claims: list[CandidateClaim],
    anchors: list[EvidenceAnchor],
) -> dict[str, Any]:
    redesign = {a.payload["hypothesis_id"]: a.payload["review"]["explanation"]
                for a in snapshot.artifacts.values() if a.artifact_type == "final_question_review"
                and not a.payload["review"]["question_is_self_contained"]
                and snapshot.hypotheses.get(a.payload["hypothesis_id"]) is HypothesisState.REJECTED}
    existing_questions = [
        {
            "hypothesis_id": hypothesis.hypothesis_id,
            "state": (
                snapshot.hypotheses[hypothesis.hypothesis_id].value
                if hypothesis.hypothesis_id in snapshot.hypotheses
                else None
            ),
            "question_intent": hypothesis.question_intent,
            **({"redesign_feedback": redesign[hypothesis.hypothesis_id]} if hypothesis.hypothesis_id in redesign else {}),
            "claim_ids": list(hypothesis.candidate_claim_ids),
            "answer_values": list(hypothesis.proposed_answer_values),
        }
        for hypothesis in hypotheses
    ]
    used_claim_ids = {
        claim_id
        for hypothesis in hypotheses
        for claim_id in hypothesis.candidate_claim_ids
    }
    anchor_by_id = {anchor.anchor_id: anchor for anchor in anchors}
    preferred_claim_ids = []
    for claim in claims:
        if claim.claim_id in used_claim_ids:
            continue
        supporting_anchors = [
            anchor_by_id[anchor_id]
            for anchor_id in claim.supporting_anchor_ids
            if anchor_id in anchor_by_id
        ]
        is_literature = any(
            anchor.anchor_id.startswith("anchor.pubmed.")
            for anchor in supporting_anchors
        )
        fact_text = (
            f"{claim.predicate} {claim.object_or_value}"
        ).casefold()
        is_information_bearing = any(
            term in fact_text
            for term in (
                "disease",
                "associat",
                "pathogenic",
                "variant",
                "function",
                "mechanism",
                "expression",
                "activation",
                "inhibit",
                "risk",
                "syndrome",
                "toxicity",
                "response",
                "survival",
                "phenotype",
            )
        )
        if is_literature or is_information_bearing:
            preferred_claim_ids.append(claim.claim_id)
    return {
        "existing_questions": existing_questions,
        "attempted_hypothesis_ids": sorted(snapshot.hypotheses),
        "used_claim_ids": sorted(used_claim_ids),
        "preferred_claim_ids": preferred_claim_ids[:12],
        "requirement": (
            "When preferred_claim_ids are present, at least one new "
            "hypothesis must use one of them."
        ),
    }


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


def _fact_claim_artifacts(
    snapshot: ObjectiveRuntimeSnapshot,
    claims: list[CandidateClaim],
    decision: MainAgentDecision,
) -> tuple[list[ArtifactEnvelope], list[str]]:
    artifacts: list[ArtifactEnvelope] = []
    claim_ids: list[str] = []
    for claim in claims:
        model_claim_id = claim.claim_id
        semantic_payload = claim.model_dump(
            mode="json",
            exclude={
                "claim_id",
                "status",
            },
        )
        for key in (
            "supporting_anchor_ids",
            "contradicting_anchor_ids",
        ):
            if isinstance(semantic_payload.get(key), list):
                semantic_payload[key] = sorted(semantic_payload[key])
        semantic = canonical_digest(semantic_payload)[:24]
        artifact_id = f"claim.{semantic}"
        suffix = 1
        while artifact_id in claim_ids:
            suffix += 1
            artifact_id = f"claim.{semantic}.{suffix}"
        claim = claim.model_copy(update={"claim_id": artifact_id})
        artifacts.append(
            _artifact(
                claim,
                "candidate_claim",
                decision,
                SubagentRole.FACT_UNDERSTANDING,
                metadata={"model_claim_id": model_claim_id},
            )
        )
        claim_ids.append(artifact_id)
    return artifacts, claim_ids


def _artifact_id(artifact_type: str, payload: dict[str, Any]) -> str:
    preferred_key = {
        "final_question_review": "artifact_id",
        "verification_gap": "artifact_id",
        "wording_repair_request": "artifact_id",
        "candidate_closure": "artifact_id",
        "release_evidence_gap": "artifact_id",
        "evidence_material": "material_id",
        "evidence_anchor": "anchor_id",
        "candidate_claim": "claim_id",
        "question_hypothesis": "hypothesis_id",
        "answer_resolution": "resolution_id",
        "frozen_question_contract": "contract_id",
        "objective_question_draft": "draft_id",
        "objective_validation_report": "report_id",
        "fact_understanding_batch": "batch_id",
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
