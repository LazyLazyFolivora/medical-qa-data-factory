"""Fuse retrieved chunks into verifiable text evidence.

Step 4 of the real-generation-agent plan. Two things happen here, in this
order:

1. A retrieved chunk becomes ``LiteratureEvidence`` only after it has been
   read back from the same snapshot and its content hash agrees. A chunk that
   cannot be read back is a context candidate, never gold.
2. A separate ``EvidenceSupportAssessment`` records whether the passage
   supports, contradicts, merely contextualises or is irrelevant to the need
   it was collected for. The assessment never overwrites the evidence.

The assessment universe matters: every applicable result for a need must be
judged. A report that only lists the supporting passages is not a coverage
report, it is a selection.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence

from ..domain import EntityRef, KGEvidence, LinkStatus
from ..domain.common import ContractModel, UtcDateTime, new_id, utc_now
from ..literature.contracts import (
    LiteratureCallStatus,
    LiteratureChunkReadResult,
    LiteratureEvidenceNeed,
    LiteratureRetrievalResult,
    RetrievedChunk,
)


class SupportVerdict(str, Enum):
    """What a passage does for the need it was collected for."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXT_ONLY = "context_only"
    IRRELEVANT = "irrelevant"
    UNCERTAIN = "uncertain"


class EvidenceSupportAssessment(ContractModel):
    """One judgement about one passage against one need.

    It is stored separately from the evidence, so the original passage is
    never rewritten by a judgement.
    """

    assessment_id: str
    assessment_version: str
    need_id: str
    need_version: str
    literature_evidence_id: str
    literature_evidence_version: str
    content_hash: str
    entity_link_version: str
    verdict: SupportVerdict
    subject_link_status: str
    object_link_status: str
    relation_match: str
    cited_span_offsets: dict[str, int] = field(default_factory=dict)
    cited_span_digest: str = ""
    deterministic_checks: dict[str, Any] = field(default_factory=dict)
    evaluator_config_id: str
    rationale: str
    created_at: UtcDateTime


class FusedLiteratureEvidence(ContractModel):
    """A chunk that survived the read-back and hash check."""

    evidence_id: str
    evidence_version: str
    need_ids: list[str] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    path_id: str | None = None
    hop_positions: list[int] = field(default_factory=list)
    document_id: str
    chunk_id: str
    provider: str
    index_snapshot_id: str | None = None
    trace_id: str | None = None
    read_trace_id: str | None = None
    round_trip_verified: bool = False
    title: str
    evidence_text: str
    content_hash: str
    location: dict[str, Any] = field(default_factory=dict)
    retrieval_query: str
    retrieval_rank: int | None = None
    retrieval_score: float | None = None
    license_note: str | None = None
    retrieved_at: UtcDateTime


def normalise_hash(text: str) -> str:
    """Canonical hash of a passage's text."""
    normalised = " ".join(text.split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


@dataclass
class FusionOutcome:
    """What the fusion produced and what it could not accept."""

    evidence: list[FusedLiteratureEvidence] = field(default_factory=list)
    assessments: list[EvidenceSupportAssessment] = field(default_factory=list)
    #: Results that could not be read back, with the reason.
    unverified: list[tuple[str, str]] = field(default_factory=list)
    #: The full universe of results that were judged.
    judged_chunk_ids: list[str] = field(default_factory=list)

    @property
    def verified_count(self) -> int:
        return sum(1 for item in self.evidence if item.round_trip_verified)


def fuse_retrieval(
    retrieval: LiteratureRetrievalResult,
    read_backs: dict[str, LiteratureChunkReadResult],
    *,
    need: LiteratureEvidenceNeed,
    provider: str,
    entity_link_version: str,
    retrieval_query: str,
    evaluator_config_id: str = "deterministic-v1",
) -> FusionOutcome:
    """Turn one search's results into verified evidence and assessments.

    A chunk becomes evidence only if it was read back from the same snapshot
    and the two texts hash the same. A chunk that was read back differently is
    recorded as unverified, never quietly kept.
    """
    outcome = FusionOutcome()
    for rank, chunk in enumerate(retrieval.chunks, start=1):
        outcome.judged_chunk_ids.append(chunk.chunk_id)
        read_back = read_backs.get(chunk.chunk_id)
        if read_back is None:
            outcome.unverified.append(
                (chunk.chunk_id, "the chunk was never read back")
            )
            continue
        if read_back.status is not LiteratureCallStatus.SUCCESS:
            outcome.unverified.append(
                (chunk.chunk_id, f"read-back failed: {read_back.status.value}")
            )
            continue
        if read_back.content is None:
            outcome.unverified.append((chunk.chunk_id, "the read-back had no text"))
            continue
        searched_hash = normalise_hash(chunk.content)
        read_hash = normalise_hash(read_back.content)
        if searched_hash != read_hash:
            outcome.unverified.append(
                (chunk.chunk_id, "the read-back text does not match the search text")
            )
            continue
        if (
            read_back.snapshot is not None
            and retrieval.snapshot.snapshot_id
            and read_back.snapshot.snapshot_id != retrieval.snapshot.snapshot_id
        ):
            outcome.unverified.append(
                (chunk.chunk_id, "the read-back came from a different snapshot")
            )
            continue
        if chunk.start_at != read_back.start_at or chunk.end_at != read_back.end_at:
            outcome.unverified.append(
                (chunk.chunk_id, "the read-back offsets do not match the search offsets")
            )
            continue
        evidence = FusedLiteratureEvidence(
            # The same physical passage can support two different facts. The
            # evidence identity therefore includes the server-built need and
            # snapshot: one physical chunk may produce several support records
            # without any of them overwriting another need's provenance.
            evidence_id=(
                "lit."
                + hashlib.sha256(
                    "|".join(
                        (
                            str(retrieval.snapshot.snapshot_id or ""),
                            chunk.chunk_id,
                            need.need_id,
                        )
                    ).encode("utf-8")
                ).hexdigest()[:24]
            ),
            evidence_version="v1",
            need_ids=[need.need_id],
            path_id=need.path_id,
            hop_positions=[need.hop_position] if need.hop_position is not None else [],
            document_id=chunk.document_id or read_back.document_id or "",
            chunk_id=chunk.chunk_id,
            provider=provider,
            index_snapshot_id=retrieval.snapshot.snapshot_id,
            trace_id=retrieval.trace_id,
            read_trace_id=read_back.trace_id,
            round_trip_verified=True,
            title=chunk.title,
            evidence_text=read_back.content,
            content_hash=read_hash,
            location={
                "chunk_id": chunk.chunk_id,
                "chunk_index": chunk.chunk_index,
                "start_at": chunk.start_at,
                "end_at": chunk.end_at,
            },
            retrieval_query=retrieval_query,
            retrieval_rank=rank,
            retrieval_score=chunk.score,
            retrieved_at=utc_now(),
        )
        outcome.evidence.append(evidence)
        outcome.assessments.append(
            assess_support(
                evidence=evidence,
                need=need,
                entity_link_version=entity_link_version,
                evaluator_config_id=evaluator_config_id,
            )
        )
    return outcome


def assess_support(
    *,
    evidence: FusedLiteratureEvidence,
    need: LiteratureEvidenceNeed,
    entity_link_version: str,
    evaluator_config_id: str = "deterministic-v1",
) -> EvidenceSupportAssessment:
    """Judge whether a passage supports the need it was collected for.

    The judgement is deterministic and explainable: it looks for the two
    entities and the relation in the passage. It is not a claim of medical
    truth - it is a statement about whether the passage speaks about the
    relation at all, which is what a coverage gate can be built on.
    """
    text = evidence.evidence_text
    lowered = text.lower()
    subject = _subject_terms(need)
    obj = _object_terms(need)
    subject_found = any(term and term.lower() in lowered for term in subject)
    object_found = any(term and term.lower() in lowered for term in obj)
    relation_found = any(
        phrase and phrase.lower() in lowered for phrase in need.relation_phrases
    )
    if subject_found and object_found and relation_found:
        verdict = SupportVerdict.SUPPORTS
        rationale = (
            "the passage names both entities and the relation"
        )
    elif subject_found and object_found:
        verdict = SupportVerdict.CONTEXT_ONLY
        rationale = (
            "the passage names both entities but does not state the relation"
        )
    elif subject_found or object_found:
        verdict = SupportVerdict.UNCERTAIN
        rationale = "the passage names only one of the two entities"
    else:
        verdict = SupportVerdict.IRRELEVANT
        rationale = "the passage names neither entity of this hop"
    subject_link = "exact" if subject_found else "not_found"
    object_link = "exact" if object_found else "not_found"
    relation_match = "matched" if relation_found else "not_matched"
    span = _cited_span(text, subject, obj)
    return EvidenceSupportAssessment(
        assessment_id=new_id("assessment"),
        assessment_version="v1",
        need_id=need.need_id,
        need_version=need.dependency_digest[:16],
        literature_evidence_id=evidence.evidence_id,
        literature_evidence_version=evidence.evidence_version,
        content_hash=evidence.content_hash,
        entity_link_version=entity_link_version,
        verdict=verdict,
        subject_link_status=subject_link,
        object_link_status=object_link,
        relation_match=relation_match,
        cited_span_offsets=span,
        cited_span_digest=hashlib.sha256(
            f"{span.get('start', 0)}:{span.get('end', 0)}:{evidence.content_hash}".encode()
        ).hexdigest(),
        deterministic_checks={
            "subject_found": subject_found,
            "object_found": object_found,
            "relation_found": relation_found,
            "round_trip_verified": evidence.round_trip_verified,
        },
        evaluator_config_id=evaluator_config_id,
        rationale=rationale,
        created_at=utc_now(),
    )


def _subject_terms(need: LiteratureEvidenceNeed) -> list[str]:
    """The names a passage may use for the subject of the hop.

    The canonical id is included, but a passage never writes an id: it writes
    a name. The name is therefore the term that must match.
    """
    return [need.subject_name, need.subject_entity_id, *need.subject_aliases]


def _object_terms(need: LiteratureEvidenceNeed) -> list[str]:
    return [need.object_name, need.object_entity_id, *need.object_aliases]


def _cited_span(text: str, subject: list[str], obj: list[str]) -> dict[str, int]:
    """The character range that covers the two entities, when both appear."""
    lowered = text.lower()
    positions: list[int] = []
    for terms in (subject, obj):
        found: list[int] = []
        for term in terms:
            if not term:
                continue
            index = lowered.find(term.lower())
            if index >= 0:
                found.append(index)
        if found:
            positions.append(min(found))
    if len(positions) == 2:
        return {
            "start": min(positions),
            "end": max(positions) + 1,
        }
    if positions:
        return {"start": positions[0], "end": positions[0] + 1}
    return {"start": 0, "end": 0}


def supporting_evidence_ids(
    assessments: Iterable[EvidenceSupportAssessment],
) -> list[str]:
    """The evidence ids judged to support their need."""
    return [
        item.literature_evidence_id
        for item in assessments
        if item.verdict is SupportVerdict.SUPPORTS
    ]


def contradicting_evidence_ids(
    assessments: Iterable[EvidenceSupportAssessment],
) -> list[str]:
    """The evidence ids judged to contradict their need."""
    return [
        item.literature_evidence_id
        for item in assessments
        if item.verdict is SupportVerdict.CONTRADICTS
    ]
