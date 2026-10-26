"""The grouped review projection.

Step 10 of the real-generation-agent plan. A reviewer compares the candidates
that ask the same structural question, so the projection puts them side by
side: the question, the answer, the evidence path, the per-hop text support,
the verifier's verdict, the checks, the ranking factors and the version.

Nothing here decides anything. The projection is a view, and the ranking only
determines the order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..domain import EvidenceBundle, QuestionSpec
from ..domain.questions import ReviewDisposition
from ..generation.grouping import REVIEW_GROUP_VERSION
from ..generation.ranking import RankingAssessment, sort_candidates
from ..repository import LocalArtifactRepository


@dataclass
class CandidateView:
    """One candidate in a group, with everything a reviewer compares."""

    question: QuestionSpec
    #: The ranking factors, so the reviewer sees why the candidate sits here.
    ranking_factors: list[dict[str, Any]] = field(default_factory=list)
    ranking_score: float = 0.0
    #: The blocking checks that failed, if any.
    failed_checks: list[dict[str, Any]] = field(default_factory=list)
    #: The advisory checks, which point at comparisons without deciding them.
    advisory_checks: list[dict[str, Any]] = field(default_factory=list)
    #: The evidence ids the question requires.
    required_evidence_ids: list[str] = field(default_factory=list)
    #: The per-hop text passages, when the bundle carries them.
    hop_passages: list[dict[str, Any]] = field(default_factory=list)
    #: The KG edges the question rests on.
    kg_edges: list[dict[str, Any]] = field(default_factory=list)
    #: Whether the question may be kept as an independent question.
    can_keep_independent: bool = True
    #: Why it may not, when it may not.
    blocked_reason: str | None = None

    @property
    def disposition(self) -> ReviewDisposition:
        return self.question.review_disposition

    @property
    def has_blocking_failure(self) -> bool:
        return bool(self.failed_checks)


@dataclass
class GroupView:
    """One review group: the candidates that ask the same structural question."""

    review_group_key: str
    review_group_version: str = REVIEW_GROUP_VERSION
    candidates: list[CandidateView] = field(default_factory=list)
    #: The relation chain the group is about, for the heading.
    relations: list[str] = field(default_factory=list)
    seed_entity_id: str | None = None
    seed_entity_name: str | None = None

    @property
    def size(self) -> int:
        return len(self.candidates)

    @property
    def has_pending(self) -> bool:
        return any(
            candidate.disposition is ReviewDisposition.PENDING
            for candidate in self.candidates
        )

    @property
    def kept_count(self) -> int:
        return sum(
            1
            for candidate in self.candidates
            if candidate.disposition is ReviewDisposition.KEEP_INDEPENDENT
        )

    @property
    def variant_count(self) -> int:
        return sum(
            1
            for candidate in self.candidates
            if candidate.disposition is ReviewDisposition.VARIANT_OF
        )

    @property
    def rejected_count(self) -> int:
        return sum(
            1
            for candidate in self.candidates
            if candidate.disposition is ReviewDisposition.REJECTED
        )


def build_candidate_view(
    question: QuestionSpec,
    bundle: EvidenceBundle | None,
) -> CandidateView:
    """One candidate, with the facts a reviewer compares."""
    view = CandidateView(question=question)
    assessment: RankingAssessment | None = question.ranking_assessment
    if assessment is not None:
        view.ranking_score = assessment.total_score
        view.ranking_factors = [
            {
                "code": factor.code,
                "value": factor.value,
                "weight": factor.weight,
                "contribution": factor.contribution,
                "explanation": factor.explanation,
            }
            for factor in assessment.factors
        ]
    for check in question.validation_checks:
        entry = {
            "check_type": check.check_type,
            "status": check.status.value,
            "severity": check.severity.value,
            "message": check.message,
            "validator_version": check.validator_version,
        }
        if check.severity.value == "blocking":
            if check.status.value != "passed":
                view.failed_checks.append(entry)
        else:
            view.advisory_checks.append(entry)
    requirements = question.evidence_requirements
    view.required_evidence_ids = list(
        requirements.required_kg_evidence_ids
        + requirements.required_literature_evidence_ids
    )
    if bundle is not None:
        by_id = {item.evidence_id: item for item in bundle.kg_evidence}
        for evidence_id in requirements.required_kg_evidence_ids:
            edge = by_id.get(evidence_id)
            if edge is None:
                continue
            view.kg_edges.append(
                {
                    "evidence_id": edge.evidence_id,
                    "relation": edge.relation,
                    "head": edge.head.canonical_name,
                    "tail": edge.tail.canonical_name,
                    "source_database": edge.source_database,
                }
            )
        literature_by_id = {
            item.evidence_id: item for item in bundle.literature_evidence
        }
        for evidence_id in requirements.required_literature_evidence_ids:
            passage = literature_by_id.get(evidence_id)
            if passage is None:
                continue
            view.hop_passages.append(
                {
                    "evidence_id": passage.evidence_id,
                    "title": passage.title,
                    "text": passage.evidence_text,
                    "location": passage.location,
                    "retrieval_query": passage.retrieval_query,
                }
            )
    if question.review_disposition is ReviewDisposition.REJECTED:
        view.can_keep_independent = False
        view.blocked_reason = "该候选已被拒绝"
    return view


def build_groups(
    repository: LocalArtifactRepository,
) -> list[GroupView]:
    """The groups, each ordered by its ranking score."""
    questions = repository.list_latest_questions()
    bundles = {
        bundle.bundle_id: bundle for bundle in repository.list_latest_bundles()
    }
    grouped: dict[str, list[QuestionSpec]] = {}
    for question in questions:
        key = question.review_group_key
        if not key:
            # A question without a group cannot be compared with anything.
            continue
        grouped.setdefault(key, []).append(question)
    views: list[GroupView] = []
    for key, members in grouped.items():
        candidates = [
            build_candidate_view(
                question,
                bundles.get(question.bundle_ids[0]) if question.bundle_ids else None,
            )
            for question in members
        ]
        assessments = [
            (candidate.question.question_id, candidate.question.ranking_assessment)
            for candidate in candidates
            if candidate.question.ranking_assessment is not None
        ]
        order = (
            sort_candidates(assessments)
            if assessments
            else [candidate.question.question_id for candidate in candidates]
        )
        by_id = {candidate.question.question_id: candidate for candidate in candidates}
        ordered = [by_id[question_id] for question_id in order if question_id in by_id]
        ordered.extend(
            candidate
            for candidate in candidates
            if candidate.question.question_id not in by_id.keys() | set(order)
        )
        relations = [
            edge["relation"] for edge in (ordered[0].kg_edges if ordered else [])
        ]
        views.append(
            GroupView(
                review_group_key=key,
                candidates=ordered,
                relations=relations,
                seed_entity_id=_seed_id(members),
                seed_entity_name=_seed_name(members, bundles),
            )
        )
    return views


def _seed_id(questions: Sequence[QuestionSpec]) -> str | None:
    for question in questions:
        intent = question.query_intent or {}
        value = intent.get("seed_entity_id")
        if value:
            return str(value)
    return None


def _seed_name(
    questions: Sequence[QuestionSpec],
    bundles: dict[str, EvidenceBundle],
) -> str | None:
    for question in questions:
        for bundle_id in question.bundle_ids:
            bundle = bundles.get(bundle_id)
            if bundle is None or not bundle.seed_entities:
                continue
            entity = bundle.seed_entities[0]
            if entity.canonical_name:
                return entity.canonical_name
    return None