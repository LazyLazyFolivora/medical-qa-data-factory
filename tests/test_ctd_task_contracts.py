from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from medical_qa_data_factory.evidence_enrichment.models import QuestionIntent
from medical_qa_data_factory.generation.logical_query import (
    LogicalDirection,
    LogicalQuery,
    QueryStep,
)
from medical_qa_data_factory.generation.task_contracts import (
    AcceptableEvidenceSet,
    AnswerCandidateOutcome,
    AnswerContract,
    ClaimPattern,
    CompletionRule,
    EvaluationTarget,
    InformationRequirement,
    ProviderSnapshotRef,
    StudyContext,
    TaskCandidate,
    TaskSpec,
    ValidationEnvironment,
    ValidationReport,
    promote_validated_task,
)
from medical_qa_data_factory.generation.task_validation import (
    validation_report_id_for,
)


def _query() -> LogicalQuery:
    query = LogicalQuery(
        query_id="query-ctd-1",
        path_id="path-ctd-1",
        path_version="ctd-study-context-v1",
        probe_id="probe-ctd-1",
        probe_digest="probe-digest",
        start_entity_id="primekg:exposure:D001241",
        steps=[
            QueryStep(
                position=0,
                relation="exposure_disease",
                direction=LogicalDirection.FORWARD,
                target_entity_type="disease",
            )
        ],
        answer_entity_type="disease",
        expected_cardinality="one",
        snapshot_id="ctd-snapshot-1",
        dependency_digest="pending",
        created_at=datetime.now(timezone.utc),
    )
    return query.model_copy(update={"dependency_digest": query.digest()})


def _context() -> StudyContext:
    return StudyContext(
        population="adults carrying genetic feature S",
        exposure_context="drug A exposure",
        dose_or_regimen="dose D",
        exposure_duration="at least two years",
        study_type="long-term follow-up",
        disease_category="digestive system cancer",
        relation_intent="risk_decrease",
        polarity="decrease",
    )


def _environment() -> ValidationEnvironment:
    return ValidationEnvironment(
        environment_id="ctd-world-1",
        providers=[
            ProviderSnapshotRef(
                provider_id="ctd",
                snapshot_id="ctd-snapshot-1",
                manifest_hash="sha256:ctd",
                completeness="full_chemical_disease_enumeration",
            ),
            ProviderSnapshotRef(
                provider_id="pubmed",
                snapshot_id="pubmed-corpus-1",
                manifest_hash="sha256:pubmed",
                completeness="fixed_read_back_corpus",
            ),
        ],
        completeness_scope=(
            "all CTD diseases matching the public study context"
        ),
    )


def _spec() -> TaskSpec:
    answer_id = "primekg:disease:ANSWER"
    requirement = InformationRequirement(
        requirement_id="req-risk-decrease",
        description="confirm the qualified risk-decrease relation",
        claim_pattern=ClaimPattern(
            subject_id="primekg:exposure:D001241",
            predicate_family="risk_decrease",
            object_id=answer_id,
            qualifier_constraints=_context().model_dump(mode="json"),
        ),
        evidence_quality_rule="ctd_record_and_supported_literature_span",
    )
    return TaskSpec(
        task_id="task-ctd-1",
        candidate_id="candidate-ctd-1",
        seed_entity_id="primekg:exposure:D001241",
        question_type="study_context_qualified_disease",
        query=_query(),
        proposed_answer_contract=AnswerContract(
            answer_type="entity",
            cardinality="exactly_one",
            canonical_answer_ids=[answer_id],
            accepted_aliases=["Answer disease"],
            scope_statement=(
                "the public study context in the pinned CTD/PubMed world"
            ),
        ),
        information_requirements=[requirement],
        acceptable_evidence_sets=[
            AcceptableEvidenceSet(all_of=[requirement.requirement_id])
        ],
        completion_rule=CompletionRule(),
        validation_environment=_environment(),
        candidate_claim_ids=["claim-risk-decrease-1"],
        candidate_evidence_ids=["ctd-row-1", "pubmed-span-1"],
        public_study_context=_context(),
        public_entity_names={
            "primekg:exposure:D001241": "Drug A",
        },
        generator_version="ctd-study-context-source-v1",
    )


def _report(spec: TaskSpec) -> ValidationReport:
    answer_id = spec.proposed_answer_contract.canonical_answer_ids[0]
    return ValidationReport(
        report_id="report-ctd-1",
        task_id=spec.task_id,
        query_id=spec.query.query_id,
        query_digest=spec.query.dependency_digest,
        environment_id=spec.validation_environment.environment_id,
        passed=True,
        world_answer_ids=[answer_id],
        result_complete=True,
        rows_examined=4,
        validator_version="ctd-task-validator-v1",
        matched_claim_ids=["claim-risk-decrease-1"],
        supporting_evidence_ids=["ctd-row-1", "pubmed-span-1"],
        competitor_outcomes=[
            AnswerCandidateOutcome(
                answer_id=answer_id,
                status="matched",
                claim_ids=["claim-risk-decrease-1"],
                evidence_ids=["ctd-row-1", "pubmed-span-1"],
            ),
            AnswerCandidateOutcome(
                answer_id="primekg:disease:COMPETITOR",
                status="not_matched",
                reason_codes=["qualifier_not_matched"],
            ),
        ],
        evidence_dependency_digest=spec.evidence_dependency_digest(),
    )


def test_question_intent_supports_candidate_biomarker_research() -> None:
    assert QuestionIntent.BIOMARKER_RESEARCH.value == "biomarker_research"


def test_study_context_requires_a_public_disambiguator() -> None:
    with pytest.raises(ValidationError, match="public disambiguator"):
        StudyContext(
            disease_category="digestive system cancer",
            relation_intent="risk_decrease",
            polarity="decrease",
        )


def test_risk_direction_and_polarity_must_agree() -> None:
    with pytest.raises(ValidationError, match="risk_decrease requires"):
        StudyContext(
            population="adults",
            disease_category="digestive system cancer",
            relation_intent="risk_decrease",
            polarity="increase",
        )


def test_ctd_candidate_requires_public_context_and_claims() -> None:
    payload = {
        "candidate_id": "candidate-ctd-1",
        "seed_entity_id": "primekg:exposure:D001241",
        "question_type": "study_context_qualified_disease",
        "steps": _query().steps,
        "proposed_answer_ids": ["primekg:disease:ANSWER"],
        "private_answer_names": ["Answer disease"],
        "answer_entity_type": "disease",
        "snapshot_id": "ctd-snapshot-1",
        "evidence_ids": ["ctd-row-1", "pubmed-span-1"],
        "discovery_source": "ctd_study_context",
        "generator_version": "ctd-study-context-source-v1",
    }

    with pytest.raises(ValidationError, match="public study context"):
        TaskCandidate.model_validate(payload)

    with pytest.raises(ValidationError, match="candidate claims"):
        TaskCandidate.model_validate(
            {**payload, "public_study_context": _context()}
        )


def test_ctd_task_spec_rejects_hidden_scope_only_tasks() -> None:
    spec = _spec()

    with pytest.raises(ValidationError, match="public study context"):
        TaskSpec.model_validate(
            {
                **spec.model_dump(mode="python"),
                "public_study_context": None,
            }
        )


def test_promotion_binds_gold_claims_evidence_and_completion_rule() -> None:
    spec = _spec()
    validated = promote_validated_task(spec, _report(spec))
    target = EvaluationTarget.from_validated_task("question-ctd-1", validated)

    assert validated.gold_claim_ids == ["claim-risk-decrease-1"]
    assert validated.gold_evidence_ids == ["ctd-row-1", "pubmed-span-1"]
    assert target.completion_rule == CompletionRule()
    assert target.gold_claim_ids == validated.gold_claim_ids
    assert target.gold_evidence_ids == validated.gold_evidence_ids


def test_promotion_rejects_unknown_competitors() -> None:
    spec = _spec()
    report = _report(spec)
    report_payload = report.model_dump(mode="python")
    report_payload["competitor_outcomes"] = [
        AnswerCandidateOutcome(
            answer_id="primekg:disease:UNKNOWN",
            status="not_evaluated",
            reason_codes=["pubmed_record_unreadable"],
        )
    ]

    with pytest.raises(ValidationError, match="unknown competitor"):
        ValidationReport.model_validate(report_payload)


def test_promotion_rejects_evidence_dependency_mismatch() -> None:
    spec = _spec()
    report = _report(spec).model_copy(
        update={"evidence_dependency_digest": "sha256:wrong"}
    )

    with pytest.raises(ValueError, match="evidence dependency digest"):
        promote_validated_task(spec, report)


def test_promotion_rejects_unbound_gold_references() -> None:
    spec = _spec()
    report = _report(spec).model_copy(
        update={"matched_claim_ids": ["claim-not-in-task"]}
    )

    with pytest.raises(ValueError, match="outside task dependencies"):
        promote_validated_task(spec, report)


def test_validation_report_identity_includes_evidence_dependencies() -> None:
    spec = _spec()
    report = _report(spec)
    common = {
        "validator_version": report.validator_version,
        "world_answer_ids": report.world_answer_ids,
        "result_complete": report.result_complete,
        "reason_codes": report.reason_codes,
        "rows_examined": report.rows_examined,
        "matched_claim_ids": report.matched_claim_ids,
        "competitor_outcomes": report.competitor_outcomes,
        "evidence_dependency_digest": report.evidence_dependency_digest,
    }

    first = validation_report_id_for(
        spec,
        supporting_evidence_ids=["ctd-row-1", "pubmed-span-1"],
        **common,
    )
    changed = validation_report_id_for(
        spec,
        supporting_evidence_ids=["ctd-row-1", "pubmed-span-2"],
        **common,
    )

    assert first != changed
