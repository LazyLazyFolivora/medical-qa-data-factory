"""Claim construction.

``RuleBasedClaimBuilder`` is the current implementation: deterministic, model
free, and honest about what a graph edge does and does not assert.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain import Claim, ClaimStatus, ClaimType, KGEvidence
from .curation import CuratedEvidence
from .semantics import RelationSemanticsPolicy


@runtime_checkable
class ClaimBuilder(Protocol):
    def build(self, evidence: list[KGEvidence], limit: int) -> list[Claim]: ...


def _claim_type(semantics: str) -> ClaimType:
    from .contracts import RelationSemantics

    if semantics == RelationSemantics.DIRECTIONAL_FACT:
        return ClaimType.FACT
    return ClaimType.ASSOCIATION


class RuleBasedClaimBuilder:
    """One claim per curated edge, using the raw relation as predicate."""

    def __init__(self, policy: RelationSemanticsPolicy | None = None) -> None:
        self.policy = policy or RelationSemanticsPolicy()

    def build(self, evidence: list[KGEvidence], limit: int) -> list[Claim]:
        raise NotImplementedError(
            "use build_from_curated so the answer endpoint stays explicit"
        )

    def build_from_curated(
        self, curated: list[CuratedEvidence], limit: int
    ) -> list[Claim]:
        claims: list[Claim] = []
        for item in curated[: max(limit, 0)]:
            edge = item.evidence
            # Subject and object follow the edge direction exactly as stored.
            claims.append(
                Claim(
                    claim_id=f"claim.{item.stable_edge_id}",
                    claim_text=(
                        f"{edge.head.canonical_name} —[{edge.relation}]→ "
                        f"{edge.tail.canonical_name}"
                    ),
                    claim_type=_claim_type(item.semantics.value),
                    subject=edge.head,
                    predicate=edge.relation,
                    object=edge.tail,
                    supporting_evidence_ids=[edge.evidence_id],
                    status=ClaimStatus.SUPPORTED,
                    rationale=(
                        f"Derived from PrimeKG edge {item.stable_edge_id} "
                        f"(source_record_id) with relation '{edge.relation}'. "
                        "The edge asserts this relation only; it is not evidence "
                        "of medical truth beyond that relation."
                    ),
                    # A graph edge is not a calibrated medical confidence.
                    confidence=None,
                )
            )
        return claims
