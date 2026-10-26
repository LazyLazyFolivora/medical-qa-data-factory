"""Deterministic evidence curation.

Curation never asks a model for judgement. It only removes edges that cannot
be trusted, balances relations, and assigns a stable ordering.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..domain import EvidenceDirection, KGEvidence
from .contracts import RelationSemantics
from .semantics import RelationSemanticsPolicy


@dataclass(frozen=True)
class CuratedEvidence:
    """One curated edge plus the derivation facts the planner needs."""

    evidence: KGEvidence
    stable_edge_id: str
    relation: str
    answer_endpoint: str
    semantics: RelationSemantics
    seed_is_head: bool


@dataclass
class CurationReport:
    curated: list[CuratedEvidence] = field(default_factory=list)
    dropped_identity_mismatch: int = 0
    dropped_unstable_id: int = 0
    dropped_duplicate: int = 0


def _endpoint_for(seed_canonical_id: str, evidence: KGEvidence) -> str | None:
    """The answer endpoint is the side opposite the seed."""
    if evidence.head.canonical_id == seed_canonical_id:
        return "tail"
    if evidence.tail.canonical_id == seed_canonical_id:
        return "head"
    return None


def curate_evidence(
    evidence: list[KGEvidence],
    *,
    seed_canonical_id: str,
    policy: RelationSemanticsPolicy | None = None,
    max_per_relation: int = 2,
) -> CurationReport:
    policy = policy or RelationSemanticsPolicy()
    report = CurationReport()
    seen: set[str] = set()
    staged: list[CuratedEvidence] = []

    for item in evidence:
        endpoint = _endpoint_for(seed_canonical_id, item)
        if endpoint is None:
            # Cross-type identity mismatch: the edge does not actually involve
            # the seed we validated.
            report.dropped_identity_mismatch += 1
            continue
        stable_id = str(item.source_record_id or item.evidence_id)
        answer = item.tail if endpoint == "tail" else item.head
        if not stable_id or not answer.canonical_id or not answer.canonical_name:
            report.dropped_unstable_id += 1
            continue
        if stable_id in seen:
            report.dropped_duplicate += 1
            continue
        seen.add(stable_id)
        staged.append(
            CuratedEvidence(
                evidence=item,
                stable_edge_id=stable_id,
                relation=item.relation,
                answer_endpoint=endpoint,
                semantics=policy.classify(item.relation),
                seed_is_head=endpoint == "tail",
            )
        )

    # Stable ordering: relation, target type, target name, edge id.
    staged.sort(
        key=lambda cur: (
            cur.relation,
            (cur.evidence.tail if cur.answer_endpoint == "tail" else cur.evidence.head).entity_type or "",
            (cur.evidence.tail if cur.answer_endpoint == "tail" else cur.evidence.head).canonical_name or "",
            cur.stable_edge_id,
        )
    )

    # Round-robin across relations so one busy relation cannot fill every slot.
    buckets: dict[str, list[CuratedEvidence]] = {}
    for item in staged:
        buckets.setdefault(item.relation, []).append(item)
    for bucket in buckets.values():
        del bucket[max_per_relation:]
    ordered: list[CuratedEvidence] = []
    while any(buckets.values()):
        for relation in sorted(buckets):
            bucket = buckets[relation]
            if bucket:
                ordered.append(bucket.pop(0))
    report.curated = ordered
    return report


def direction_for(endpoint: str) -> EvidenceDirection:
    return (
        EvidenceDirection.FORWARD if endpoint == "tail" else EvidenceDirection.BACKWARD
    )
