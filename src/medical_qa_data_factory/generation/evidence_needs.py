"""Deterministic construction of per-hop literature evidence needs.

Step 4 of the real-generation-agent plan. Every semantic hop of a KG path
becomes its own information need, and each need must be answered by a
locatable, citable text passage before the path may carry a question.

The needs are built by code from the persisted path. A model may suggest the
wording of a query, but never the need id, the hop, the entity identity or
the budget.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable, Sequence

from ..domain import KGEvidence
from ..literature.contracts import LiteratureEvidenceNeed


#: Controlled relation phrasings. The relation vocabulary in a graph is
#: machine-oriented, so each relation gets a small set of phrasings that a
#: human-written paper is likely to use.
RELATION_PHRASES: dict[str, tuple[str, ...]] = {
    "treats": ("treatment", "therapeutic use", "indications"),
    "exposure_disease": ("associated with", "risk of", "exposure"),
    "contraindication": ("contraindicated in", "contraindications"),
    "indication": ("indicated for", "used in the treatment of"),
    "symptom": ("symptom", "clinical manifestation"),
    "mechanism": ("mechanism of action", "pharmacology"),
    "side_effect": ("adverse effect", "side effect", "adverse reaction"),
    "interacts_with": ("drug interaction", "interaction with"),
    "affects": ("affects", "effect on"),
    "has_symptom": ("presents with", "symptom"),
    "relates": ("related to", "associated with"),
}


def relation_phrases(relation: str) -> list[str]:
    """The controlled phrasings for one relation."""
    phrases = RELATION_PHRASES.get(relation)
    if phrases:
        return list(phrases)
    # An unknown relation still gets a usable, neutral phrasing rather than
    # a silent empty list.
    return [relation.replace("_", " "), relation]


def _names(evidence_by_id: dict[str, KGEvidence], evidence_id: str) -> tuple[str, str]:
    evidence = evidence_by_id[evidence_id]
    subject = evidence.head.canonical_name or evidence.head.raw_mention
    obj = evidence.tail.canonical_name or evidence.tail.raw_mention
    return subject, obj


def _aliases(evidence_by_id: dict[str, KGEvidence], evidence_id: str) -> tuple[list[str], list[str]]:
    evidence = evidence_by_id[evidence_id]
    subject = list(evidence.head.aliases)
    obj = list(evidence.tail.aliases)
    return subject, obj


def _need_digest(payload: dict[str, object]) -> str:
    text = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_needs_for_path(
    path: object,
    *,
    run_id: str,
    episode_id: str,
    evidence_by_id: dict[str, KGEvidence],
    filters: dict[str, object] | None = None,
    required_support_count: int = 1,
) -> list[LiteratureEvidenceNeed]:
    """One need per semantic hop of a path.

    The need binds the path, the hop, the KG edge, both entities and their
    aliases, so a result can later be traced back to the exact hop it was
    collected for.
    """
    needs: list[LiteratureEvidenceNeed] = []
    for step in getattr(path, "steps", []):
        evidence = evidence_by_id.get(step.evidence_id)
        if evidence is None:
            # A step without persisted evidence cannot become a need.
            continue
        subject, obj = _names(evidence_by_id, step.evidence_id)
        subject_aliases, object_aliases = _aliases(evidence_by_id, step.evidence_id)
        phrases = relation_phrases(step.relation)
        information_need = (
            f"{subject} {phrases[0]} {obj}"
        )
        dependency = _need_digest(
            {
                "path_id": getattr(path, "path_id", None),
                "hop_position": step.position,
                "kg_evidence_id": step.evidence_id,
                "subject": evidence.head.canonical_id,
                "object": evidence.tail.canonical_id,
                "relation": step.relation,
            }
        )
        needs.append(
            LiteratureEvidenceNeed(
                need_id=f"need.{getattr(path, 'path_id', 'path')}.{step.position}",
                run_id=run_id,
                episode_id=episode_id,
                path_id=getattr(path, "path_id", None),
                hop_position=step.position,
                kg_evidence_id=step.evidence_id,
                subject_entity_id=evidence.head.canonical_id or "",
                subject_name=evidence.head.canonical_name or "",
                subject_aliases=subject_aliases,
                canonical_relation=step.relation,
                relation_phrases=phrases,
                object_entity_id=evidence.tail.canonical_id or "",
                object_name=evidence.tail.canonical_name or "",
                object_aliases=object_aliases,
                information_need=information_need,
                required_support_count=required_support_count,
                filters=dict(filters or {}),
                dependency_digest=dependency,
            )
        )
    return needs


def build_query_for_need(need: LiteratureEvidenceNeed) -> str:
    """The retrieval query for one need.

    The query names both entities and the relation, so a text passage has to
    mention the actual pair to be a candidate support. Aliases are used when
    the canonical name is unusually long, which is where a name-only query
    usually fails.
    """
    subject = need.subject_name or need.subject_entity_id
    obj = need.object_name or need.object_entity_id
    phrase = need.relation_phrases[0] if need.relation_phrases else need.canonical_relation
    return f"{subject} {phrase} {obj}"


def build_need_for_edge(
    evidence: KGEvidence,
    *,
    run_id: str,
    episode_id: str,
    required_support_count: int = 1,
    filters: dict[str, object] | None = None,
) -> LiteratureEvidenceNeed:
    """Bind one discovered KG fact to one RAG information need.

    The model never creates this id. It is stable for the exact edge, so a
    resumed run can address the same need without issuing a second physical
    search.
    """
    subject = evidence.head.canonical_name or evidence.head.raw_mention
    obj = evidence.tail.canonical_name or evidence.tail.raw_mention
    phrases = relation_phrases(evidence.relation)
    dependency = _need_digest(
        {
            "kg_evidence_id": evidence.evidence_id,
            "subject": evidence.head.canonical_id,
            "relation": evidence.relation,
            "object": evidence.tail.canonical_id,
        }
    )
    need_id = "need." + dependency[:24]
    return LiteratureEvidenceNeed(
        need_id=need_id,
        run_id=run_id,
        episode_id=episode_id,
        kg_evidence_id=evidence.evidence_id,
        subject_entity_id=evidence.head.canonical_id or "",
        subject_name=subject,
        subject_aliases=list(evidence.head.aliases),
        canonical_relation=evidence.relation,
        relation_phrases=phrases,
        object_entity_id=evidence.tail.canonical_id or "",
        object_name=obj,
        object_aliases=list(evidence.tail.aliases),
        information_need=f"{subject} {phrases[0]} {obj}",
        required_support_count=required_support_count,
        filters=dict(filters or {}),
        dependency_digest=dependency,
    )


def needs_for_paths(
    paths: Sequence[object],
    *,
    run_id: str,
    episode_id: str,
    evidence_by_id: dict[str, KGEvidence],
    filters: dict[str, object] | None = None,
) -> list[LiteratureEvidenceNeed]:
    """Needs for several paths, deduplicated by dependency digest."""
    seen: set[str] = set()
    needs: list[LiteratureEvidenceNeed] = []
    for path in paths:
        for need in build_needs_for_path(
            path,
            run_id=run_id,
            episode_id=episode_id,
            evidence_by_id=evidence_by_id,
            filters=filters,
        ):
            if need.dependency_digest in seen:
                continue
            seen.add(need.dependency_digest)
            needs.append(need)
    return needs


def needs_covering(
    needs: Iterable[LiteratureEvidenceNeed], *, hop_positions: Iterable[int]
) -> set[int]:
    """Which hop positions have at least one need."""
    wanted = set(hop_positions)
    return {need.hop_position for need in needs if need.hop_position in wanted}
