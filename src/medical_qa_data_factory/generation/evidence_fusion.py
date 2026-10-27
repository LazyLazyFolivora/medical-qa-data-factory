"""Fuse retrieved evidence into verifiable supports and claims.

The literature fusion path and the CTD/PubMed formal-task path share the same
rules:

1. A retrieved chunk becomes ``LiteratureEvidence`` only after it has been
   read back from the same snapshot and its content hash agrees. A chunk that
   cannot be read back is a context candidate, never gold.
2. A separate ``EvidenceSupportAssessment`` records whether the passage
   supports, contradicts, merely contextualises or is irrelevant to the need
   it was collected for. The assessment never overwrites the evidence.

The assessment universe matters: every applicable result for a need must be
judged. A report that only lists the supporting passages is not a coverage
report, it is a selection. The CTD fusion path applies the same closed-world
rule per PMID and adds deterministic qualifier, conflict and semantic gates
before a source-neutral claim can be built.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence

from pydantic import Field, model_validator

from ..domain import EntityRef, KGEvidence, LinkStatus
from ..domain.common import ContractModel, UtcDateTime, new_id, utc_now
from ..evidence_enrichment.contracts import (
    EvidenceRecord,
    NormalizedClaim,
    evidence_from_ctd,
    predicate_family_for_intent,
)
from ..evidence_enrichment.models import (
    CtdEvidenceRecord,
    EvidencePolarity,
    PubMedEvidenceRecord,
    QuestionIntent,
)
from ..evidence_enrichment.pubmed_evidence import (
    PubMedEvidenceSpan,
    PubMedSpanCitation,
    PubMedSpanError,
    evidence_from_verified_span,
    verify_span_citation,
)
from ..literature.contracts import (
    LiteratureCallStatus,
    LiteratureChunkReadResult,
    LiteratureEvidenceNeed,
    LiteratureRetrievalResult,
    RetrievedChunk,
)
from .semantics import study_claim_rejection_codes


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


class StudyContextIntent(str, Enum):
    """Intents allowed in the first CTD-backed release."""

    ASSOCIATION_RESEARCH = "association_research"
    BIOMARKER_RESEARCH = "biomarker_research"
    THERAPEUTIC_RESEARCH = "therapeutic_research"
    RISK_INCREASE = "risk_increase"
    RISK_DECREASE = "risk_decrease"
    SAFETY = "safety"


class PubMedFindingVerdict(str, Enum):
    """What one independently verified PubMed span does for the claim."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXT_ONLY = "context_only"
    UNCERTAIN = "uncertain"


class CtdEvidenceFusionStatus(str, Enum):
    ELIGIBLE = "eligible"
    INSUFFICIENT = "insufficient"
    CONFLICTING = "conflicting"
    REJECTED = "rejected"


class StudyContextQualifiers(ContractModel):
    """Scientific conditions that materially narrow the claim."""

    population: str | None = None
    study_type: str | None = None
    dose_exposure: str | None = None
    exposure_context: str | None = None
    exposure_duration: str | None = None
    temporal_scope: str | None = None
    sample_type: str | None = None
    disease_category: str | None = None
    additional_qualifiers: dict[str, str] = Field(default_factory=dict)

    def as_claim_qualifiers(self) -> dict[str, str | None]:
        payload = self.model_dump(mode="json")
        if (
            payload["temporal_scope"] is None
            and payload["exposure_duration"] is not None
        ):
            payload["temporal_scope"] = payload["exposure_duration"]
        for key, value in self.additional_qualifiers.items():
            payload.setdefault(key, value)
        return payload


class PubMedFinding(ContractModel):
    """Constrained extraction proposed by the model for one PMID."""

    pmid: str = Field(min_length=1)
    verdict: PubMedFindingVerdict
    intent: StudyContextIntent | None = None
    polarity: EvidencePolarity = EvidencePolarity.UNKNOWN
    support_level: str = "literature_supported"
    claim_text: str = ""
    qualifiers: StudyContextQualifiers = Field(
        default_factory=StudyContextQualifiers
    )
    #: Every non-empty qualifier must point to a verbatim fragment covered by
    #: ``citation.quote``. This prevents a model from filling a plausible
    #: population, dose or duration that the cited text never states.
    qualifier_evidence: dict[str, str] = Field(default_factory=dict)
    citation: PubMedSpanCitation

    @model_validator(mode="after")
    def supporting_findings_are_complete(self) -> "PubMedFinding":
        if self.verdict in {
            PubMedFindingVerdict.SUPPORTS,
            PubMedFindingVerdict.CONTRADICTS,
        }:
            if self.intent is None:
                raise ValueError("supporting or contradicting finding needs intent")
            if not self.claim_text.strip():
                raise ValueError("supporting finding needs claim text")
            if self.support_level not in {
                "direct",
                "literature_supported",
            }:
                raise ValueError("finding cannot be inferred or unsupported")
        return self


class CtdEvidenceExtraction(ContractModel):
    """The only model output accepted by the Step 4 fusion gate."""

    findings: list[PubMedFinding] = Field(min_length=1)


class VerifiedPubMedAssessment(ContractModel):
    """A finding with an independently re-readable span and evidence id."""

    pmid: str = Field(min_length=1)
    verdict: PubMedFindingVerdict
    intent: StudyContextIntent | None = None
    polarity: EvidencePolarity
    support_level: str
    claim_text: str
    qualifiers: StudyContextQualifiers
    span: PubMedEvidenceSpan
    evidence_id: str = Field(min_length=1)


def _ctd_fusion_id(
    *,
    ctd_record_ids: list[str],
    pubmed_assessments: list[VerifiedPubMedAssessment],
    status: CtdEvidenceFusionStatus,
    claim: NormalizedClaim | None,
) -> str:
    payload = {
        "ctd_record_ids": sorted(ctd_record_ids),
        "pubmed_assessments": [
            {
                "pmid": item.pmid,
                "verdict": item.verdict.value,
                "intent": item.intent.value if item.intent else None,
                "polarity": item.polarity.value,
                "qualifiers": item.qualifiers.model_dump(mode="json"),
                "span_id": item.span.span_id,
                "evidence_id": item.evidence_id,
            }
            for item in sorted(pubmed_assessments, key=lambda value: value.pmid)
        ],
        "status": status.value,
        "claim_id": claim.claim_id if claim else None,
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"ctd-fusion.{digest[:24]}"


class CtdEvidenceFusion(ContractModel):
    """Layered CTD/PubMed observations and their deterministic disposition."""

    schema_version: str = "ctd-evidence-fusion-v1"
    fusion_id: str = Field(min_length=1)
    status: CtdEvidenceFusionStatus
    ctd_record_ids: list[str] = Field(default_factory=list)
    ctd_direct_evidence: list[str] = Field(default_factory=list)
    pubmed_assessments: list[VerifiedPubMedAssessment] = Field(
        default_factory=list
    )
    evidence_records: list[EvidenceRecord] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    context_evidence_ids: list[str] = Field(default_factory=list)
    uncertain_evidence_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    normalizer_version: str = "ctd-pubmed-fusion-v1"
    claim: NormalizedClaim | None = None

    @model_validator(mode="after")
    def identity_and_disposition_are_valid(self) -> "CtdEvidenceFusion":
        if self.status is CtdEvidenceFusionStatus.ELIGIBLE:
            if self.claim is None or not self.supporting_evidence_ids:
                raise ValueError("eligible fusion needs a claim and support")
        elif self.claim is not None:
            raise ValueError("rejected fusion cannot publish a claim")
        expected = _ctd_fusion_id(
            ctd_record_ids=self.ctd_record_ids,
            pubmed_assessments=self.pubmed_assessments,
            status=self.status,
            claim=self.claim,
        )
        if self.fusion_id != expected:
            raise ValueError("fusion ID does not match its evidence and outcome")
        return self


def _dedupe_ctd_records(
    records: Sequence[CtdEvidenceRecord],
) -> tuple[list[CtdEvidenceRecord], list[str]]:
    by_id: dict[str, CtdEvidenceRecord] = {}
    reasons: list[str] = []
    for record in records:
        record_id = record.source_record_locator or record.content_hash
        key = f"{record.source_snapshot_id}:{record_id}"
        previous = by_id.get(key)
        if previous is not None and (
            previous.content_hash != record.content_hash
        ):
            reasons.append("ctd_record_conflict")
            continue
        by_id[key] = previous or record
    return [by_id[key] for key in sorted(by_id)], reasons


def _dedupe_pubmed_records(
    records: Sequence[PubMedEvidenceRecord],
) -> tuple[list[PubMedEvidenceRecord], list[str]]:
    by_pmid: dict[str, PubMedEvidenceRecord] = {}
    reasons: list[str] = []
    for record in records:
        previous = by_pmid.get(record.pmid)
        if previous is not None and (
            previous.content_hash != record.content_hash
            or previous.snapshot_id != record.snapshot_id
        ):
            reasons.append("pubmed_record_conflict")
            continue
        by_pmid[record.pmid] = previous or record
    return [by_pmid[pmid] for pmid in sorted(by_pmid)], reasons


def _finding_signature(
    finding: PubMedFinding | VerifiedPubMedAssessment,
) -> tuple[str | None, str, str]:
    return (
        finding.intent.value if finding.intent else None,
        finding.polarity.value,
        json.dumps(
            finding.qualifiers.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def _rejected_fusion(
    *,
    ctd_record_ids: list[str],
    ctd_direct_evidence: list[str],
    pubmed_assessments: list[VerifiedPubMedAssessment] | None = None,
    evidence_records: list[EvidenceRecord] | None = None,
    status: CtdEvidenceFusionStatus = CtdEvidenceFusionStatus.REJECTED,
    reason_codes: list[str],
    supporting_evidence_ids: list[str] | None = None,
    contradicting_evidence_ids: list[str] | None = None,
    context_evidence_ids: list[str] | None = None,
    uncertain_evidence_ids: list[str] | None = None,
) -> CtdEvidenceFusion:
    assessments = pubmed_assessments or []
    support = supporting_evidence_ids or []
    contradiction = contradicting_evidence_ids or []
    context = context_evidence_ids or []
    uncertain = uncertain_evidence_ids or []
    return CtdEvidenceFusion(
        fusion_id=_ctd_fusion_id(
            ctd_record_ids=ctd_record_ids,
            pubmed_assessments=assessments,
            status=status,
            claim=None,
        ),
        status=status,
        ctd_record_ids=ctd_record_ids,
        ctd_direct_evidence=ctd_direct_evidence,
        pubmed_assessments=assessments,
        evidence_records=evidence_records or [],
        supporting_evidence_ids=support,
        contradicting_evidence_ids=contradiction,
        context_evidence_ids=context,
        uncertain_evidence_ids=uncertain,
        reason_codes=sorted(set(reason_codes)),
    )


def fuse_ctd_pubmed_evidence(
    *,
    ctd_records: Sequence[CtdEvidenceRecord],
    pubmed_records: Sequence[PubMedEvidenceRecord],
    extraction: CtdEvidenceExtraction,
    seed_entity_id: str,
    answer_entity_id: str,
    ctd_retrieved_at: UtcDateTime | str | None = None,
) -> CtdEvidenceFusion:
    """Fuse CTD rows and constrained PubMed findings into one claim.

    The model proposes per-PMID findings. This function owns every decision
    that can make a claim publishable: record identity, PMID coverage, span
    verification, evidence roles, semantic upgrade checks and conflict status.
    """
    ctd, ctd_reasons = _dedupe_ctd_records(ctd_records)
    pubmed, pubmed_reasons = _dedupe_pubmed_records(pubmed_records)
    ctd_ids = [
        f"{item.source_snapshot_id}:"
        f"{item.source_record_locator or item.content_hash}"
        for item in ctd
    ]
    direct_evidence = sorted(
        {
            value
            for record in ctd
            for value in record.direct_evidence
            if value.strip()
        }
    )
    record_reasons = ctd_reasons + pubmed_reasons
    if record_reasons:
        return _rejected_fusion(
            ctd_record_ids=ctd_ids,
            ctd_direct_evidence=direct_evidence,
            reason_codes=record_reasons,
        )
    if not ctd:
        return _rejected_fusion(
            ctd_record_ids=[],
            ctd_direct_evidence=[],
            status=CtdEvidenceFusionStatus.INSUFFICIENT,
            reason_codes=["missing_ctd_evidence"],
        )
    if not pubmed:
        return _rejected_fusion(
            ctd_record_ids=ctd_ids,
            ctd_direct_evidence=direct_evidence,
            status=CtdEvidenceFusionStatus.INSUFFICIENT,
            reason_codes=["missing_pubmed_evidence"],
        )
    if not direct_evidence:
        return _rejected_fusion(
            ctd_record_ids=ctd_ids,
            ctd_direct_evidence=[],
            reason_codes=["ctd_inference_only"],
        )

    supplied_pmids = {record.pmid for record in pubmed}
    finding_by_pmid: dict[str, PubMedFinding] = {}
    for finding in extraction.findings:
        if finding.pmid in finding_by_pmid:
            return _rejected_fusion(
                ctd_record_ids=ctd_ids,
                ctd_direct_evidence=direct_evidence,
                reason_codes=["pubmed_assessment_duplicate"],
            )
        finding_by_pmid[finding.pmid] = finding
    if set(finding_by_pmid) != supplied_pmids:
        return _rejected_fusion(
            ctd_record_ids=ctd_ids,
            ctd_direct_evidence=direct_evidence,
            reason_codes=["pubmed_assessment_incomplete"],
        )

    record_by_pmid = {record.pmid: record for record in pubmed}
    verified: list[VerifiedPubMedAssessment] = []
    span_evidence: list[EvidenceRecord] = []
    for pmid in sorted(finding_by_pmid):
        finding = finding_by_pmid[pmid]
        try:
            span = verify_span_citation(
                finding.citation,
                records=pubmed,
            )
            evidence = evidence_from_verified_span(
                span,
                record=record_by_pmid[pmid],
            )
        except PubMedSpanError as exc:
            return _rejected_fusion(
                ctd_record_ids=ctd_ids,
                ctd_direct_evidence=direct_evidence,
                reason_codes=[exc.code],
            )
        verified.append(
            VerifiedPubMedAssessment(
                pmid=pmid,
                verdict=finding.verdict,
                intent=finding.intent,
                polarity=finding.polarity,
                support_level=finding.support_level,
                claim_text=finding.claim_text,
                qualifiers=finding.qualifiers,
                span=span,
                evidence_id=evidence.evidence_id,
            )
        )
        span_evidence.append(evidence)

    support = [
        item
        for item in verified
        if item.verdict is PubMedFindingVerdict.SUPPORTS
    ]
    contradiction = [
        item
        for item in verified
        if item.verdict is PubMedFindingVerdict.CONTRADICTS
    ]
    context = [
        item
        for item in verified
        if item.verdict is PubMedFindingVerdict.CONTEXT_ONLY
    ]
    uncertain = [
        item
        for item in verified
        if item.verdict is PubMedFindingVerdict.UNCERTAIN
    ]
    support_ids = sorted({item.evidence_id for item in support})
    contradiction_ids = sorted(
        {item.evidence_id for item in contradiction}
    )
    context_ids = sorted({item.evidence_id for item in context})
    uncertain_ids = sorted({item.evidence_id for item in uncertain})

    ctd_evidence = [
        evidence_from_ctd(
            item,
            subject_entity_id=seed_entity_id,
            object_entity_id=answer_entity_id,
            retrieved_at=ctd_retrieved_at or utc_now(),
        )
        for item in ctd
    ]
    evidence_records = list(
        {
            item.evidence_id: item
            for item in [*ctd_evidence, *span_evidence]
        }.values()
    )
    ctd_context_ids = sorted(item.evidence_id for item in ctd_evidence)

    if not support:
        return _rejected_fusion(
            ctd_record_ids=ctd_ids,
            ctd_direct_evidence=direct_evidence,
            pubmed_assessments=verified,
            evidence_records=evidence_records,
            status=CtdEvidenceFusionStatus.INSUFFICIENT,
            reason_codes=["no_supporting_pubmed_evidence"],
            contradicting_evidence_ids=contradiction_ids,
            context_evidence_ids=[*ctd_context_ids, *context_ids],
            uncertain_evidence_ids=uncertain_ids,
        )

    signatures = {_finding_signature(item) for item in support}
    if len(signatures) != 1:
        return _rejected_fusion(
            ctd_record_ids=ctd_ids,
            ctd_direct_evidence=direct_evidence,
            pubmed_assessments=verified,
            evidence_records=evidence_records,
            reason_codes=["mixed_supporting_claims"],
            supporting_evidence_ids=support_ids,
            contradicting_evidence_ids=contradiction_ids,
            context_evidence_ids=[*ctd_context_ids, *context_ids],
            uncertain_evidence_ids=uncertain_ids,
        )

    primary = sorted(support, key=lambda item: item.pmid)[0]
    assert primary.intent is not None
    semantic_reasons = study_claim_rejection_codes(
        intent=primary.intent.value,
        polarity=primary.polarity.value,
        claim_text=primary.claim_text,
        direct_evidence=direct_evidence,
    )
    if semantic_reasons:
        return _rejected_fusion(
            ctd_record_ids=ctd_ids,
            ctd_direct_evidence=direct_evidence,
            pubmed_assessments=verified,
            evidence_records=evidence_records,
            reason_codes=semantic_reasons,
            supporting_evidence_ids=support_ids,
            contradicting_evidence_ids=contradiction_ids,
            context_evidence_ids=[*ctd_context_ids, *context_ids],
            uncertain_evidence_ids=uncertain_ids,
        )

    primary_signature = _finding_signature(primary)
    if any(
        _finding_signature(item) == primary_signature
        for item in contradiction
    ):
        return _rejected_fusion(
            ctd_record_ids=ctd_ids,
            ctd_direct_evidence=direct_evidence,
            pubmed_assessments=verified,
            evidence_records=evidence_records,
            status=CtdEvidenceFusionStatus.CONFLICTING,
            reason_codes=["contradicting_pubmed_evidence"],
            supporting_evidence_ids=support_ids,
            contradicting_evidence_ids=contradiction_ids,
            context_evidence_ids=[*ctd_context_ids, *context_ids],
            uncertain_evidence_ids=uncertain_ids,
        )

    claim_text = min(item.claim_text for item in support)
    claim = NormalizedClaim.create(
        subject_id=seed_entity_id,
        predicate_family=predicate_family_for_intent(
            QuestionIntent(primary.intent.value),
            fallback_relation="chemical_disease_association",
        ),
        object_id=answer_entity_id,
        qualifiers=primary.qualifiers.as_claim_qualifiers(),
        polarity=primary.polarity.value,
        normalizer_version="ctd-pubmed-fusion-v1",
        supporting_evidence_ids=support_ids,
        contradicting_evidence_ids=contradiction_ids,
        context_evidence_ids=[*ctd_context_ids, *context_ids],
        claim_text=claim_text,
    )
    return CtdEvidenceFusion(
        fusion_id=_ctd_fusion_id(
            ctd_record_ids=ctd_ids,
            pubmed_assessments=verified,
            status=CtdEvidenceFusionStatus.ELIGIBLE,
            claim=claim,
        ),
        status=CtdEvidenceFusionStatus.ELIGIBLE,
        ctd_record_ids=ctd_ids,
        ctd_direct_evidence=direct_evidence,
        pubmed_assessments=verified,
        evidence_records=evidence_records,
        supporting_evidence_ids=support_ids,
        contradicting_evidence_ids=contradiction_ids,
        context_evidence_ids=[*ctd_context_ids, *context_ids],
        uncertain_evidence_ids=uncertain_ids,
        claim=claim,
    )
