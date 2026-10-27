from __future__ import annotations

import hashlib

from medical_qa_data_factory.domain.common import utc_now
from medical_qa_data_factory.evidence_enrichment.contracts import (
    EvidenceStore,
)
from medical_qa_data_factory.evidence_enrichment.models import (
    CtdEvidenceRecord,
    EvidencePolarity,
    PubMedEvidenceRecord,
)
from medical_qa_data_factory.evidence_enrichment.pubmed_evidence import (
    PubMedSpanCitation,
)
from medical_qa_data_factory.generation.ctd_world_evaluation import (
    FusionCtdCandidateAssessor,
)
from medical_qa_data_factory.generation.logical_query import (
    LogicalDirection,
    QueryStep,
)
from medical_qa_data_factory.generation.requirement_matching import (
    match_requirement,
)
from medical_qa_data_factory.generation.task_compiler import TaskCompiler
from medical_qa_data_factory.generation.evidence_fusion import (
    CtdEvidenceExtraction,
    CtdEvidenceFusionStatus,
    PubMedFinding,
    PubMedFindingVerdict,
    StudyContextIntent,
    StudyContextQualifiers,
    fuse_ctd_pubmed_evidence,
)
from medical_qa_data_factory.generation.task_contracts import (
    ProviderSnapshotRef,
    StudyContext,
    TaskCandidate,
    ValidationEnvironment,
)


ABSTRACT = (
    "In adults receiving low-dose aspirin for at least two years, "
    "prostate cancer risk was lower in the observational cohort."
)
QUOTE = "prostate cancer risk was lower"


def _ctd(
    *,
    direct_evidence: list[str] | None = None,
    inference_genes: list[str] | None = None,
) -> CtdEvidenceRecord:
    direct = (
        ["marker/mechanism"]
        if direct_evidence is None
        else direct_evidence
    )
    return CtdEvidenceRecord(
        chemical_id="D001241",
        chemical_name="Aspirin",
        disease_id="MESH:D011471",
        disease_name="prostate cancer",
        direct_evidence=direct,
        inference_genes=inference_genes or [],
        pubmed_ids=["123"],
        source_snapshot_id="ctd:2026-10",
        source_record_locator="ctd:row:1",
        content_hash=hashlib.sha256(b"ctd-row-1").hexdigest(),
    )


def _pubmed(
    *,
    pmid: str = "123",
    abstract: str = ABSTRACT,
    snapshot_id: str = "pubmed:test",
) -> PubMedEvidenceRecord:
    title = "Aspirin exposure and prostate cancer"
    return PubMedEvidenceRecord(
        pmid=pmid,
        title=title,
        abstract=abstract,
        snapshot_id=snapshot_id,
        content_hash=hashlib.sha256(
            f"{title}\n{abstract}".encode()
        ).hexdigest(),
        retrieved_at=utc_now(),
    )


def _span(
    *,
    pmid: str = "123",
    abstract: str = ABSTRACT,
    quote: str = QUOTE,
) -> PubMedSpanCitation:
    start = abstract.index(quote)
    return PubMedSpanCitation(
        pmid=pmid,
        start=start,
        end=start + len(quote),
        quote=quote,
    )


def _finding(
    *,
    pmid: str = "123",
    verdict: PubMedFindingVerdict = PubMedFindingVerdict.SUPPORTS,
    intent: StudyContextIntent | None = StudyContextIntent.RISK_DECREASE,
    polarity: EvidencePolarity = EvidencePolarity.DECREASE,
    claim_text: str = "prostate cancer risk was lower",
    qualifiers: StudyContextQualifiers | None = None,
    span: PubMedSpanCitation | None = None,
) -> PubMedFinding:
    return PubMedFinding(
        pmid=pmid,
        verdict=verdict,
        intent=intent,
        polarity=polarity,
        support_level="literature_supported",
        claim_text=claim_text,
        qualifiers=qualifiers
        or StudyContextQualifiers(
            population="adults",
            study_type="observational cohort",
            dose_exposure="low-dose aspirin",
            temporal_scope="at least two years",
        ),
        citation=span or _span(pmid=pmid),
    )


def _extraction(*findings: PubMedFinding) -> CtdEvidenceExtraction:
    return CtdEvidenceExtraction(findings=list(findings))


def _fuse(
    *,
    ctd_records: list[CtdEvidenceRecord] | None = None,
    pubmed_records: list[PubMedEvidenceRecord] | None = None,
    findings: tuple[PubMedFinding, ...] | None = None,
):
    return fuse_ctd_pubmed_evidence(
        ctd_records=ctd_records or [_ctd()],
        pubmed_records=pubmed_records or [_pubmed()],
        extraction=_extraction(*(findings or (_finding(),))),
        seed_entity_id="primekg:exposure:D001241",
        answer_entity_id="primekg:disease:8315",
    )


def test_supported_finding_preserves_qualifiers_and_span_evidence() -> None:
    result = _fuse()

    assert result.status is CtdEvidenceFusionStatus.ELIGIBLE
    assert result.claim is not None
    assert result.claim.predicate_family == "disease_risk"
    assert result.claim.polarity == "decrease"
    assert result.claim.qualifiers == {
        "disease_category": None,
        "dose_exposure": "low-dose aspirin",
        "exposure_context": None,
        "exposure_duration": None,
        "population": "adults",
        "study_type": "observational cohort",
        "temporal_scope": "at least two years",
        "sample_type": None,
        "additional_qualifiers": {},
    }
    assert result.ctd_direct_evidence == ["marker/mechanism"]
    assert result.supporting_evidence_ids == result.claim.supporting_evidence_ids
    assert result.pubmed_assessments[0].span.start >= 0
    assert result.pubmed_assessments[0].verdict is PubMedFindingVerdict.SUPPORTS


def test_support_and_contradiction_with_the_same_qualifiers_are_conflicting() -> None:
    contradiction_text = (
        "No lower prostate cancer risk was observed in the same cohort."
    )
    contradiction_record = _pubmed(
        pmid="456", abstract=contradiction_text
    )
    contradiction = _finding(
        pmid="456",
        verdict=PubMedFindingVerdict.CONTRADICTS,
        claim_text="no lower risk was observed",
        span=_span(
            pmid="456",
            abstract=contradiction_text,
            quote="No lower prostate cancer risk",
        ),
    )

    result = _fuse(
        pubmed_records=[_pubmed(), contradiction_record],
        findings=(_finding(), contradiction),
    )

    assert result.status is CtdEvidenceFusionStatus.CONFLICTING
    assert result.claim is None
    assert "contradicting_pubmed_evidence" in result.reason_codes


def test_context_only_findings_never_create_a_claim() -> None:
    result = _fuse(
        findings=(
            _finding(
                verdict=PubMedFindingVerdict.CONTEXT_ONLY,
                intent=None,
                polarity=EvidencePolarity.UNKNOWN,
            ),
        )
    )

    assert result.status is CtdEvidenceFusionStatus.INSUFFICIENT
    assert result.claim is None
    assert "no_supporting_pubmed_evidence" in result.reason_codes


def test_inference_only_ctd_records_cannot_become_eligible() -> None:
    result = _fuse(
        ctd_records=[
            _ctd(
                direct_evidence=[],
                inference_genes=["PTGS1", "TP53"],
            )
        ]
    )

    assert result.status is CtdEvidenceFusionStatus.REJECTED
    assert result.claim is None
    assert "ctd_inference_only" in result.reason_codes


def test_association_text_cannot_upgrade_to_causality() -> None:
    association_quote = "Aspirin exposure causes lower prostate cancer risk."
    abstract = f"{association_quote} This sentence is intentionally invalid."
    result = _fuse(
        pubmed_records=[_pubmed(abstract=abstract)],
        findings=(
            _finding(
                intent=StudyContextIntent.ASSOCIATION_RESEARCH,
                polarity=EvidencePolarity.UNKNOWN,
                claim_text=association_quote,
                span=_span(
                    abstract=abstract,
                    quote=association_quote,
                ),
            ),
        ),
    )

    assert result.status is CtdEvidenceFusionStatus.REJECTED
    assert result.claim is None
    assert "claim_strength_exaggerated" in result.reason_codes


def test_therapeutic_research_requires_direct_therapeutic_ctd_evidence() -> None:
    result = _fuse(
        ctd_records=[_ctd(direct_evidence=["marker/mechanism"])],
        findings=(
            _finding(
                intent=StudyContextIntent.THERAPEUTIC_RESEARCH,
                polarity=EvidencePolarity.UNKNOWN,
                claim_text="aspirin was studied for use in this disease",
            ),
        ),
    )

    assert result.status is CtdEvidenceFusionStatus.REJECTED
    assert result.claim is None
    assert "ctd_intent_evidence_mismatch" in result.reason_codes


def test_risk_intent_requires_matching_polarity() -> None:
    result = _fuse(
        findings=(
            _finding(
                intent=StudyContextIntent.RISK_INCREASE,
                polarity=EvidencePolarity.DECREASE,
            ),
        )
    )

    assert result.status is CtdEvidenceFusionStatus.REJECTED
    assert result.claim is None
    assert "intent_polarity_mismatch" in result.reason_codes


def test_every_supplied_pubmed_record_must_be_assessed() -> None:
    second = _pubmed(
        pmid="456",
        abstract="Aspirin and prostate cancer were discussed.",
    )

    result = _fuse(
        pubmed_records=[_pubmed(), second],
        findings=(_finding(),),
    )

    assert result.status is CtdEvidenceFusionStatus.REJECTED
    assert result.claim is None
    assert "pubmed_assessment_incomplete" in result.reason_codes


def test_duplicate_observations_are_deduplicated() -> None:
    result = _fuse(
        ctd_records=[_ctd(), _ctd()],
        pubmed_records=[_pubmed(), _pubmed()],
    )

    assert result.status is CtdEvidenceFusionStatus.ELIGIBLE
    assert len(result.ctd_record_ids) == 1
    assert len(result.pubmed_assessments) == 1
    assert len(result.claim.supporting_evidence_ids) == 1


def test_fixed_inputs_replay_to_the_same_fusion_and_claim_identity() -> None:
    first = _fuse()
    second = _fuse()

    assert first.fusion_id == second.fusion_id
    assert first.claim is not None
    assert second.claim is not None
    assert first.claim.claim_id == second.claim.claim_id
    assert first.supporting_evidence_ids == second.supporting_evidence_ids


def test_fusion_outcome_can_be_consumed_by_complete_world_evaluation() -> None:
    record = _ctd()
    fusion = _fuse(
        findings=(
            _finding(
                qualifiers=StudyContextQualifiers(
                    population="adults",
                    study_type="observational cohort",
                    dose_exposure="low-dose aspirin",
                    temporal_scope="at least two years",
                    disease_category="prostate cancer",
                )
            ),
        )
    )
    assessor = FusionCtdCandidateAssessor(
        fusions={
            record.source_record_locator or record.content_hash: fusion
        }
    )

    outcome = assessor.assess(
        record=record,
        context=StudyContext(
            population="adults",
            dose_or_regimen="low-dose aspirin",
            study_type="observational cohort",
            exposure_duration="at least two years",
            disease_category="prostate cancer",
            relation_intent="risk_decrease",
            polarity="decrease",
        ),
        answer_id="primekg:disease:8315",
    )

    assert outcome.status == "matched"
    assert outcome.claim_ids == [fusion.claim.claim_id]
    assert len(outcome.evidence_ids) == len(
        fusion.supporting_evidence_ids
    ) + 1
    assert set(fusion.supporting_evidence_ids).issubset(
        set(outcome.evidence_ids)
    )


def test_compiled_study_requirement_matches_the_fused_claim() -> None:
    fusion = _fuse(
        findings=(
            _finding(
                qualifiers=StudyContextQualifiers(
                    population="adults",
                    study_type="observational cohort",
                    dose_exposure="low-dose aspirin",
                    temporal_scope="at least two years",
                    disease_category="prostate cancer",
                )
            ),
        )
    )
    context = StudyContext(
        population="adults",
        dose_or_regimen="low-dose aspirin",
        study_type="observational cohort",
        exposure_duration="at least two years",
        disease_category="prostate cancer",
        relation_intent="risk_decrease",
        polarity="decrease",
    )
    candidate = TaskCandidate(
        candidate_id="candidate-context-integration",
        seed_entity_id="primekg:exposure:D001241",
        question_type="study_context_qualified_disease",
        steps=[
            QueryStep(
                position=0,
                relation="chemical_exposure_disease",
                direction=LogicalDirection.FORWARD,
                target_entity_type="disease",
            )
        ],
        proposed_answer_ids=["primekg:disease:8315"],
        private_answer_names=["prostate cancer"],
        answer_entity_type="disease",
        snapshot_id="ctd:2026-10",
        evidence_ids=["ctd:row:1"],
        candidate_claim_ids=[fusion.claim.claim_id],
        public_study_context=context,
        public_entity_names={
            "primekg:exposure:D001241": "Aspirin",
        },
        discovery_source="test",
        generator_version="ctd-study-context-source-v1",
    )
    environment = ValidationEnvironment(
        environment_id="ctd-environment",
        providers=[
            ProviderSnapshotRef(
                provider_id="ctd",
                snapshot_id="ctd:2026-10",
                completeness="full_chemical_disease_enumeration",
            ),
            ProviderSnapshotRef(
                provider_id="pubmed",
                snapshot_id="pubmed:test",
                completeness="fixed_read_back_corpus",
            ),
        ],
        completeness_scope="all matching CTD diseases",
    )
    task = TaskCompiler().compile(candidate, environment)

    match = match_requirement(
        task.information_requirements[0],
        EvidenceStore(
            records=fusion.evidence_records,
            claims=[fusion.claim],
        ),
    )

    assert match.satisfied is True
    assert match.matched_claim_ids == [fusion.claim.claim_id]
