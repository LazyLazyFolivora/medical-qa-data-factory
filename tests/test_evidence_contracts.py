from __future__ import annotations

from medical_qa_data_factory.domain import (
    EntityRef,
    EvidenceDirection,
    KGEvidence,
    LinkStatus,
)
from medical_qa_data_factory.evidence_enrichment.contracts import (
    EvidenceRecord,
    EvidenceStore,
    NormalizedClaim,
    evidence_from_ctd,
    evidence_from_kg,
    evidence_from_pubmed,
    migrate_legacy_claim,
)
from medical_qa_data_factory.evidence_enrichment.models import (
    CtdEvidenceRecord,
    EvidencePolarity,
    EvidenceSupportLevel,
    NormalizedEvidenceClaim,
    PubMedEvidenceRecord,
    QuestionIntent,
)
from medical_qa_data_factory.generation.requirement_matching import (
    match_requirement,
)
from medical_qa_data_factory.generation.task_contracts import (
    ClaimPattern,
    InformationRequirement,
)


NOW = "2026-10-27T00:00:00Z"


def _ref(name: str, canonical_id: str, entity_type: str) -> EntityRef:
    return EntityRef(
        entity_ref_id=f"entity-{canonical_id}",
        raw_mention=name,
        canonical_name=name,
        canonical_id=canonical_id,
        namespace="test",
        entity_type=entity_type,
        aliases=[],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="test",
    )


def _ctd() -> CtdEvidenceRecord:
    return CtdEvidenceRecord(
        chemical_id="D001241",
        chemical_name="Aspirin",
        disease_id="MESH:D011471",
        disease_name="Prostatic Neoplasms",
        disease_aliases=["Prostate cancer", "Prostatic Neoplasms"],
        direct_evidence=["marker/mechanism"],
        pubmed_ids=["123"],
        source_snapshot_id="ctd:snapshot-1",
        source_record_locator=(
            "ctd-record-v1:D001241:id:MESH%3AD011471"
        ),
        content_hash="a" * 64,
    )


def _pubmed() -> PubMedEvidenceRecord:
    return PubMedEvidenceRecord(
        pmid="123",
        title="Aspirin and prostate cancer",
        abstract="Aspirin exposure was associated with lower risk.",
        snapshot_id="pubmed:snapshot-1",
        content_hash="b" * 64,
        retrieved_at=NOW,
    )


def _kg() -> KGEvidence:
    return KGEvidence(
        evidence_id="kg-1",
        head=_ref("Aspirin", "primekg:exposure:D001241", "exposure"),
        relation="exposure_disease",
        tail=_ref("Prostate cancer", "primekg:disease:8315", "disease"),
        direction=EvidenceDirection.FORWARD,
        source_database="primekg",
        source_record_id="edge-1",
        retrieval_action={"provider_id": "primekg"},
        retrieved_at=NOW,
    )


def test_evidence_identity_binds_provider_snapshot_locator_and_content() -> None:
    first = EvidenceRecord.create(
        provider_id="ctd",
        source_kind="structured_database",
        snapshot_id="snapshot-1",
        source_locator="row-1",
        content_hash="a" * 64,
        record_type="structured_relation",
        subject_id="chemical-1",
        predicate_family="association",
        object_id="disease-1",
        retrieved_at=NOW,
    )
    replay = EvidenceRecord.create(
        provider_id="ctd",
        source_kind="structured_database",
        snapshot_id="snapshot-1",
        source_locator="row-1",
        content_hash="a" * 64,
        record_type="structured_relation",
        subject_id="chemical-1",
        predicate_family="association",
        object_id="disease-1",
        retrieved_at="2027-01-01T00:00:00Z",
    )
    changed = EvidenceRecord.create(
        provider_id="ctd",
        source_kind="structured_database",
        snapshot_id="snapshot-2",
        source_locator="row-1",
        content_hash="a" * 64,
        record_type="structured_relation",
        subject_id="chemical-1",
        predicate_family="association",
        object_id="disease-1",
        retrieved_at=NOW,
    )

    assert first.evidence_id == replay.evidence_id
    assert changed.evidence_id != first.evidence_id


def test_heterogeneous_adapters_emit_the_same_source_neutral_contract() -> None:
    ctd = evidence_from_ctd(
        _ctd(),
        subject_entity_id="primekg:exposure:D001241",
        object_entity_id="primekg:disease:8315",
        retrieved_at=NOW,
    )
    pubmed = evidence_from_pubmed(_pubmed())
    kg = evidence_from_kg(_kg(), provider_snapshot_id="primekg:snapshot-1")

    assert {ctd.provider_id, pubmed.provider_id, kg.provider_id} == {
        "ctd",
        "pubmed",
        "primekg",
    }
    assert ctd.record_type == "structured_relation"
    assert ctd.predicate_family == "chemical_disease_association"
    assert pubmed.record_type == "document"
    assert "lower risk" in (pubmed.document_text or "")
    assert kg.source_locator == "edge-1"
    assert len({ctd.evidence_id, pubmed.evidence_id, kg.evidence_id}) == 3


def test_claim_identity_is_semantic_and_evidence_route_independent() -> None:
    first = NormalizedClaim.create(
        subject_id="primekg:exposure:D001241",
        predicate_family="disease_risk",
        object_id="primekg:disease:8315",
        qualifiers={"population": "adults", "study_type": "cohort"},
        polarity="decrease",
        supporting_evidence_ids=["evidence-ctd"],
        normalizer_version="biomedical-claim-v1",
    )
    second = NormalizedClaim.create(
        subject_id="primekg:exposure:D001241",
        predicate_family="disease_risk",
        object_id="primekg:disease:8315",
        qualifiers={"study_type": "cohort", "population": "adults"},
        polarity="decrease",
        supporting_evidence_ids=["evidence-pubmed"],
        normalizer_version="biomedical-claim-v1",
    )

    assert first.claim_id == second.claim_id


def test_requirement_matcher_consumes_claims_not_provider_actions() -> None:
    ctd_evidence = evidence_from_ctd(
        _ctd(),
        subject_entity_id="primekg:exposure:D001241",
        object_entity_id="primekg:disease:8315",
        retrieved_at=NOW,
    )
    pubmed_evidence = evidence_from_pubmed(_pubmed())
    claim = NormalizedClaim.create(
        subject_id="primekg:exposure:D001241",
        predicate_family="disease_risk",
        object_id="primekg:disease:8315",
        qualifiers={"population": "adults", "study_type": "cohort"},
        polarity="decrease",
        supporting_evidence_ids=[pubmed_evidence.evidence_id],
        context_evidence_ids=[ctd_evidence.evidence_id],
        normalizer_version="biomedical-claim-v1",
    )
    store = EvidenceStore(
        records=[ctd_evidence, pubmed_evidence], claims=[claim]
    )
    requirement = InformationRequirement(
        requirement_id="req-risk",
        description="Aspirin is associated with lower disease risk in adults",
        claim_pattern=ClaimPattern(
            subject_id="primekg:exposure:D001241",
            predicate_family="disease_risk",
            object_id="primekg:disease:8315",
            qualifier_constraints={
                "population": "adults",
                "polarity": "decrease",
            },
        ),
        evidence_quality_rule="structured_or_supported_literature",
    )

    result = match_requirement(requirement, store)

    assert result.satisfied is True
    assert result.matched_claim_ids == [claim.claim_id]
    assert result.supporting_evidence_ids == [pubmed_evidence.evidence_id]


def test_contradicted_claim_does_not_satisfy_requirement() -> None:
    support = evidence_from_pubmed(_pubmed())
    contradiction = EvidenceRecord.create(
        provider_id="pubmed",
        source_kind="literature",
        snapshot_id="pubmed:snapshot-1",
        source_locator="pmid:456",
        content_hash="c" * 64,
        record_type="document",
        document_text="A second study reported a conflicting result.",
        retrieved_at=NOW,
    )
    claim = NormalizedClaim.create(
        subject_id="primekg:exposure:D001241",
        predicate_family="disease_risk",
        object_id="primekg:disease:8315",
        qualifiers={"population": "adults"},
        polarity="decrease",
        supporting_evidence_ids=[support.evidence_id],
        contradicting_evidence_ids=[contradiction.evidence_id],
        normalizer_version="biomedical-claim-v1",
    )
    store = EvidenceStore(records=[support, contradiction], claims=[claim])
    requirement = InformationRequirement(
        requirement_id="req-risk",
        description="risk claim",
        claim_pattern=ClaimPattern(
            subject_id=claim.subject_id,
            predicate_family=claim.predicate_family,
            object_id=claim.object_id,
        ),
    )

    result = match_requirement(requirement, store)

    assert result.satisfied is False
    assert result.reason_codes == ["matching_claim_has_contradiction"]


def test_legacy_claim_has_an_explicit_migration_adapter() -> None:
    legacy = NormalizedEvidenceClaim(
        claim_id="legacy-claim",
        kg_edge_id="kg-1",
        seed_entity_id="primekg:exposure:D001241",
        answer_entity_id="primekg:disease:8315",
        answer_entity_type="disease",
        relation="exposure_disease",
        question_intent=QuestionIntent.RISK_DECREASE,
        support_level=EvidenceSupportLevel.LITERATURE_SUPPORTED,
        polarity=EvidencePolarity.DECREASE,
        claim_text="lower risk association",
        literature_evidence_ids=["evidence-pubmed"],
    )

    migrated = migrate_legacy_claim(
        legacy,
        qualifiers={"population": "adults"},
        supporting_evidence_ids=["evidence-pubmed"],
    )

    assert migrated.predicate_family == "disease_risk"
    assert migrated.polarity == "decrease"
    assert migrated.legacy_claim_id == "legacy-claim"

