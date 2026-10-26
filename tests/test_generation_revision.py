"""Subclaims of a multi-hop path, and the one allowed rewrite.

Step 7 of the real-generation-agent plan.
"""
from __future__ import annotations

from typing import Any

import pytest

from medical_qa_data_factory.domain import (
    EntityRef,
    EvidenceDirection,
    KGEvidence,
    LinkStatus,
)
from medical_qa_data_factory.domain.common import utc_now
from medical_qa_data_factory.generation.blind_verification import (
    BlindCheckCode,
    BlindVerdict,
    BlindVerificationResult,
)
from medical_qa_data_factory.generation.composition import CompositionVerdict
from medical_qa_data_factory.generation.contracts import (
    EvidencePath,
    EvidencePathStep,
)
from medical_qa_data_factory.generation.revision import (
    RewriteBudget,
    RewriteOutcome,
    build_path_claims,
)


ASPIRIN = "drug:aspirin"
HEADACHE = "disease:headache"
MIGRAINE = "disease:migraine"


def _ref(canonical_id: str, name: str) -> EntityRef:
    return EntityRef(
        entity_ref_id=f"entity-{canonical_id}",
        raw_mention=name,
        canonical_name=name,
        canonical_id=canonical_id,
        namespace="primekg",
        entity_type="disease",
        aliases=[],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="primekg",
    )


def _evidence(evidence_id: str, head: str, relation: str, tail: str) -> KGEvidence:
    return KGEvidence(
        evidence_id=evidence_id,
        head=_ref(head, head),
        relation=relation,
        tail=_ref(tail, tail),
        direction=EvidenceDirection.FORWARD,
        source_database="primekg",
        source_record_id=evidence_id,
        confidence=1.0,
        retrieval_action={"provider_id": "primekg"},
        retrieved_at=utc_now(),
    )


def _path() -> EvidencePath:
    relations = [("treats", HEADACHE), ("indication", MIGRAINE)]
    steps = []
    for index, (relation, target) in enumerate(relations):
        source = ASPIRIN if index == 0 else relations[index - 1][1]
        steps.append(
            EvidencePathStep(
                position=index,
                evidence_id=f"kg.e{index}",
                traversal_direction="forward",
                from_entity_id=source,
                relation=relation,
                to_entity_id=target,
                provider_id="primekg",
                snapshot_id="snap-1",
                query_trace_id=f"q{index}",
            )
        )
    return EvidencePath(
        path_id="opath.1",
        path_version="opath.1.v1",
        episode_id="ep1",
        task_id="t1",
        bundle_id="b1",
        provider_id="primekg",
        snapshot_id="snap-1",
        start_entity_id=ASPIRIN,
        end_entity_id=MIGRAINE,
        steps=steps,
        intermediate_entity_ids=[HEADACHE],
        source_evidence_ids=["kg.e0", "kg.e1"],
        path_hash="hash",
        query_trace_ids=["q0", "q1"],
    )


def _evidence_registry() -> dict[str, KGEvidence]:
    return {
        "kg.e0": _evidence("kg.e0", ASPIRIN, "treats", HEADACHE),
        "kg.e1": _evidence("kg.e1", HEADACHE, "indication", MIGRAINE),
    }


# ---- subclaims -----------------------------------------------------------


def test_every_hop_gets_a_subclaim() -> None:
    claims = build_path_claims(_path(), evidence_by_id=_evidence_registry())
    assert len(claims.subclaims) == 2
    assert [item.hop_position for item in claims.subclaims] == [0, 1]


def test_a_subclaim_binds_its_kg_evidence() -> None:
    claims = build_path_claims(_path(), evidence_by_id=_evidence_registry())
    assert claims.subclaims[0].kg_evidence_ids == ["kg.e0"]
    assert claims.subclaims[1].kg_evidence_ids == ["kg.e1"]


def test_a_subclaim_binds_its_text_evidence() -> None:
    claims = build_path_claims(
        _path(),
        evidence_by_id=_evidence_registry(),
        literature_by_hop={0: ["lit.1"]},
        assessments_by_hop={0: ["assessment.1"]},
    )
    assert claims.subclaims[0].literature_evidence_ids == ["lit.1"]
    assert claims.subclaims[0].support_assessment_ids == ["assessment.1"]


def test_a_hop_without_evidence_produces_no_subclaim() -> None:
    claims = build_path_claims(_path(), evidence_by_id={"kg.e0": _evidence_registry()["kg.e0"]})
    assert len(claims.subclaims) == 1


def test_the_chain_claim_asserts_connectivity_only() -> None:
    claims = build_path_claims(_path(), evidence_by_id=_evidence_registry())
    assert claims.chain_claim is not None
    text = claims.chain_claim.claim_text
    assert "chain" in text
    assert "does not assert" in text


def test_the_chain_claim_is_an_association_not_a_fact() -> None:
    from medical_qa_data_factory.domain import ClaimType

    claims = build_path_claims(_path(), evidence_by_id=_evidence_registry())
    assert claims.chain_claim is not None
    assert claims.chain_claim.claim_type is ClaimType.ASSOCIATION


def test_the_composition_verdict_is_recorded() -> None:
    claims = build_path_claims(_path(), evidence_by_id=_evidence_registry())
    assert claims.composition_verdict == CompositionVerdict.NEUTRALIZE.value
    assert "treats" in claims.composition_rationale


def test_the_chain_claim_lists_all_its_evidence() -> None:
    claims = build_path_claims(_path(), evidence_by_id=_evidence_registry())
    assert claims.chain_claim is not None
    assert set(claims.chain_claim.supporting_evidence_ids) == {"kg.e0", "kg.e1"}


# ---- the rewrite budget --------------------------------------------------


def _verification(verdict: BlindVerdict) -> BlindVerificationResult:
    return BlindVerificationResult(
        request_id="v1",
        verdict=verdict,
        reason_codes=["answer_leaked"] if verdict is not BlindVerdict.ACCEPTED else [],
        message="a message",
        blocking=verdict is BlindVerdict.HARD_REJECTED,
        created_at=utc_now(),
    )


def test_an_accepted_question_is_accepted_without_a_rewrite() -> None:
    budget = RewriteBudget()
    decision = budget.decide(
        question_text="Which disease?",
        verification=_verification(BlindVerdict.ACCEPTED),
    )
    assert decision.outcome is RewriteOutcome.ACCEPTED
    assert decision.rewrites_used == 0
    assert decision.question_text == "Which disease?"


def test_a_hard_rejection_is_not_rewritten() -> None:
    budget = RewriteBudget()
    decision = budget.decide(
        question_text="Does aspirin treat migraine?",
        verification=_verification(BlindVerdict.HARD_REJECTED),
    )
    assert decision.outcome is RewriteOutcome.HARD_REJECTED
    assert decision.rewrites_used == 0


def test_one_rewrite_is_allowed() -> None:
    budget = RewriteBudget()
    decision = budget.decide(
        question_text="Does aspirin treat migraine?",
        verification=_verification(BlindVerdict.NEEDS_REVISION),
        rewrite=lambda text: "Which disease is indicated for the condition "
        "aspirin treats?",
    )
    assert decision.outcome is RewriteOutcome.REWRITTEN
    assert decision.rewrites_used == 1
    assert "migraine" not in decision.question_text.lower()


def test_a_second_rewrite_is_hard_rejected() -> None:
    budget = RewriteBudget()
    budget.decide(
        question_text="Does aspirin treat migraine?",
        verification=_verification(BlindVerdict.NEEDS_REVISION),
        rewrite=lambda text: "Which disease?",
    )
    decision = budget.decide(
        question_text="Which disease?",
        verification=_verification(BlindVerdict.NEEDS_REVISION),
        rewrite=lambda text: "Which one?",
    )
    assert decision.outcome is RewriteOutcome.HARD_REJECTED
    assert decision.rewrites_used == 1


def test_a_rewrite_that_produces_nothing_is_hard_rejected() -> None:
    budget = RewriteBudget()
    decision = budget.decide(
        question_text="Does aspirin treat migraine?",
        verification=_verification(BlindVerdict.NEEDS_REVISION),
        rewrite=lambda text: "   ",
    )
    assert decision.outcome is RewriteOutcome.HARD_REJECTED
    assert budget.used == 0


def test_a_revision_without_a_rewriter_is_hard_rejected() -> None:
    budget = RewriteBudget()
    decision = budget.decide(
        question_text="Does aspirin treat migraine?",
        verification=_verification(BlindVerdict.NEEDS_REVISION),
    )
    assert decision.outcome is RewriteOutcome.HARD_REJECTED


def test_the_budget_is_per_lineage() -> None:
    """A rewrite that produces a new draft continues the same budget."""
    budget = RewriteBudget()
    first = budget.decide(
        question_text="a",
        verification=_verification(BlindVerdict.NEEDS_REVISION),
        rewrite=lambda text: "b",
    )
    assert first.rewrites_used == 1
    second = budget.decide(
        question_text="b",
        verification=_verification(BlindVerdict.NEEDS_REVISION),
        rewrite=lambda text: "c",
    )
    assert second.outcome is RewriteOutcome.HARD_REJECTED