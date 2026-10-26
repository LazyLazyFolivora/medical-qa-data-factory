"""The one allowed rewrite, and the subclaims of a multi-hop path.

Step 7 of the real-generation-agent plan. Two things live here.

**Subclaims.** A multi-hop path is not one claim, it is one claim per hop plus
the conservative claim that the chain connects them. Each subclaim binds the
hop's KG evidence, its text evidence and its support assessment. The chain
claim is only ever derived, never asserted as a causal or therapeutic fact.

**The rewrite budget.** A question that the verifier asks to revise may be
rewritten exactly once, and only its wording may change. The answer, the
query, the path, the evidence and the split identity are fixed. A second
failure is a hard rejection, not another attempt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from pydantic import Field

from ..domain import Claim, ClaimStatus, ClaimType, KGEvidence
from ..domain.common import ContractModel, UtcDateTime
from .blind_verification import BlindVerdict, BlindVerificationResult
from .composition import CompositionVerdict, RelationCompositionPolicy
from .contracts import EvidencePath, EvidencePathStep


class RewriteOutcome(str, Enum):
    """What the rewrite budget allowed."""

    ACCEPTED = "accepted"
    REWRITTEN = "rewritten"
    HARD_REJECTED = "hard_rejected"


class Subclaim(ContractModel):
    """One hop's claim, with everything that supports it."""

    subclaim_id: str = Field(min_length=1)
    hop_position: int = Field(ge=0)
    claim_text: str = Field(min_length=1)
    relation: str = Field(min_length=1)
    subject_entity_id: str = Field(min_length=1)
    object_entity_id: str = Field(min_length=1)
    #: The KG evidence the hop rests on.
    kg_evidence_ids: list[str] = Field(default_factory=list)
    #: The text evidence that supports the hop.
    literature_evidence_ids: list[str] = Field(default_factory=list)
    #: The support assessments for that text evidence.
    support_assessment_ids: list[str] = Field(default_factory=list)
    created_at: UtcDateTime


class PathClaims(ContractModel):
    """The claims of one path: one per hop, plus the derived chain claim."""

    path_id: str = Field(min_length=1)
    path_version: str = Field(min_length=1)
    subclaims: list[Subclaim] = Field(default_factory=list)
    #: The conservative claim about the chain as a whole.
    chain_claim: Claim | None = None
    #: What the composition policy says about the chain.
    composition_verdict: str = CompositionVerdict.NEUTRALIZE.value
    composition_rationale: str = ""
    created_at: UtcDateTime


def build_path_claims(
    path: EvidencePath,
    *,
    evidence_by_id: dict[str, KGEvidence],
    literature_by_hop: dict[int, list[str]] | None = None,
    assessments_by_hop: dict[int, list[str]] | None = None,
    composition: RelationCompositionPolicy | None = None,
) -> PathClaims:
    """Build the per-hop claims and the conservative chain claim.

    The chain claim says the path connects the two entities. It does not say
    the first relation holds of the last entity, which is exactly the
    confusion the composition policy exists to prevent.
    """
    from ..domain.common import utc_now

    policy = composition or RelationCompositionPolicy()
    literature_by_hop = literature_by_hop or {}
    assessments_by_hop = assessments_by_hop or {}
    subclaims: list[Subclaim] = []
    for step in path.steps:
        evidence = evidence_by_id.get(step.evidence_id)
        if evidence is None:
            # A hop without persisted evidence cannot become a claim.
            continue
        subject = evidence.head.canonical_name or step.from_entity_id
        obj = evidence.tail.canonical_name or step.to_entity_id
        subclaims.append(
            Subclaim(
                subclaim_id=f"subclaim.{path.path_id}.{step.position}",
                hop_position=step.position,
                claim_text=f"{subject} —[{step.relation}]→ {obj}",
                relation=step.relation,
                subject_entity_id=evidence.head.canonical_id or step.from_entity_id,
                object_entity_id=evidence.tail.canonical_id or step.to_entity_id,
                kg_evidence_ids=[step.evidence_id],
                literature_evidence_ids=list(literature_by_hop.get(step.position, [])),
                support_assessment_ids=list(
                    assessments_by_hop.get(step.position, [])
                ),
                created_at=utc_now(),
            )
        )
    relations = [step.relation for step in path.steps]
    verdict = policy.verdict(relations)
    rationale = policy.rationale(relations)
    chain_claim = _chain_claim(path, subclaims, verdict)
    return PathClaims(
        path_id=path.path_id,
        path_version=path.path_version,
        subclaims=subclaims,
        chain_claim=chain_claim,
        composition_verdict=verdict.value,
        composition_rationale=rationale,
        created_at=utc_now(),
    )


def _chain_claim(
    path: EvidencePath, subclaims: list[Subclaim], verdict: CompositionVerdict
) -> Claim | None:
    """The conservative claim about the chain as a whole."""
    if not subclaims:
        return None
    first = subclaims[0]
    last = subclaims[-1]
    text = (
        f"{first.subject_entity_id} is connected to {last.object_entity_id} "
        f"by a {len(subclaims)}-hop chain of relations."
    )
    if verdict is CompositionVerdict.NEUTRALIZE:
        text += (
            " The chain does not assert that the first relation holds of the "
            "last entity."
        )
    return Claim(
        claim_id=f"claim.{path.path_id}.chain",
        claim_text=text,
        claim_type=ClaimType.ASSOCIATION,
        subject=None,
        predicate="path_connects",
        object=None,
        supporting_evidence_ids=[
            evidence_id
            for subclaim in subclaims
            for evidence_id in subclaim.kg_evidence_ids
        ],
        status=ClaimStatus.SUPPORTED,
        rationale=(
            "Derived from the ordered relation chain. The chain claim asserts "
            "connectivity only."
        ),
        confidence=None,
    )


@dataclass
class RewriteDecision:
    """The outcome of the one allowed rewrite."""

    outcome: RewriteOutcome
    #: The question text after the rewrite, when one happened.
    question_text: str = ""
    #: The verification result that produced this decision.
    verification: BlindVerificationResult | None = None
    #: How many rewrites the lineage has used.
    rewrites_used: int = 0
    message: str = ""


class RewriteBudget:
    """Tracks the rewrite budget of one question lineage.

    The budget is per lineage, not per question text. A rewrite that produces
    a new draft continues the same lineage, so it spends the same budget.
    """

    #: The number of rewrites one lineage may use.
    max_rewrites: int = 1

    def __init__(self, used: int = 0) -> None:
        self.used = used

    def decide(
        self,
        *,
        question_text: str,
        verification: BlindVerificationResult,
        rewrite: Any = None,
    ) -> RewriteDecision:
        """Decide what to do with a verification result.

        ``rewrite`` is called with the current question text and must return
        the new text. It may only change the wording: the answer, the query,
        the path and the evidence are fixed by the caller.
        """
        if verification.verdict is BlindVerdict.ACCEPTED:
            return RewriteDecision(
                outcome=RewriteOutcome.ACCEPTED,
                question_text=question_text,
                verification=verification,
                rewrites_used=self.used,
            )
        if verification.verdict is BlindVerdict.HARD_REJECTED:
            return RewriteDecision(
                outcome=RewriteOutcome.HARD_REJECTED,
                question_text=question_text,
                verification=verification,
                rewrites_used=self.used,
                message=verification.message or "the question was hard rejected",
            )
        # A revision was requested.
        if self.used >= self.max_rewrites:
            return RewriteDecision(
                outcome=RewriteOutcome.HARD_REJECTED,
                question_text=question_text,
                verification=verification,
                rewrites_used=self.used,
                message=(
                    "the question still needs a revision after the single "
                    "allowed rewrite"
                ),
            )
        if rewrite is None:
            return RewriteDecision(
                outcome=RewriteOutcome.HARD_REJECTED,
                question_text=question_text,
                verification=verification,
                rewrites_used=self.used,
                message="no rewriter was supplied for a question that needs one",
            )
        new_text = rewrite(question_text)
        if not new_text or not str(new_text).strip():
            return RewriteDecision(
                outcome=RewriteOutcome.HARD_REJECTED,
                question_text=question_text,
                verification=verification,
                rewrites_used=self.used,
                message="the rewrite produced an empty question",
            )
        self.used += 1
        return RewriteDecision(
            outcome=RewriteOutcome.REWRITTEN,
            question_text=str(new_text),
            verification=verification,
            rewrites_used=self.used,
            message=(
                "the question was rewritten; the answer, query, path and "
                "evidence are unchanged"
            ),
        )