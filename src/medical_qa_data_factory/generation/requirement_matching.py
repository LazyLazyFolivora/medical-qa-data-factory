"""Deterministic matching of source-neutral claims to task requirements."""
from __future__ import annotations

from pydantic import Field

from ..domain.common import ContractModel
from ..evidence_enrichment.contracts import (
    EvidenceRecord,
    EvidenceStore,
    NormalizedClaim,
)
from .task_contracts import ClaimPattern, InformationRequirement


class RequirementMatch(ContractModel):
    requirement_id: str = Field(min_length=1)
    satisfied: bool
    matched_claim_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)


def _matches_pattern(
    claim: NormalizedClaim, pattern: ClaimPattern
) -> bool:
    if claim.subject_id != pattern.subject_id:
        return False
    if claim.predicate_family != pattern.predicate_family:
        return False
    if pattern.object_id is not None and claim.object_id != pattern.object_id:
        return False
    if (
        pattern.object_value is not None
        and claim.object_value != pattern.object_value
    ):
        return False
    actual_qualifiers = {**claim.qualifiers, "polarity": claim.polarity}
    return all(
        actual_qualifiers.get(key) == value
        for key, value in pattern.qualifier_constraints.items()
    )


def _meets_quality_rule(
    claim: NormalizedClaim,
    evidence_by_id: dict[str, EvidenceRecord],
    rule: str,
) -> bool:
    supporting = [
        evidence_by_id[evidence_id]
        for evidence_id in claim.supporting_evidence_ids
    ]
    if rule == "structured_or_supported_literature":
        return bool(supporting)
    if rule == "literature_required":
        return any(item.source_kind == "literature" for item in supporting)
    if rule == "structured_required":
        return any(
            item.record_type == "structured_relation" for item in supporting
        )
    raise ValueError(f"unsupported evidence quality rule: {rule}")


def match_requirement(
    requirement: InformationRequirement,
    store: EvidenceStore,
) -> RequirementMatch:
    """Match by claim semantics and evidence quality, never by action route."""
    semantic_matches = [
        claim
        for claim in store.claims
        if _matches_pattern(claim, requirement.claim_pattern)
    ]
    if not semantic_matches:
        return RequirementMatch(
            requirement_id=requirement.requirement_id,
            satisfied=False,
            reason_codes=["no_matching_claim"],
        )
    if any(claim.contradicting_evidence_ids for claim in semantic_matches):
        return RequirementMatch(
            requirement_id=requirement.requirement_id,
            satisfied=False,
            matched_claim_ids=sorted(
                claim.claim_id for claim in semantic_matches
            ),
            reason_codes=["matching_claim_has_contradiction"],
        )
    evidence_by_id = {
        evidence.evidence_id: evidence for evidence in store.records
    }
    accepted = [
        claim
        for claim in semantic_matches
        if _meets_quality_rule(
            claim, evidence_by_id, requirement.evidence_quality_rule
        )
    ]
    if not accepted:
        return RequirementMatch(
            requirement_id=requirement.requirement_id,
            satisfied=False,
            matched_claim_ids=sorted(
                claim.claim_id for claim in semantic_matches
            ),
            reason_codes=["evidence_quality_rule_not_satisfied"],
        )
    return RequirementMatch(
        requirement_id=requirement.requirement_id,
        satisfied=True,
        matched_claim_ids=sorted(claim.claim_id for claim in accepted),
        supporting_evidence_ids=sorted(
            {
                evidence_id
                for claim in accepted
                for evidence_id in claim.supporting_evidence_ids
            }
        ),
    )
