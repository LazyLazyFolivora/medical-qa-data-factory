"""Validated task services for PrimeKG and CTD-backed questions."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import Field, model_validator

from ..domain.common import ContractModel
from ..evidence_enrichment.models import CtdEvidenceRecord
from ..evidence_enrichment.ctd import CtdMappingStatus
from ..evidence_enrichment.pubmed_evidence import PubMedCorpusManifest
from ..knowledge_graph.contracts import (
    GraphEdge,
    GraphEntity,
    GraphQueryTrace,
    NeighborQuery,
)
from ..knowledge_graph.ports import KnowledgeGraphProvider
from .evidence_fusion import CtdEvidenceFusion
from .logical_query import LogicalDirection
from .ctd_world_evaluation import (
    CtdWorldEvaluator,
    FusionCtdCandidateAssessor,
    StaticCtdDiseaseCategoryResolver,
    StaticCtdDiseaseIdentityResolver,
)
from .task_candidates import (
    ConstrainedIntersectionCandidateSource,
    CtdCandidateProposal,
    CtdStudyContextCandidateSource,
)
from .task_compiler import TaskCompiler
from .task_contracts import (
    AnswerCandidateOutcome,
    EvaluationTarget,
    PlannerQuestionInput,
    StudyContext,
    ValidatedTask,
    ValidationEnvironment,
    ValidationReport,
    promote_validated_task,
)
from .task_realization import (
    CtdTaskQuestionWriter,
    CtdTaskTextSemanticVerifier,
    QuestionVariant,
    TaskQuestionWriter,
    TaskTextSemanticVerifier,
    TextSemanticReport,
)
from .task_validation import (
    CtdTaskValidator,
    TaskValidator,
    validation_report_id_for,
)
from .world_evaluation import PrimeKGWorldEvaluator


@dataclass(frozen=True)
class RelationPairProfile:
    """One reviewed relation composition eligible for candidate discovery."""

    primary_relation: str
    primary_direction: LogicalDirection
    constraint_relation: str
    constraint_direction: LogicalDirection
    primary_source: str | None = None


@dataclass(frozen=True)
class _NeighborScan:
    edges: list[GraphEdge]
    traces: list[GraphQueryTrace]
    trace_ids_by_edge: dict[str, set[str]]


DEFAULT_RELATION_PROFILES = (
    # Deterministic test/adapter vocabulary.
    RelationPairProfile(
        "targets",
        LogicalDirection.FORWARD,
        "associated_with",
        LogicalDirection.FORWARD,
    ),
    # Real PrimeKG: disease -> protein and pathway -> protein.  Starting from
    # a disease, the answer is a protein satisfying a pathway constraint.
    RelationPairProfile(
        "disease_protein",
        LogicalDirection.FORWARD,
        "pathway_protein",
        LogicalDirection.REVERSE,
    ),
)

CTD_RELATION_PROFILE = RelationPairProfile(
    "exposure_disease",
    LogicalDirection.FORWARD,
    "disease_protein",
    LogicalDirection.REVERSE,
    primary_source="CTD",
)


class PublishedTask(ContractModel):
    """All public and hidden artifacts for one publishable question."""

    planner_input: PlannerQuestionInput
    evaluation_target: EvaluationTarget
    validated_task: ValidatedTask
    validation_report: ValidationReport
    question_variant: QuestionVariant
    semantic_report: TextSemanticReport

    @model_validator(mode="after")
    def publication_gates_are_closed(self) -> "PublishedTask":
        expected_report_id = validation_report_id_for(
            self.validated_task.task,
            validator_version=self.validation_report.validator_version,
            world_answer_ids=self.validation_report.world_answer_ids,
            result_complete=self.validation_report.result_complete,
            reason_codes=self.validation_report.reason_codes,
            rows_examined=self.validation_report.rows_examined,
            matched_claim_ids=self.validation_report.matched_claim_ids,
            supporting_evidence_ids=(
                self.validation_report.supporting_evidence_ids
            ),
            competitor_outcomes=self.validation_report.competitor_outcomes,
            evidence_dependency_digest=(
                self.validation_report.evidence_dependency_digest
            ),
        )
        if self.validation_report.report_id != expected_report_id:
            raise ValueError("validation report content identity is invalid")
        promoted = promote_validated_task(
            self.validated_task.task, self.validation_report
        )
        if promoted != self.validated_task:
            raise ValueError("validated task does not match its validation report")
        if not self.semantic_report.passed:
            raise ValueError("question text did not pass semantic verification")
        if (
            self.semantic_report.reconstructed_query_digest
            != self.validated_task.task.query.dependency_digest
        ):
            raise ValueError("question text is not bound to the validated query")
        return self


class TaskGenerationRejection(ContractModel):
    candidate_id: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)


class EvidenceCatalogEntry(ContractModel):
    """A discovery fact retained separately from Planner-visible inputs."""

    snapshot_id: str = Field(min_length=1)
    edge: GraphEdge
    generation_trace_ids: list[str] = Field(min_length=1)


class CtdEvidenceCatalogEntry(ContractModel):
    catalog_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    record: CtdEvidenceRecord
    fusion: CtdEvidenceFusion | None = None
    primekg_subject_id: str | None = None
    primekg_object_id: str | None = None
    mapping_status: CtdMappingStatus | None = None
    pubmed_corpus: PubMedCorpusManifest | None = None
    competitor_outcomes: list[AnswerCandidateOutcome] = Field(
        default_factory=list
    )


class CtdGenerationEpisode(ContractModel):
    episode_id: str = Field(min_length=1)
    seed_entity_id: str = Field(min_length=1)
    chemical_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    environment_id: str = Field(min_length=1)
    candidate_count: int = Field(ge=0)
    competitor_count: int = Field(ge=0)
    published_count: int = Field(ge=0)
    search_complete: bool
    stop_reason: str = Field(min_length=1)
    evidence_gaps: list[str] = Field(default_factory=list)
    provenance: dict[str, str] = Field(default_factory=dict)


class CtdTaskRunSpec(ContractModel):
    """Pinned inputs for one formal CTD task-generation run."""

    seed_entity_id: str = Field(min_length=1)
    chemical_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    context: StudyContext
    identity_mapping: dict[str, str | list[str]]
    category_mapping: dict[str, list[str]]
    fusions: dict[str, CtdEvidenceFusion] = Field(default_factory=dict)
    provider_failures: dict[str, str] = Field(default_factory=dict)
    environment: ValidationEnvironment
    model_config_id: str = "unknown"
    prompt_version: str = "curator-v2"
    code_commit: str = "unknown"


class TaskGenerationResult(ContractModel):
    seed_entity_id: str = Field(min_length=1)
    environment_id: str = Field(min_length=1)
    candidates_seen: int = Field(ge=0)
    published: list[PublishedTask] = Field(default_factory=list)
    rejections: list[TaskGenerationRejection] = Field(default_factory=list)
    validation_reports: list[ValidationReport] = Field(default_factory=list)
    evidence_catalog: list[
        EvidenceCatalogEntry | CtdEvidenceCatalogEntry
    ] = Field(default_factory=list)
    generation_traces: list[GraphQueryTrace] = Field(default_factory=list)
    episode: CtdGenerationEpisode | None = None


class ConstrainedTaskGenerationService:
    """Generate the first production slice from actual provider edges.

    The service evaluates an allow-list of reviewed relation compositions and
    validates each corresponding intersection against the entire pinned
    snapshot. Discovery never establishes uniqueness.
    """

    def __init__(
        self,
        provider: KnowledgeGraphProvider,
        *,
        page_size: int = 500,
        relation_profiles: tuple[RelationPairProfile, ...] = DEFAULT_RELATION_PROFILES,
    ) -> None:
        self.provider = provider
        self.page_size = page_size
        self.relation_profiles = relation_profiles
        self.candidate_source = ConstrainedIntersectionCandidateSource()
        self.compiler = TaskCompiler()
        self.validator = TaskValidator(
            PrimeKGWorldEvaluator(provider, page_size=page_size)
        )
        self.writer = TaskQuestionWriter()
        self.text_verifier = TaskTextSemanticVerifier()

    def generate(
        self,
        seed: GraphEntity,
        *,
        environment: ValidationEnvironment,
    ) -> TaskGenerationResult:
        snapshot = self.provider.snapshot()
        snapshot_id = snapshot.data_snapshot_id
        if seed.provider_id != self.provider.provider_id:
            raise ValueError("seed entity belongs to a different provider")
        if (snapshot.provider_id, snapshot_id) not in {
            (item.provider_id, item.snapshot_id)
            for item in environment.providers
        }:
            raise ValueError("provider snapshot is outside the validation world")

        published: list[PublishedTask] = []
        rejections: list[TaskGenerationRejection] = []
        validation_reports: list[ValidationReport] = []
        generation_traces: dict[str, GraphQueryTrace] = {}
        evidence_edges: dict[str, GraphEdge] = {}
        evidence_trace_ids: dict[str, set[str]] = {}
        seen_signatures: set[str] = set()
        candidates_seen = 0

        for profile in self.relation_profiles:
            primary_scan = self._all_neighbors(
                seed,
                relation=profile.primary_relation,
                direction=profile.primary_direction,
            )
            self._record_scan(
                primary_scan,
                traces=generation_traces,
                edges=evidence_edges,
                trace_ids=evidence_trace_ids,
            )
            primary_edges = primary_scan.edges
            if profile.primary_source is not None:
                primary_edges = [
                    edge
                    for edge in primary_edges
                    if edge.source == profile.primary_source
                ]
            for primary_edge in primary_edges:
                answer_entity = (
                    primary_edge.tail
                    if profile.primary_direction is LogicalDirection.FORWARD
                    else primary_edge.head
                )
                constraint_scan = self._all_neighbors(
                    answer_entity,
                    relation=profile.constraint_relation,
                    direction=profile.constraint_direction,
                )
                self._record_scan(
                    constraint_scan,
                    traces=generation_traces,
                    edges=evidence_edges,
                    trace_ids=evidence_trace_ids,
                )
                for constraint_edge in constraint_scan.edges:
                    candidate = self.candidate_source.from_edges(
                        seed_edge=primary_edge,
                        constraint_edge=constraint_edge,
                        snapshot_id=snapshot_id,
                        primary_direction=profile.primary_direction,
                        constraint_direction=profile.constraint_direction,
                    )
                    signature = candidate.semantic_digest()
                    if signature in seen_signatures:
                        continue
                    seen_signatures.add(signature)
                    candidates_seen += 1
                    task = self.compiler.compile(candidate, environment)
                    report = self.validator.validate(
                        task,
                        slice_answer_ids=candidate.proposed_answer_ids,
                    )
                    validation_reports.append(report)
                    if not report.passed:
                        rejections.append(
                            TaskGenerationRejection(
                                candidate_id=candidate.candidate_id,
                                stage="task_validation",
                                reason_codes=list(report.reason_codes),
                            )
                        )
                        continue

                    validated = promote_validated_task(task, report)
                    qid = f"question-{task.task_id}"
                    variant = self.writer.write(validated, qid=qid)
                    semantic_report = self.text_verifier.verify(
                        variant,
                        validated,
                    )
                    if not semantic_report.passed:
                        rejections.append(
                            TaskGenerationRejection(
                                candidate_id=candidate.candidate_id,
                                stage="text_semantic_verification",
                                reason_codes=list(semantic_report.reason_codes),
                            )
                        )
                        continue

                    planner = PlannerQuestionInput(
                        qid=qid,
                        question=variant.question_text,
                        qtype=task.question_type,
                        budget={"relation_access": 8, "tool_calls": 12},
                        max_steps=8,
                        sufficiency_threshold=1.0,
                        environment_ref=environment.environment_id,
                    )
                    published.append(
                        PublishedTask(
                            planner_input=planner,
                            evaluation_target=EvaluationTarget.from_validated_task(
                                qid, validated
                            ),
                            validated_task=validated,
                            validation_report=report,
                            question_variant=variant,
                            semantic_report=semantic_report,
                        ),
                    )

        return TaskGenerationResult(
            seed_entity_id=seed.canonical_id,
            environment_id=environment.environment_id,
            candidates_seen=candidates_seen,
            published=published,
            rejections=rejections,
            validation_reports=validation_reports,
            evidence_catalog=[
                EvidenceCatalogEntry(
                    snapshot_id=snapshot_id,
                    edge=evidence_edges[edge_id],
                    generation_trace_ids=sorted(evidence_trace_ids[edge_id]),
                )
                for edge_id in sorted(evidence_edges)
            ],
            generation_traces=[
                generation_traces[trace_id]
                for trace_id in sorted(generation_traces)
            ],
        )

    def _all_neighbors(
        self,
        entity: GraphEntity,
        *,
        relation: str,
        direction: LogicalDirection,
    ) -> _NeighborScan:
        cursor: str | None = None
        edges: dict[str, GraphEdge] = {}
        traces: list[GraphQueryTrace] = []
        trace_ids_by_edge: dict[str, set[str]] = {}
        while True:
            result = self.provider.neighbors(
                NeighborQuery(
                    entity=entity,
                    direction=(
                        "outgoing"
                        if direction is LogicalDirection.FORWARD
                        else "incoming"
                    ),
                    relations=[relation],
                    limit=self.page_size,
                    cursor=cursor,
                )
            )
            traces.append(result.trace)
            for edge in result.edges:
                edges[edge.edge_id] = edge
                trace_ids_by_edge.setdefault(edge.edge_id, set()).add(
                    result.trace.query_id
                )
            if not result.truncated:
                break
            if not result.next_cursor or result.next_cursor == cursor:
                raise RuntimeError(
                    "provider returned an incomplete page without progress"
                )
            cursor = result.next_cursor
        return _NeighborScan(
            edges=sorted(edges.values(), key=lambda item: item.edge_id),
            traces=traces,
            trace_ids_by_edge=trace_ids_by_edge,
        )

    @staticmethod
    def _record_scan(
        scan: _NeighborScan,
        *,
        traces: dict[str, GraphQueryTrace],
        edges: dict[str, GraphEdge],
        trace_ids: dict[str, set[str]],
    ) -> None:
        for trace in scan.traces:
            traces[trace.query_id] = trace
        for edge in scan.edges:
            edges[edge.edge_id] = edge
            trace_ids.setdefault(edge.edge_id, set()).update(
                scan.trace_ids_by_edge[edge.edge_id]
            )


class CtdTaskGenerationService:
    """Assemble the formal CTD task path into one validated release result."""

    version = "ctd-task-generation-service-v1"

    def __init__(
        self,
        *,
        candidate_source: CtdStudyContextCandidateSource,
        compiler: TaskCompiler,
        validator: CtdTaskValidator,
        evidence_by_record: dict[str, CtdEvidenceFusion],
        writer: CtdTaskQuestionWriter | None = None,
        text_verifier: CtdTaskTextSemanticVerifier | None = None,
        provenance: dict[str, str] | None = None,
    ) -> None:
        self.candidate_source = candidate_source
        self.compiler = compiler
        self.validator = validator
        self.evidence_by_record = dict(evidence_by_record)
        self.writer = writer or CtdTaskQuestionWriter()
        self.text_verifier = (
            text_verifier or CtdTaskTextSemanticVerifier()
        )
        self.provenance = dict(provenance or {})

    def generate(
        self,
        *,
        seed_entity_id: str,
        chemical_id: str,
        snapshot_id: str,
        context: StudyContext,
        environment: ValidationEnvironment,
    ) -> TaskGenerationResult:
        proposal = self.candidate_source.propose(
            seed_entity_id=seed_entity_id,
            chemical_id=chemical_id,
            snapshot_id=snapshot_id,
            context=context,
        )
        evaluation = proposal.evaluation
        rejections: list[TaskGenerationRejection] = []
        reports: list[ValidationReport] = []
        published: list[PublishedTask] = []
        if proposal.candidate is None:
            rejections.append(
                TaskGenerationRejection(
                    candidate_id=self._rejected_candidate_id(
                        snapshot_id,
                        chemical_id,
                        context,
                    ),
                    stage="candidate_source",
                    reason_codes=list(proposal.reason_codes),
                )
            )
        else:
            candidate = proposal.candidate
            try:
                task = self.compiler.compile(candidate, environment)
            except ValueError as exc:
                rejections.append(
                    TaskGenerationRejection(
                        candidate_id=candidate.candidate_id,
                        stage="task_compilation",
                        reason_codes=[str(exc)],
                    )
                )
            else:
                report = self.validator.validate(
                    task,
                    slice_answer_ids=candidate.proposed_answer_ids,
                )
                reports.append(report)
                if not report.passed:
                    rejections.append(
                        TaskGenerationRejection(
                            candidate_id=candidate.candidate_id,
                            stage="task_validation",
                            reason_codes=list(report.reason_codes),
                        )
                    )
                else:
                    validated = promote_validated_task(task, report)
                    qid = f"question-{task.task_id}"
                    try:
                        variant = self.writer.write(validated, qid=qid)
                    except ValueError as exc:
                        rejections.append(
                            TaskGenerationRejection(
                                candidate_id=candidate.candidate_id,
                                stage="question_realization",
                                reason_codes=[str(exc)],
                            )
                        )
                    else:
                        semantic = self.text_verifier.verify(
                            variant,
                            validated,
                        )
                        if not semantic.passed:
                            rejections.append(
                                TaskGenerationRejection(
                                    candidate_id=candidate.candidate_id,
                                    stage="text_semantic_verification",
                                    reason_codes=list(
                                        semantic.reason_codes
                                    ),
                                )
                            )
                        else:
                            planner = PlannerQuestionInput(
                                qid=qid,
                                question=variant.question_text,
                                qtype=task.question_type,
                                budget={
                                    "ctd_access": 8,
                                    "pubmed_access": 12,
                                },
                                max_steps=8,
                                sufficiency_threshold=1.0,
                                environment_ref=environment.environment_id,
                            )
                            published.append(
                                PublishedTask(
                                    planner_input=planner,
                                    evaluation_target=(
                                        EvaluationTarget.from_validated_task(
                                            qid,
                                            validated,
                                        )
                                    ),
                                    validated_task=validated,
                                    validation_report=report,
                                    question_variant=variant,
                                    semantic_report=semantic,
                                )
                            )

        episode = self._episode(
            seed_entity_id=seed_entity_id,
            chemical_id=chemical_id,
            snapshot_id=snapshot_id,
            environment=environment,
            proposal=proposal,
            published_count=len(published),
            provenance={
                "service_version": self.version,
                "validator_version": self.validator.version,
                "writer_version": self.writer.version,
                "verifier_version": self.text_verifier.version,
                "task_schema_version": "task-construction-v2",
                "pubmed_snapshots": ",".join(
                    sorted(
                        {
                            assessment.span.snapshot_id
                            for fusion in self.evidence_by_record.values()
                            for assessment in fusion.pubmed_assessments
                        }
                    )
                ),
                "normalizer_versions": ",".join(
                    sorted(
                        {
                            fusion.claim.normalizer_version
                            for fusion in self.evidence_by_record.values()
                            if fusion.claim is not None
                        }
                    )
                ),
                **self.provenance,
            },
        )
        catalog = []
        for answer_id in sorted(evaluation.answer_records):
            for record in evaluation.answer_records[answer_id]:
                locator = record.source_record_locator or record.content_hash
                catalog.append(
                    CtdEvidenceCatalogEntry(
                        catalog_id=(
                            f"{record.source_snapshot_id}:{locator}"
                        ),
                        snapshot_id=record.source_snapshot_id,
                        record=record,
                        fusion=self.evidence_by_record.get(locator),
                    )
                )
        return TaskGenerationResult(
            seed_entity_id=seed_entity_id,
            environment_id=environment.environment_id,
            candidates_seen=(1 if proposal.candidate is not None else 0),
            published=published,
            rejections=rejections,
            validation_reports=reports,
            evidence_catalog=sorted(
                catalog,
                key=lambda item: item.catalog_id,
            ),
            generation_traces=[],
            episode=episode,
        )

    @staticmethod
    def _rejected_candidate_id(
        snapshot_id: str,
        chemical_id: str,
        context: StudyContext,
    ) -> str:
        digest = hashlib.sha256(
            json.dumps(
                {
                    "snapshot_id": snapshot_id,
                    "chemical_id": chemical_id,
                    "context": context.model_dump(mode="json"),
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:24]
        return f"candidate-ctd-rejected-{digest}"

    @staticmethod
    def _episode(
        *,
        seed_entity_id: str,
        chemical_id: str,
        snapshot_id: str,
        environment: ValidationEnvironment,
        proposal: CtdCandidateProposal,
        published_count: int,
        provenance: dict[str, str],
    ) -> CtdGenerationEpisode:
        evaluation = proposal.evaluation
        if any(
            item.status == "provider_failed"
            for item in evaluation.competitor_outcomes
        ) or "ctd_provider_unavailable" in evaluation.reason_codes:
            stop_reason = "provider_unavailable"
        elif not evaluation.result_complete:
            stop_reason = "search_incomplete"
        elif published_count:
            stop_reason = "question_targets_met"
        else:
            stop_reason = "required_searches_completed"
        identity = hashlib.sha256(
            json.dumps(
                {
                    "seed_entity_id": seed_entity_id,
                    "chemical_id": chemical_id,
                    "snapshot_id": snapshot_id,
                    "environment_id": environment.environment_id,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:24]
        return CtdGenerationEpisode(
            episode_id=f"ctd-episode-{identity}",
            seed_entity_id=seed_entity_id,
            chemical_id=chemical_id,
            snapshot_id=snapshot_id,
            environment_id=environment.environment_id,
            candidate_count=1 if proposal.candidate is not None else 0,
            competitor_count=len(evaluation.competitor_outcomes),
            published_count=published_count,
            search_complete=evaluation.result_complete,
            stop_reason=stop_reason,
            evidence_gaps=sorted(
                {
                    *evaluation.reason_codes,
                    *proposal.reason_codes,
                }
            ),
            provenance=dict(sorted(provenance.items())),
        )
