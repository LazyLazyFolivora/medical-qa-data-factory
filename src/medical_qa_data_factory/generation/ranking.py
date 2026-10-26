"""The explainable ranking within a review group.

Step 9 of the real-generation-agent plan. The ranking decides the order
candidates are shown in. It is not a quality verdict:

* It never changes a validation level, a review disposition or an export
  decision.
* Every contribution is shown, including the ones that cost a candidate
  points, so a reviewer can see why it sits where it does.
* The weights live in a versioned configuration, not in the code, so a
  calibration can be published without changing the implementation.

A candidate that failed a blocking check may still be shown, but it is marked
and its score is capped, so a broken candidate never outranks a sound one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..domain.common import ContractModel, UtcDateTime
from ..domain.questions import RankingAssessment, RankingFactor


#: The ranking rules version.
RANKING_VERSION = "ranking.v1"

#: The factor codes.
VERIFIER_ACCEPTED = "verifier_accepted"
EVIDENCE_COMPLETE = "evidence_complete"
ANSWER_DETERMINATE = "answer_determinate"
NO_SHORTCUT = "no_shorter_path"
NO_LEAKAGE = "no_leakage"
NATURALNESS = "naturalness"
LOW_TEMPLATE = "low_template_likeness"

#: The weight of each factor. The weights sum to 100.
RANKING_WEIGHTS: dict[str, float] = {
    VERIFIER_ACCEPTED: 30.0,
    EVIDENCE_COMPLETE: 20.0,
    ANSWER_DETERMINATE: 15.0,
    NO_SHORTCUT: 10.0,
    NO_LEAKAGE: 10.0,
    NATURALNESS: 10.0,
    LOW_TEMPLATE: 5.0,
}

#: The score a candidate with a blocking failure is capped at.
BLOCKED_SCORE_CAP = 20.0


@dataclass(frozen=True)
class RankingSignals:
    """The facts the ranking reads. Every one is observable."""

    verifier_accepted: bool = False
    evidence_complete: bool = False
    answer_determinate: bool = False
    has_shorter_path: bool = False
    leaks_answer: bool = False
    naturalness_score: float = 0.0
    template_likeness: float = 0.0
    #: Whether a blocking check failed for this candidate.
    blocking_failed: bool = False
    #: The candidate's naturalness and template scores are advisory and
    #: usually 0..1.
    explanation_overrides: dict[str, str] = field(default_factory=dict)


def rank_candidate(
    signals: RankingSignals,
    *,
    ranking_version: str = RANKING_VERSION,
    weights: dict[str, float] | None = None,
) -> RankingAssessment:
    """Score one candidate and explain every contribution."""
    from ..domain.common import utc_now

    table = dict(weights or RANKING_WEIGHTS)
    raw: list[tuple[str, float, float, str]] = [
        (
            VERIFIER_ACCEPTED,
            1.0 if signals.verifier_accepted else 0.0,
            table[VERIFIER_ACCEPTED],
            "the independent verifier accepted the question"
            if signals.verifier_accepted
            else "the independent verifier did not accept the question",
        ),
        (
            EVIDENCE_COMPLETE,
            1.0 if signals.evidence_complete else 0.0,
            table[EVIDENCE_COMPLETE],
            "every required evidence reference resolves"
            if signals.evidence_complete
            else "a required evidence reference does not resolve",
        ),
        (
            ANSWER_DETERMINATE,
            1.0 if signals.answer_determinate else 0.0,
            table[ANSWER_DETERMINATE],
            "the answer is unique and its cardinality matches"
            if signals.answer_determinate
            else "the answer is ambiguous or its cardinality does not match",
        ),
        (
            NO_SHORTCUT,
            0.0 if signals.has_shorter_path else 1.0,
            table[NO_SHORTCUT],
            "no shorter chain reaches the same answer"
            if not signals.has_shorter_path
            else "a shorter chain reaches the same answer",
        ),
        (
            NO_LEAKAGE,
            0.0 if signals.leaks_answer else 1.0,
            table[NO_LEAKAGE],
            "the question does not leak the answer"
            if not signals.leaks_answer
            else "the question leaks the answer or an intermediate entity",
        ),
        (
            NATURALNESS,
            max(0.0, min(1.0, signals.naturalness_score)),
            table[NATURALNESS],
            f"the naturalness score is {signals.naturalness_score:.2f}",
        ),
        (
            LOW_TEMPLATE,
            max(0.0, min(1.0, 1.0 - signals.template_likeness)),
            table[LOW_TEMPLATE],
            f"the template-likeness score is {signals.template_likeness:.2f}",
        ),
    ]
    factors: list[RankingFactor] = []
    total = 0.0
    for code, value, weight, explanation in raw:
        contribution = value * weight
        total += contribution
        factors.append(
            RankingFactor(
                code=code,
                value=value,
                weight=weight,
                contribution=contribution,
                explanation=signals.explanation_overrides.get(code, explanation),
            )
        )
    if signals.blocking_failed and total > BLOCKED_SCORE_CAP:
        total = BLOCKED_SCORE_CAP
    return RankingAssessment(
        ranking_version=ranking_version,
        total_score=round(total, 4),
        factors=factors,
        computed_at=utc_now(),
    )


def sort_candidates(
    assessments: Sequence[tuple[str, RankingAssessment]],
) -> list[str]:
    """The display order: highest score first, id as the tiebreaker.

    The tiebreaker keeps the order stable, so a reviewer sees the same
    sequence every time the group is rendered.
    """
    return [
        question_id
        for question_id, _ in sorted(
            assessments, key=lambda item: (-item[1].total_score, item[0])
        )
    ]


def explain(assessment: RankingAssessment) -> str:
    """A one-line summary of why a candidate sits where it does."""
    losses = [
        f"{factor.code} ({factor.contribution:+.1f})"
        for factor in assessment.factors
        if factor.contribution < factor.weight
    ]
    if not losses:
        return f"scored {assessment.total_score:.1f} with no deductions"
    return f"scored {assessment.total_score:.1f}; deductions: {', '.join(losses)}"