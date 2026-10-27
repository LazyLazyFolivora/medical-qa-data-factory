"""Generate questions from CTD direct evidence without a disease crosswalk."""
from __future__ import annotations

import hashlib
from typing import Literal

from ..domain.common import utc_now
from ..evidence_enrichment.ctd import CtdEvidenceIndex
from ..evidence_enrichment.contracts import (
    EvidenceRecord,
    NormalizedClaim,
    evidence_from_ctd,
)
from ..evidence_enrichment.models import CtdEvidenceRecord
from .logical_query import LogicalDirection, LogicalQuery, QueryStep
from .task_contracts import (
    AcceptableEvidenceSet,
    AnswerCandidateOutcome,
    AnswerContract,
    ClaimPattern,
    CompletionRule,
    EvaluationTarget,
    InformationRequirement,
    PlannerQuestionInput,
    ProviderSnapshotRef,
    TaskCandidate,
    TaskSpec,
    ValidationEnvironment,
    ValidationReport,
    promote_validated_task,
)
from .task_realization import (
    QuestionVariant,
    TextSemanticReport,
)
from .task_service import (
    CtdEvidenceCatalogEntry,
    CtdGenerationEpisode,
    PublishedTask,
    TaskGenerationRejection,
    TaskGenerationResult,
)
from .task_validation import validation_report_id_for


CtdDirectEvidenceKind = Literal["marker/mechanism", "therapeutic"]


class CtdDirectEvidenceService:
    """Build a CTD-backed direct-evidence entity question.

    PrimeKG is intentionally absent from this answer world. A discovered
    CTD ChemicalID is enough to enumerate every CTD disease record and decide
    the complete direct-evidence competitor set.
    """

    version = "ctd-direct-evidence-service-v1"

    def __init__(self, index: CtdEvidenceIndex) -> None:
        self.index = index

    def generate(
        self,
        *,
        chemical_id: str,
        evidence_kind: CtdDirectEvidenceKind,
    ) -> TaskGenerationResult:
        if evidence_kind not in {"marker/mechanism", "therapeutic"}:
            raise ValueError(f"unsupported CTD evidence kind: {evidence_kind}")
        records = self.index.enumerate_diseases(chemical_id=chemical_id)
        if not records:
            raise ValueError(f"CTD chemical not found: {chemical_id}")
        snapshot_id = self.index.metadata()["snapshot_id"]
        environment = self._environment(snapshot_id)
        outcomes: list[AnswerCandidateOutcome] = []
        evidence_records: list[EvidenceRecord] = []
        claims: list[NormalizedClaim] = []
        matched_records: dict[str, list[CtdEvidenceRecord]] = {}
        complete = True
        for record in records:
            if record.disease_id is None:
                complete = False
                outcomes.append(
                    AnswerCandidateOutcome(
                        answer_id=self._unknown_answer_id(record),
                        status="not_evaluated",
                        reason_codes=["ctd_record_unmapped"],
                    )
                )
                continue
            answer_id = self._disease_id(record.disease_id)
            if evidence_kind not in record.direct_evidence:
                reason = (
                    "inference_only"
                    if not record.direct_evidence
                    else "direct_evidence_kind_mismatch"
                )
                outcomes.append(
                    AnswerCandidateOutcome(
                        answer_id=answer_id,
                        status="not_matched",
                        reason_codes=[reason],
                    )
                )
                continue
            if not record.pubmed_ids:
                complete = False
                outcomes.append(
                    AnswerCandidateOutcome(
                        answer_id=answer_id,
                        status="not_evaluated",
                        reason_codes=["pubmed_record_unreadable"],
                    )
                )
                continue
            evidence = evidence_from_ctd(
                record,
                subject_entity_id=self._chemical_id(chemical_id),
                object_entity_id=answer_id,
                retrieved_at=utc_now(),
            )
            claim = NormalizedClaim.create(
                subject_id=self._chemical_id(chemical_id),
                predicate_family=self._predicate_family(evidence_kind),
                object_id=answer_id,
                qualifiers={
                    "direct_evidence": evidence_kind,
                    "ctd_snapshot_id": snapshot_id,
                },
                polarity="neutral",
                normalizer_version=self.version,
                supporting_evidence_ids=[evidence.evidence_id],
                claim_text=(
                    f"{record.chemical_name} has {evidence_kind} evidence "
                    f"for {record.disease_name}"
                ),
            )
            evidence_records.append(evidence)
            claims.append(claim)
            matched_records.setdefault(answer_id, []).append(record)
            outcomes.append(
                AnswerCandidateOutcome(
                    answer_id=answer_id,
                    status="matched",
                    claim_ids=[claim.claim_id],
                    evidence_ids=[evidence.evidence_id],
                )
            )

        world_answers = sorted(
            item.answer_id for item in outcomes if item.status == "matched"
        )
        rejections: list[TaskGenerationRejection] = []
        published: list[PublishedTask] = []
        reports: list[ValidationReport] = []
        candidate_id = self._candidate_id(
            chemical_id,
            evidence_kind,
            snapshot_id,
        )
        if not complete:
            reasons = ["unknown_competitor"]
        elif not world_answers:
            reasons = ["world_answer_missing"]
        elif len(world_answers) != 1:
            reasons = ["world_cardinality_mismatch"]
        else:
            reasons = []

        if reasons:
            rejections.append(
                TaskGenerationRejection(
                    candidate_id=candidate_id,
                    stage="ctd_direct_evidence",
                    reason_codes=reasons,
                )
            )
        else:
            answer_id = world_answers[0]
            record = matched_records[answer_id][0]
            claim = next(
                item
                for item in claims
                if item.object_id == answer_id
            )
            candidate = self._candidate(
                candidate_id=candidate_id,
                chemical_id=chemical_id,
                chemical_name=record.chemical_name,
                evidence_kind=evidence_kind,
                answer_id=answer_id,
                record=record,
                claim=claim,
                snapshot_id=snapshot_id,
            )
            task = self._task(
                candidate=candidate,
                environment=environment,
                evidence_kind=evidence_kind,
                answer_id=answer_id,
                claim=claim,
            )
            report = self._report(
                task=task,
                outcomes=outcomes,
                world_answers=world_answers,
                claim_ids=[claim.claim_id],
                evidence_ids=list(claim.supporting_evidence_ids),
                complete=complete,
            )
            reports.append(report)
            validated = promote_validated_task(task, report)
            qid = f"question-{task.task_id}"
            variant = self._question(
                chemical_name=record.chemical_name,
                evidence_kind=evidence_kind,
                qid=qid,
                task_id=task.task_id,
            )
            semantic = self._verify(
                variant=variant,
                task=task,
                answer_name=record.disease_name,
                chemical_name=record.chemical_name,
            )
            planner = PlannerQuestionInput(
                qid=qid,
                question=variant.question_text,
                qtype=task.question_type,
                budget={"ctd_access": 4},
                max_steps=4,
                sufficiency_threshold=1.0,
                environment_ref=environment.environment_id,
            )
            published.append(
                PublishedTask(
                    planner_input=planner,
                    evaluation_target=EvaluationTarget.from_validated_task(
                        qid,
                        validated,
                    ),
                    validated_task=validated,
                    validation_report=report,
                    question_variant=variant,
                    semantic_report=semantic,
                )
            )

        catalog = [
            CtdEvidenceCatalogEntry(
                catalog_id=(
                    f"{record.source_snapshot_id}:"
                    f"{record.source_record_locator or record.content_hash}"
                ),
                snapshot_id=record.source_snapshot_id,
                record=record,
            )
            for record in records
        ]
        episode_id = "ctd-direct-" + hashlib.sha256(
            f"{snapshot_id}|{chemical_id}|{evidence_kind}".encode("utf-8")
        ).hexdigest()[:24]
        return TaskGenerationResult(
            seed_entity_id=self._chemical_id(chemical_id),
            environment_id=environment.environment_id,
            candidates_seen=len(world_answers) or 1,
            published=published,
            rejections=rejections,
            validation_reports=reports,
            evidence_catalog=catalog,
            episode=CtdGenerationEpisode(
                episode_id=episode_id,
                seed_entity_id=self._chemical_id(chemical_id),
                chemical_id=chemical_id,
                snapshot_id=snapshot_id,
                environment_id=environment.environment_id,
                candidate_count=len(world_answers),
                competitor_count=len(outcomes),
                published_count=len(published),
                search_complete=complete,
                stop_reason=(
                    "question_targets_met"
                    if published
                    else "required_searches_completed"
                ),
                evidence_gaps=sorted(
                    {
                        reason
                        for item in outcomes
                        for reason in item.reason_codes
                    }
                ),
                provenance={
                    "service_version": self.version,
                    "ctd_snapshot_id": snapshot_id,
                    "evidence_kind": evidence_kind,
                },
            ),
        )

    def _candidate(
        self,
        *,
        candidate_id: str,
        chemical_id: str,
        chemical_name: str,
        evidence_kind: str,
        answer_id: str,
        record: CtdEvidenceRecord,
        claim: NormalizedClaim,
        snapshot_id: str,
    ) -> TaskCandidate:
        return TaskCandidate(
            candidate_id=candidate_id,
            seed_entity_id=self._chemical_id(chemical_id),
            question_type="ctd_direct_evidence_disease",
            steps=[
                QueryStep(
                    position=0,
                    relation="ctd_direct_evidence",
                    direction=LogicalDirection.FORWARD,
                    target_entity_type="disease",
                )
            ],
            proposed_answer_ids=[answer_id],
            private_answer_names=[
                *sorted(
                    {
                        record.disease_name,
                        *record.disease_aliases,
                    }
                )
            ],
            answer_entity_type="disease",
            snapshot_id=snapshot_id,
            evidence_ids=list(claim.supporting_evidence_ids),
            candidate_claim_ids=[claim.claim_id],
            public_entity_names={
                self._chemical_id(chemical_id): chemical_name,
            },
            discovery_source=f"ctd_direct_{evidence_kind}",
            generator_version=self.version,
        )

    def _task(
        self,
        *,
        candidate: TaskCandidate,
        environment: ValidationEnvironment,
        evidence_kind: str,
        answer_id: str,
        claim: NormalizedClaim,
    ) -> TaskSpec:
        semantic_digest = candidate.semantic_digest()
        query = LogicalQuery(
            query_id="query-pending",
            path_id=f"ctd-direct-{candidate.candidate_id}",
            path_version=self.version,
            probe_id=f"ctd-probe-{candidate.candidate_id}",
            probe_digest=semantic_digest,
            start_entity_id=candidate.seed_entity_id,
            steps=list(candidate.steps),
            answer_entity_type="disease",
            expected_cardinality="one",
            filters={"direct_evidence": evidence_kind},
            snapshot_id=candidate.snapshot_id,
            dependency_digest="pending",
            created_at=utc_now(),
        )
        digest = query.digest()
        query = query.model_copy(
            update={
                "query_id": f"query-{digest[:24]}",
                "dependency_digest": digest,
            }
        )
        requirement = InformationRequirement(
            requirement_id=f"ctd-direct-requirement-{digest[:24]}",
            description=(
                f"establish CTD direct {evidence_kind} evidence for the "
                "answer disease"
            ),
            claim_pattern=ClaimPattern(
                subject_id=candidate.seed_entity_id,
                predicate_family=self._predicate_family(evidence_kind),
                object_id=answer_id,
                qualifier_constraints={
                    "direct_evidence": evidence_kind,
                },
            ),
            evidence_quality_rule="structured_required",
        )
        return TaskSpec(
            task_id="task-ctd-direct-" + digest[:24],
            candidate_id=candidate.candidate_id,
            seed_entity_id=candidate.seed_entity_id,
            question_type="ctd_direct_evidence_disease",
            query=query,
            proposed_answer_contract=AnswerContract(
                answer_type="entity",
                cardinality="exactly_one",
                canonical_answer_ids=[answer_id],
                accepted_aliases=list(candidate.private_answer_names),
                scope_statement=(
                    "all CTD disease records for the chemical in the frozen "
                    "CTD snapshot"
                ),
            ),
            information_requirements=[requirement],
            acceptable_evidence_sets=[
                AcceptableEvidenceSet(all_of=[requirement.requirement_id])
            ],
            completion_rule=CompletionRule(),
            validation_environment=environment,
            candidate_claim_ids=[claim.claim_id],
            candidate_evidence_ids=list(claim.supporting_evidence_ids),
            public_entity_names=dict(candidate.public_entity_names),
            generator_version=self.version,
        )

    def _report(
        self,
        *,
        task: TaskSpec,
        outcomes: list[AnswerCandidateOutcome],
        world_answers: list[str],
        claim_ids: list[str],
        evidence_ids: list[str],
        complete: bool,
    ) -> ValidationReport:
        reasons: list[str] = [] if complete else ["unknown_competitor"]
        report_id = validation_report_id_for(
            task,
            validator_version=self.version,
            world_answer_ids=world_answers,
            result_complete=complete,
            reason_codes=reasons,
            rows_examined=len(outcomes),
            matched_claim_ids=claim_ids,
            supporting_evidence_ids=evidence_ids,
            competitor_outcomes=outcomes,
            evidence_dependency_digest=task.evidence_dependency_digest(),
        )
        return ValidationReport(
            report_id=report_id,
            task_id=task.task_id,
            query_id=task.query.query_id,
            query_digest=task.query.dependency_digest,
            environment_id=task.validation_environment.environment_id,
            passed=complete,
            reason_codes=reasons,
            slice_answer_ids=list(world_answers),
            world_answer_ids=list(world_answers),
            result_complete=complete,
            rows_examined=len(outcomes),
            matched_claim_ids=list(claim_ids),
            supporting_evidence_ids=list(evidence_ids),
            competitor_outcomes=list(outcomes),
            evidence_dependency_digest=task.evidence_dependency_digest(),
            validator_version=self.version,
        )

    def _environment(self, snapshot_id: str) -> ValidationEnvironment:
        metadata = self.index.metadata()
        return ValidationEnvironment(
            environment_id=f"ctd-direct-world-{snapshot_id}",
            providers=[
                ProviderSnapshotRef(
                    provider_id="ctd",
                    snapshot_id=snapshot_id,
                    schema_version=metadata.get("schema_version"),
                    manifest_hash=metadata.get("source_sha256"),
                    completeness="full_chemical_disease_enumeration",
                )
            ],
            completeness_scope=(
                "every CTD disease record for the chemical in the frozen "
                "CTD index"
            ),
        )

    @staticmethod
    def _question(
        *,
        chemical_name: str,
        evidence_kind: str,
        qid: str,
        task_id: str,
    ) -> QuestionVariant:
        if evidence_kind == "marker/mechanism":
            text = (
                f"{chemical_name} 在 CTD 被记录与哪种疾病存在"
                "标记物或机制研究关联？"
            )
        else:
            text = (
                f"{chemical_name} 在 CTD 记录中被研究用于哪种疾病？"
            )
        return QuestionVariant(
            variant_id=f"variant-{qid}-ctd-direct-v1",
            qid=qid,
            task_id=task_id,
            question_text=text,
            writer_version="ctd-direct-writer-v1",
        )

    @staticmethod
    def _verify(
        *,
        variant: QuestionVariant,
        task: TaskSpec,
        answer_name: str,
        chemical_name: str,
    ) -> TextSemanticReport:
        forbidden = (
            "导致",
            "引起",
            "有效治疗",
            "治疗有效",
            "能够治疗",
            task.seed_entity_id,
            *task.proposed_answer_contract.canonical_answer_ids,
        )
        reasons: list[str] = []
        if answer_name.casefold() in variant.question_text.casefold():
            reasons.append("answer_leakage")
        if chemical_name not in variant.question_text:
            reasons.append("seed_anchor_missing")
        if any(
            value.casefold() in variant.question_text.casefold()
            for value in forbidden
        ):
            reasons.append("claim_semantics_mismatch")
        if not variant.question_text.endswith(("？", "?")):
            reasons.append("not_a_question")
        return TextSemanticReport(
            variant_id=variant.variant_id,
            task_id=task.task_id,
            passed=not reasons,
            reason_codes=sorted(set(reasons)),
            reconstructed_query_digest=(
                task.query.dependency_digest if not reasons else None
            ),
            verifier_version="ctd-direct-verifier-v1",
        )

    @staticmethod
    def _predicate_family(evidence_kind: str) -> str:
        return (
            "ctd_marker_mechanism"
            if evidence_kind == "marker/mechanism"
            else "ctd_therapeutic_research"
        )

    @staticmethod
    def _chemical_id(chemical_id: str) -> str:
        return f"ctd:chemical:{chemical_id}"

    @staticmethod
    def _disease_id(disease_id: str) -> str:
        return f"ctd:disease:{disease_id}"

    @staticmethod
    def _candidate_id(
        chemical_id: str,
        evidence_kind: str,
        snapshot_id: str,
    ) -> str:
        digest = hashlib.sha256(
            f"{snapshot_id}|{chemical_id}|{evidence_kind}".encode("utf-8")
        ).hexdigest()[:24]
        return f"candidate-ctd-direct-{digest}"

    @staticmethod
    def _unknown_answer_id(record: CtdEvidenceRecord) -> str:
        digest = hashlib.sha256(
            f"{record.source_snapshot_id}|{record.disease_name}".encode(
                "utf-8"
            )
        ).hexdigest()[:24]
        return f"ctd:unknown:{digest}"
