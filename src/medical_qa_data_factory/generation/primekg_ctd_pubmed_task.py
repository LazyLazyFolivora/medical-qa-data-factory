"""Compile PrimeKG/CTD/PubMed evidence into a complete publishable task."""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import cast

from pydantic import Field

from ..domain.common import ContractModel, utc_now
from ..evidence_enrichment.ctd import CtdEvidenceIndex, CtdMappingStatus
from ..evidence_enrichment.pubmed import PubMedClient
from ..knowledge_graph.contracts import (
    GraphEdge,
    GraphQueryResult,
    GraphQueryTrace,
    NeighborQuery,
)
from ..knowledge_graph.ports import KnowledgeGraphProvider
from .ctd_world_evaluation import UNKNOWN_OUTCOME_STATUSES
from .evidence_fusion import (
    CtdEvidenceFusion,
    CtdEvidenceFusionStatus,
    PubMedFindingVerdict,
)
from .logical_query import LogicalDirection, QueryStep
from .primekg_ctd_pubmed import (
    EvidenceKind,
    PrimeKgCtdDiseaseMapper,
    PrimeKgCtdPubMedResult,
    PrimeKgCtdPubMedService,
)
from .prompt_writing import (
    PromptQuestionSemanticVerifier,
    PromptQuestionWriter,
)
from .task_candidates import TaskCandidate
from .task_compiler import TaskCompiler
from .task_contracts import (
    AnswerCandidateOutcome,
    EvaluationTarget,
    PlannerQuestionInput,
    StudyContext,
    StudyRelationIntent,
    TaskSpec,
    ValidationEnvironment,
    ValidationReport,
    promote_validated_task,
)
from .task_service import (
    CtdEvidenceCatalogEntry,
    CtdGenerationEpisode,
    PublishedTask,
    TaskGenerationRejection,
    TaskGenerationResult,
)
from .task_validation import validation_report_id_for


class PrimeKgCtdPubmedTaskOutcome(ContractModel):
    """Evidence and competition view retained while building one task."""

    edge: GraphEdge
    evidence: PrimeKgCtdPubMedResult | None = None
    outcome: AnswerCandidateOutcome


class PrimeKGCtdPubmedTaskService:
    """Run a complete PrimeKG-first task generation attempt.

    Candidate discovery requires a ``source=CTD`` PrimeKG edge. Uniqueness is
    not decided from that edge: every PrimeKG answer in the same public query
    scope is assessed against CTD and all CTD-cited PubMed records.
    """

    version = "primekg-ctd-pubmed-task-service-v1"

    def __init__(
        self,
        *,
        provider: KnowledgeGraphProvider,
        ctd_index: CtdEvidenceIndex,
        pubmed: PubMedClient,
        disease_mapper: PrimeKgCtdDiseaseMapper,
        model,
        page_size: int = 500,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        self.provider = provider
        self.ctd_index = ctd_index
        self.disease_mapper = disease_mapper
        self.page_size = page_size
        self.model = model
        self.evidence_service = PrimeKgCtdPubMedService(
            ctd_index=ctd_index,
            disease_mapper=disease_mapper,
            pubmed=pubmed,
            model=model,
            primekg_edge_verifier=lambda edge: False,
        )
        self.compiler = TaskCompiler()
        self.writer = PromptQuestionWriter(model)
        self.text_verifier = PromptQuestionSemanticVerifier(model)

    def generate(
        self,
        *,
        edge: GraphEdge,
        evidence_kind: EvidenceKind,
        environment: ValidationEnvironment,
    ) -> TaskGenerationResult:
        if edge.provider_id != self.provider.provider_id:
            raise ValueError("seed edge belongs to a different provider")
        snapshot = self.provider.snapshot()
        if (
            snapshot.provider_id,
            snapshot.data_snapshot_id,
        ) not in {
            (item.provider_id, item.snapshot_id)
            for item in environment.providers
        }:
            raise ValueError("PrimeKG snapshot is outside the validation world")
        ctd_snapshot = self.ctd_index.metadata().get("snapshot_id")
        if not any(
            item.provider_id == "ctd"
            and item.snapshot_id == ctd_snapshot
            for item in environment.providers
        ):
            raise ValueError("CTD snapshot is outside the validation world")
        if not any(
            item.provider_id == "pubmed" for item in environment.providers
        ):
            raise ValueError("PubMed snapshot is outside the validation world")

        candidate_id = self._candidate_id(edge, evidence_kind, snapshot.data_snapshot_id)
        if str(edge.source or "").casefold() != "ctd":
            return self._rejected_result(
                edge=edge,
                candidate_id=candidate_id,
                environment=environment,
                stage="candidate_source",
                reasons=["source_not_ctd"],
            )

        try:
            graph_edges, traces = self._enumerate_scope(edge)
        except Exception:
            return self._rejected_result(
                edge=edge,
                candidate_id=candidate_id,
                environment=environment,
                stage="primekg_competition",
                reasons=["primekg_candidate_universe_incomplete"],
            )
        if not graph_edges:
            return self._rejected_result(
                edge=edge,
                candidate_id=candidate_id,
                environment=environment,
                stage="primekg_competition",
                reasons=["primekg_candidate_universe_incomplete"],
            )

        by_answer: dict[str, GraphEdge] = {}
        for candidate_edge in graph_edges:
            by_answer.setdefault(candidate_edge.tail.canonical_id, candidate_edge)
        if edge.tail.canonical_id not in by_answer:
            return self._rejected_result(
                edge=edge,
                candidate_id=candidate_id,
                environment=environment,
                stage="primekg_competition",
                reasons=["primekg_edge_mismatch"],
                traces=traces,
            )
        verified_candidate = next(
            (
                item
                for item in graph_edges
                if item.head.canonical_id == edge.head.canonical_id
                and item.tail.canonical_id == edge.tail.canonical_id
                and item.relation == edge.relation
                and str(item.source or "").casefold() == "ctd"
            ),
            None,
        )
        if verified_candidate is None:
            return self._rejected_result(
                edge=edge,
                candidate_id=candidate_id,
                environment=environment,
                stage="primekg_competition",
                reasons=["source_not_ctd"],
                traces=traces,
            )

        outcomes: list[PrimeKgCtdPubmedTaskOutcome] = []
        allowed_pubmed_snapshots = {
            item.snapshot_id
            for item in environment.providers
            if item.provider_id == "pubmed"
        }
        for answer_id in sorted(by_answer):
            candidate_edge = by_answer[answer_id]
            try:
                evidence = self.evidence_service.evaluate_answer(
                    seed_entity_id=edge.head.canonical_id,
                    answer_entity_id=answer_id,
                    subject_name=edge.head.name,
                    object_name=candidate_edge.tail.name,
                    evidence_kind=evidence_kind,
                    source_edge_id=candidate_edge.edge_id,
                    retrieved_at=utc_now(),
                )
            except Exception:
                evidence = None
                outcome = AnswerCandidateOutcome(
                    answer_id=answer_id,
                    status="provider_failed",
                    reason_codes=["primekg_ctd_pubmed_provider_failed"],
                )
            else:
                outcome = self._to_competitor_outcome(evidence)
                if (
                    evidence.pubmed_corpus is not None
                    and evidence.pubmed_corpus.snapshot_id
                    not in allowed_pubmed_snapshots
                ):
                    outcome = AnswerCandidateOutcome(
                        answer_id=answer_id,
                        status="provider_failed",
                        reason_codes=["pubmed_snapshot_mismatch"],
                    )
            outcomes.append(
                PrimeKgCtdPubmedTaskOutcome(
                    edge=candidate_edge,
                    evidence=evidence,
                    outcome=outcome,
                )
            )

        target = next(
            item for item in outcomes if item.edge.tail.canonical_id == edge.tail.canonical_id
        )
        if target.evidence is None or target.evidence.fusion is None:
            return self._rejected_result(
                edge=edge,
                candidate_id=candidate_id,
                environment=environment,
                stage="evidence_validation",
                reasons=(
                    [
                        *target.evidence.reason_codes,
                        "unknown_competitor",
                    ]
                    if target.evidence is not None
                    else [
                        *target.outcome.reason_codes,
                        "unknown_competitor",
                    ]
                ),
                traces=traces,
            )
        try:
            context = self._context_from_fusion(target.evidence.fusion)
            question_type = self._question_type(evidence_kind, context)
        except ValueError as exc:
            return self._rejected_result(
                edge=edge,
                candidate_id=candidate_id,
                environment=environment,
                stage="context_compilation",
                reasons=[str(exc)],
                traces=traces,
            )

        target_fusion = target.evidence.fusion
        assert target_fusion.claim is not None
        evidence_ids = sorted(
            {
                evidence_id
                for item in outcomes
                if item.evidence is not None
                and item.evidence.fusion is not None
                for evidence_id in (
                    item.evidence.fusion.supporting_evidence_ids
                    + item.evidence.fusion.contradicting_evidence_ids
                    + item.evidence.fusion.context_evidence_ids
                    + item.evidence.fusion.uncertain_evidence_ids
                )
            }
        )
        candidate = TaskCandidate(
            candidate_id=candidate_id,
            seed_entity_id=edge.head.canonical_id,
            question_type=question_type,
            steps=[
                QueryStep(
                    position=0,
                    relation=edge.relation,
                    direction=LogicalDirection.FORWARD,
                    target_entity_type="disease",
                )
            ],
            proposed_answer_ids=[edge.tail.canonical_id],
            private_answer_names=sorted(
                set([edge.tail.name, *edge.tail.aliases])
            ),
            answer_entity_type="disease",
            snapshot_id=snapshot.data_snapshot_id,
            evidence_ids=evidence_ids or target_fusion.supporting_evidence_ids,
            candidate_claim_ids=[target_fusion.claim.claim_id],
            public_study_context=context,
            public_entity_names={edge.head.canonical_id: edge.head.name},
            discovery_source="primekg_ctd_pubmed_competition",
            generator_version=self.version,
        )
        try:
            task = self.compiler.compile(candidate, environment)
        except ValueError as exc:
            return self._rejected_result(
                edge=edge,
                candidate_id=candidate_id,
                environment=environment,
                stage="task_compilation",
                reasons=[str(exc)],
                traces=traces,
                outcomes=outcomes,
            )

        reports: list[ValidationReport] = []
        report = self._validate(task, outcomes)
        reports.append(report)
        if not report.passed:
            return self._result(
                edge=edge,
                environment=environment,
                outcomes=outcomes,
                traces=traces,
                reports=reports,
                published=[],
                rejections=[
                    TaskGenerationRejection(
                        candidate_id=candidate_id,
                        stage="task_validation",
                        reason_codes=list(report.reason_codes),
                    )
                ],
                search_complete=report.result_complete,
            )

        validated = promote_validated_task(task, report)
        qid = f"question-{task.task_id}"
        try:
            variant = self.writer.write(validated, qid=qid)
        except Exception:
            return self._result(
                edge=edge,
                environment=environment,
                outcomes=outcomes,
                traces=traces,
                reports=reports,
                published=[],
                rejections=[
                    TaskGenerationRejection(
                        candidate_id=candidate_id,
                        stage="question_realization",
                        reason_codes=["question_writer_failed"],
                    )
                ],
                search_complete=True,
            )
        try:
            semantic = self.text_verifier.verify(
                variant.question_text, validated
            )
        except Exception:
            return self._result(
                edge=edge,
                environment=environment,
                outcomes=outcomes,
                traces=traces,
                reports=reports,
                published=[],
                rejections=[
                    TaskGenerationRejection(
                        candidate_id=candidate_id,
                        stage="text_semantic_verification",
                        reason_codes=["question_semantic_verifier_failed"],
                    )
                ],
                search_complete=True,
            )
        if not semantic.passed:
            return self._result(
                edge=edge,
                environment=environment,
                outcomes=outcomes,
                traces=traces,
                reports=reports,
                published=[],
                rejections=[
                    TaskGenerationRejection(
                        candidate_id=candidate_id,
                        stage="text_semantic_verification",
                        reason_codes=list(semantic.reason_codes),
                    )
                ],
                search_complete=True,
            )

        published = PublishedTask(
            planner_input=PlannerQuestionInput(
                qid=qid,
                question=variant.question_text,
                qtype=question_type,
                budget={"primekg_access": 8, "ctd_access": 8, "pubmed_access": 12},
                max_steps=8,
                sufficiency_threshold=1.0,
                environment_ref=environment.environment_id,
            ),
            evaluation_target=EvaluationTarget.from_validated_task(
                qid, validated
            ),
            validated_task=validated,
            validation_report=report,
            question_variant=variant,
            semantic_report=semantic,
        )
        return self._result(
            edge=edge,
            environment=environment,
            outcomes=outcomes,
            traces=traces,
            reports=reports,
            published=[published],
            rejections=[],
            search_complete=True,
        )

    def _enumerate_scope(
        self, edge: GraphEdge
    ) -> tuple[list[GraphEdge], list[GraphQueryTrace]]:
        cursor: str | None = None
        seen: dict[str, GraphEdge] = {}
        traces: list[GraphQueryTrace] = []
        while True:
            result: GraphQueryResult = self.provider.neighbors(
                NeighborQuery(
                    entity=edge.head,
                    direction="outgoing",
                    relations=[edge.relation],
                    target_types=["disease"],
                    limit=self.page_size,
                    cursor=cursor,
                )
            )
            current_snapshot = self.provider.snapshot()
            if (
                result.snapshot.provider_id != self.provider.provider_id
                or result.snapshot.data_snapshot_id
                != current_snapshot.data_snapshot_id
            ):
                raise RuntimeError(
                    "PrimeKG snapshot changed during candidate enumeration"
                )
            traces.append(result.trace)
            for candidate in result.edges:
                seen[candidate.edge_id] = candidate
            if not result.truncated:
                break
            if not result.next_cursor or result.next_cursor == cursor:
                raise RuntimeError(
                    "PrimeKG returned an incomplete page without progress"
                )
            cursor = result.next_cursor
        return (
            sorted(seen.values(), key=lambda item: item.edge_id),
            traces,
        )

    @staticmethod
    def _to_competitor_outcome(
        result: PrimeKgCtdPubMedResult,
    ) -> AnswerCandidateOutcome:
        reasons = set(result.reason_codes)
        if result.status == "accepted" and result.fusion is not None:
            fusion = result.fusion
            assert fusion.claim is not None
            return AnswerCandidateOutcome(
                answer_id=result.primekg_object_id,
                status="matched",
                claim_ids=[fusion.claim.claim_id],
                evidence_ids=list(fusion.claim.supporting_evidence_ids),
            )
        if result.mapping_status is CtdMappingStatus.AMBIGUOUS:
            status = "ambiguous"
        elif result.mapping_status is CtdMappingStatus.UNMAPPED:
            status = "not_evaluated"
        elif result.fusion is not None and (
            result.fusion.status is CtdEvidenceFusionStatus.CONFLICTING
        ):
            status = "ambiguous"
        elif any(
            reason.startswith("pubmed_")
            or reason
            in {
                "unknown_pmid_reference",
                "support_span_coverage_mismatch",
            }
            for reason in reasons
        ):
            status = "unreadable"
        elif reasons & {
            "ctd_direct_evidence_missing",
            "pubmed_document_missing",
            "claim_not_supported",
        }:
            status = "not_evaluated"
        elif reason := next(
            (
                reason
                for reason in reasons
                if "provider" in reason
                or reason.endswith("_unavailable")
                or reason.endswith("_failed")
            ),
            None,
        ):
            status = "provider_failed"
        else:
            status = "not_matched"
        return AnswerCandidateOutcome(
            answer_id=result.primekg_object_id,
            status=status,
            reason_codes=sorted(reasons),
        )

    @staticmethod
    def _context_from_fusion(fusion: CtdEvidenceFusion) -> StudyContext:
        support = [
            item
            for item in fusion.pubmed_assessments
            if item.verdict is PubMedFindingVerdict.SUPPORTS
            and item.intent is not None
        ]
        if not support or fusion.claim is None:
            raise ValueError("claim_not_supported")
        primary = sorted(support, key=lambda item: item.pmid)[0]
        assert primary.intent is not None
        qualifiers = primary.qualifiers
        if not qualifiers.disease_category:
            raise ValueError("missing_public_disambiguator")
        allowed_intents = {
            "association_research",
            "biomarker_research",
            "therapeutic_research",
            "risk_increase",
            "risk_decrease",
            "safety",
        }
        if primary.intent.value not in allowed_intents:
            raise ValueError("unsupported_study_intent")
        intent = cast(StudyRelationIntent, primary.intent.value)
        return StudyContext(
            population=qualifiers.population,
            exposure_context=qualifiers.exposure_context,
            dose_or_regimen=qualifiers.dose_exposure,
            exposure_duration=qualifiers.exposure_duration,
            study_type=qualifiers.study_type,
            sample_type=qualifiers.sample_type,
            disease_category=qualifiers.disease_category,
            temporal_scope=qualifiers.temporal_scope,
            relation_intent=intent,
            polarity=fusion.claim.polarity,
            additional_qualifiers=dict(qualifiers.additional_qualifiers),
        )

    @staticmethod
    def _question_type(
        evidence_kind: EvidenceKind,
        context: StudyContext,
    ) -> str:
        if (
            evidence_kind == "therapeutic"
            and context.relation_intent == "therapeutic_research"
        ):
            return "primekg_ctd_pubmed_therapeutic_research_disease"
        if (
            evidence_kind == "marker/mechanism"
            and context.relation_intent == "association_research"
        ):
            return "primekg_ctd_pubmed_association"
        if context.relation_intent in {
            "biomarker_research",
        }:
            raise ValueError("marker_mechanism_disease_answer_not_allowed")
        raise ValueError("claim_semantics_mismatch")

    def _validate(
        self,
        task: TaskSpec,
        evidence_outcomes: Sequence[PrimeKgCtdPubmedTaskOutcome],
    ) -> ValidationReport:
        competitor_outcomes = [item.outcome for item in evidence_outcomes]
        reasons: set[str] = set()
        result_complete = not any(
            item.status in UNKNOWN_OUTCOME_STATUSES
            for item in competitor_outcomes
        )
        if not result_complete:
            reasons.add("unknown_competitor")
        world_answers = sorted(
            item.answer_id
            for item in competitor_outcomes
            if item.status == "matched"
        )
        proposed = sorted(task.proposed_answer_contract.canonical_answer_ids)
        if len(world_answers) != 1:
            reasons.add(
                "world_answer_missing"
                if not world_answers
                else "world_cardinality_mismatch"
            )
        if result_complete and proposed != world_answers:
            reasons.add("candidate_answer_mismatch")

        matched = [
            item for item in competitor_outcomes if item.status == "matched"
        ]
        matched_claim_ids = sorted(
            {claim_id for item in matched for claim_id in item.claim_ids}
        )
        supporting_evidence_ids = sorted(
            {
                evidence_id
                for item in matched
                for evidence_id in item.evidence_ids
            }
        )
        if not matched_claim_ids or not supporting_evidence_ids:
            reasons.add("claim_not_supported")
            result_complete = False
        if not set(matched_claim_ids).issubset(
            task.candidate_claim_ids
        ) or not set(supporting_evidence_ids).issubset(
            task.candidate_evidence_ids
        ):
            reasons.add("evidence_dependency_mismatch")
            result_complete = False

        reason_codes = sorted(reasons)
        report_id = validation_report_id_for(
            task,
            validator_version=self.version,
            world_answer_ids=world_answers,
            result_complete=result_complete,
            reason_codes=reason_codes,
            rows_examined=len(competitor_outcomes),
            matched_claim_ids=matched_claim_ids,
            supporting_evidence_ids=supporting_evidence_ids,
            competitor_outcomes=competitor_outcomes,
            evidence_dependency_digest=task.evidence_dependency_digest(),
        )
        return ValidationReport(
            report_id=report_id,
            task_id=task.task_id,
            query_id=task.query.query_id,
            query_digest=task.query.dependency_digest,
            environment_id=task.validation_environment.environment_id,
            passed=not reason_codes,
            reason_codes=reason_codes,
            slice_answer_ids=[task.proposed_answer_contract.canonical_answer_ids[0]],
            world_answer_ids=world_answers,
            result_complete=result_complete,
            rows_examined=len(competitor_outcomes),
            matched_claim_ids=matched_claim_ids,
            supporting_evidence_ids=supporting_evidence_ids,
            competitor_outcomes=list(competitor_outcomes),
            evidence_dependency_digest=task.evidence_dependency_digest(),
            validator_version=self.version,
            validated_at=utc_now(),
        )

    def _result(
        self,
        *,
        edge: GraphEdge,
        environment: ValidationEnvironment,
        outcomes: Sequence[PrimeKgCtdPubmedTaskOutcome],
        traces: Sequence[GraphQueryTrace],
        reports: Sequence[ValidationReport],
        published: Sequence[PublishedTask],
        rejections: Sequence[TaskGenerationRejection],
        search_complete: bool,
    ) -> TaskGenerationResult:
        catalog = self._catalog(outcomes)
        ctd_snapshot = self.ctd_index.metadata().get("snapshot_id", "unknown")
        identity = hashlib.sha256(
            f"{edge.edge_id}|{environment.environment_id}".encode("utf-8")
        ).hexdigest()[:24]
        stop_reason = (
            "question_targets_met"
            if published
            else "required_searches_completed"
            if search_complete
            else "search_incomplete"
        )
        episode = CtdGenerationEpisode(
            episode_id=f"primekg-ctd-pubmed-episode-{identity}",
            seed_entity_id=edge.head.canonical_id,
            chemical_id=edge.head.native_id or edge.head.canonical_id.rsplit(":", 1)[-1],
            snapshot_id=ctd_snapshot,
            environment_id=environment.environment_id,
            candidate_count=len(outcomes),
            competitor_count=len(outcomes),
            published_count=len(published),
            search_complete=search_complete,
            stop_reason=stop_reason,
            evidence_gaps=sorted(
                {
                    reason
                    for item in outcomes
                    for reason in item.outcome.reason_codes
                }
            ),
            provenance={
                "service_version": self.version,
                "writer_version": self.writer.version,
                "verifier_version": self.text_verifier.version,
                "model_config_id": str(
                    getattr(
                        self.model,
                        "model_config_id",
                        getattr(
                            getattr(self.model, "config", None),
                            "config_hash",
                            "unknown",
                        ),
                    )
                ),
                "model_role_versions": (
                    "evidence_curator:curator-v2,"
                    "question_writer:writer-v2,"
                    "answer_verifier:verifier-v2"
                ),
            },
        )
        return TaskGenerationResult(
            seed_entity_id=edge.head.canonical_id,
            environment_id=environment.environment_id,
            candidates_seen=len(outcomes),
            published=list(published),
            rejections=list(rejections),
            validation_reports=list(reports),
            evidence_catalog=catalog,
            generation_traces=list(traces),
            episode=episode,
        )

    def _catalog(
        self,
        outcomes: Sequence[PrimeKgCtdPubmedTaskOutcome],
    ) -> list[CtdEvidenceCatalogEntry]:
        records = self.ctd_index.enumerate_diseases(
            chemical_id=(
                outcomes[0].edge.head.native_id
                or outcomes[0].edge.head.canonical_id.rsplit(":", 1)[-1]
            )
        ) if outcomes else []
        result_by_locator = {
            locator: item.evidence
            for item in outcomes
            if item.evidence is not None
            for locator in item.evidence.ctd_record_ids
        }
        catalog: dict[str, CtdEvidenceCatalogEntry] = {}
        for record in records:
            locator = record.source_record_locator or record.content_hash
            evidence = result_by_locator.get(locator)
            catalog[f"{record.source_snapshot_id}:{locator}"] = (
                CtdEvidenceCatalogEntry(
                    catalog_id=(
                        f"{record.source_snapshot_id}:{locator}"
                    ),
                    snapshot_id=record.source_snapshot_id,
                    record=record,
                    fusion=evidence.fusion if evidence else None,
                    primekg_subject_id=(
                        evidence.primekg_subject_id if evidence else None
                    ),
                    primekg_object_id=(
                        evidence.primekg_object_id if evidence else None
                    ),
                    mapping_status=(
                        evidence.mapping_status if evidence else None
                    ),
                    pubmed_corpus=(
                        evidence.pubmed_corpus if evidence else None
                    ),
                    competitor_outcomes=[
                        item.outcome
                        for item in outcomes
                        if item.evidence is evidence
                    ],
                )
            )
        return [catalog[key] for key in sorted(catalog)]

    def _rejected_result(
        self,
        *,
        edge: GraphEdge,
        candidate_id: str,
        environment: ValidationEnvironment,
        stage: str,
        reasons: Sequence[str],
        traces: Sequence[GraphQueryTrace] = (),
        outcomes: Sequence[PrimeKgCtdPubmedTaskOutcome] = (),
    ) -> TaskGenerationResult:
        return self._result(
            edge=edge,
            environment=environment,
            outcomes=outcomes,
            traces=traces,
            reports=[],
            published=[],
            rejections=[
                TaskGenerationRejection(
                    candidate_id=candidate_id,
                    stage=stage,
                    reason_codes=sorted(set(reasons)) or ["generation_rejected"],
                )
            ],
            search_complete=False,
        )

    def _candidate_id(
        self,
        edge: GraphEdge,
        evidence_kind: EvidenceKind,
        snapshot_id: str,
    ) -> str:
        digest = hashlib.sha256(
            "|".join(
                (
                    snapshot_id,
                    edge.edge_id,
                    evidence_kind,
                    self.version,
                )
            ).encode("utf-8")
        ).hexdigest()[:24]
        return f"candidate-primekg-ctd-pubmed-{digest}"
