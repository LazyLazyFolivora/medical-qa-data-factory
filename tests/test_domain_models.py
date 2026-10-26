from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from medical_qa_data_factory.domain import (
    AnswerForm,
    Answerability,
    Claim,
    ClaimStatus,
    ClaimType,
    EntityRef,
    EvidenceBundle,
    EvidenceRequirements,
    GenerationMode,
    GoldAnswer,
    KGEvidence,
    LinkStatus,
    QuestionSpec,
    QuestionTaxonomy,
    QuestionVariant,
    Route,
    ValidationLevel,
)


NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def entity(ref_id: str, name: str) -> EntityRef:
    return EntityRef(
        entity_ref_id=ref_id,
        raw_mention=name,
        canonical_name=name,
        canonical_id=f"primekg:{ref_id}",
        namespace="primekg",
        entity_type="drug",
        aliases=[],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="manual",
    )


def test_linked_entity_requires_canonical_identity():
    with pytest.raises(ValidationError, match="canonical_name and canonical_id"):
        EntityRef(
            entity_ref_id="e1",
            raw_mention="aspirin",
            canonical_name=None,
            canonical_id=None,
            namespace="primekg",
            entity_type="drug",
            aliases=[],
            link_status=LinkStatus.EXACT,
            source="manual",
        )


def test_contract_rejects_naive_datetime():
    aspirin = entity("e1", "aspirin")
    with pytest.raises(ValidationError, match="timezone-aware"):
        KGEvidence(
            evidence_id="kg1",
            head=aspirin,
            relation="targets",
            tail=aspirin,
            direction="forward",
            source_database="PrimeKG",
            retrieval_action={"kind": "neighbors"},
            retrieved_at=datetime(2026, 10, 15),
        )


def test_evidence_bundle_rejects_dangling_claim_evidence_reference():
    aspirin = entity("e1", "aspirin")
    claim = Claim(
        claim_id="c1",
        claim_text="Aspirin has an associated target.",
        claim_type=ClaimType.ASSOCIATION,
        subject=aspirin,
        predicate="targets",
        object="target",
        supporting_evidence_ids=["missing"],
        contradicting_evidence_ids=[],
        context_evidence_ids=[],
        confidence=0.8,
        status=ClaimStatus.SUPPORTED,
        rationale="KG support",
    )

    with pytest.raises(ValidationError, match="unknown evidence ids"):
        EvidenceBundle(
            bundle_id="b1",
            bundle_version="1",
            task_id="t1",
            seed_entities=[aspirin],
            kg_evidence=[],
            literature_evidence=[],
            claims=[claim],
            created_at=NOW,
        )


def test_minimal_question_spec_is_serializable():
    question = QuestionSpec(
        question_id="q1",
        logical_question_id="lq1",
        question_version="1",
        bundle_ids=["b1"],
        generation_mode=GenerationMode.MANUAL,
        canonical_question="阿司匹林主要作用于什么靶点？",
        question_variants=[
            QuestionVariant(
                variant_id="v1",
                text="阿司匹林主要作用于什么靶点？",
                language="zh-CN",
                persona="biomedical_researcher",
                style="natural",
                generation_source="manual",
                semantic_check_status="passed",
            )
        ],
        taxonomy=QuestionTaxonomy(
            domain_intent="drug_target",
            answer_form=AnswerForm.ENTITY,
            reasoning_pattern=["single_hop"],
            hop_complexity=1,
            difficulty_tags=[],
        ),
        query_intent={"entity": "aspirin"},
        answerability=Answerability.ANSWERABLE,
        gold_answer=GoldAnswer(
            answer_mode=AnswerForm.ENTITY,
            values=[{"entity_id": "gene:PTGS1"}],
            short_answer="PTGS1",
            acceptable_variants=["COX-1"],
        ),
        evidence_requirements=EvidenceRequirements(
            required_claim_ids=["c1"],
            required_kg_evidence_ids=["kg1"],
            required_literature_evidence_ids=[],
            optional_evidence_ids=[],
            checks=[{"type": "path_exists"}],
            sufficiency_rule_version="v0",
        ),
        expected_route=Route.KG,
        verified_route=Route.UNKNOWN,
        validation_level=ValidationLevel.DRAFT,
        split_group_id="aspirin-target",
        created_at=NOW,
    )

    payload = question.model_dump(mode="json")

    assert payload["question_id"] == "q1"
    assert payload["generation_mode"] == "manual"
    assert payload["created_at"].endswith("Z")


def test_kg_evidence_requires_distinct_stable_id():
    aspirin = entity("e1", "aspirin")
    target = entity("e2", "PTGS1")
    evidence = KGEvidence(
        evidence_id="kg1",
        head=aspirin,
        relation="targets",
        tail=target,
        direction="forward",
        source_database="PrimeKG",
        retrieval_action={"kind": "neighbors"},
        retrieved_at=NOW,
    )

    assert evidence.head.canonical_id == "primekg:e1"

