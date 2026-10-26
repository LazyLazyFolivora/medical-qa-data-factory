"""The acceptance runner.

Step 11 of the real-generation-agent plan. The runner executes the pinned
manifest and produces a report that says what happened, including what did
not work.

The report is deliberately blunt. It records the seeds that produced
nothing, the questions that never reached the queue, the coverage gaps, the
stop reasons and the objective usage. A run that mostly failed is a useful
result; a run that looks clean because the failures are hidden is not.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..domain.common import new_id, utc_now
from ..domain.questions import QuestionSpec, ReviewDisposition
from ..generation.run_identity import RequiredEvidenceMode
from .manifest import EvalManifest, EvalSeed, verify_against_environment


@dataclass
class SeedOutcome:
    """What one seed produced."""

    seed_id: str
    canonical_name: str
    #: The episode the run produced.
    episode_id: str | None = None
    status: str = "not_run"
    stop_reason: str | None = None
    question_ids: list[str] = field(default_factory=list)
    #: The hops the run actually explored.
    hops_observed: list[int] = field(default_factory=list)
    #: The hops that never got text support.
    coverage_gaps: list[int] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: int = 0

    @property
    def produced_questions(self) -> bool:
        return bool(self.question_ids)


@dataclass
class AcceptanceReport:
    """The result of one acceptance run."""

    manifest_id: str
    manifest_hash: str
    started_at: str
    finished_at: str = ""
    #: The mode the run used.
    mode: str = "rule-based"
    #: The environment mismatches that make the run non-reproducible.
    environment_problems: list[str] = field(default_factory=list)
    seed_outcomes: list[SeedOutcome] = field(default_factory=list)
    #: The questions the run produced, by their route.
    route_counts: dict[str, int] = field(default_factory=dict)
    #: The questions that never reached the review queue.
    excluded_questions: list[dict[str, Any]] = field(default_factory=list)
    #: The number of questions the summary saw. It may exceed the seed
    #: outcomes when a workspace holds questions from an earlier run.
    summarized_question_count: int = 0
    #: The review groups the run produced.
    group_count: int = 0
    #: The hop complexities the run actually produced.
    hop_complexities: list[int] = field(default_factory=list)
    #: The paths the questions require.
    hop_paths: list[str] = field(default_factory=list)
    #: The sizes of those groups.
    group_sizes: list[int] = field(default_factory=list)
    #: How many candidates the reviewer kept, as a share of the decisions.
    kept_count: int = 0
    pending_count: int = 0
    #: The objective usage the run recorded.
    usage_totals: dict[str, Any] = field(default_factory=dict)
    #: The policy cost the run recorded.
    policy_cost: float = 0.0
    #: Whether the literature provider was eligible for a formal run.
    formal_rag_eligible: bool = False
    #: The structured verdict, filled in by the caller.
    verdict: str = "incomplete"
    notes: list[str] = field(default_factory=list)

    @property
    def question_count(self) -> int:
        """The questions the run produced.

        The seed outcomes are the source of truth, because they record what
        each seed actually yielded. When a run produced questions outside a
        seed, the summary count is used as well.
        """
        from_seeds = sum(len(item.question_ids) for item in self.seed_outcomes)
        return max(from_seeds, self.summarized_question_count)

    @property
    def kept_rate(self) -> float:
        decided = self.kept_count + self.pending_count
        if decided <= 0:
            return 0.0
        return self.kept_count / decided

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "manifest_hash": self.manifest_hash,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "verdict": self.verdict,
            "environment_problems": list(self.environment_problems),
            "question_count": self.question_count,
            "route_counts": dict(self.route_counts),
            "group_count": self.group_count,
            "hop_complexities": list(self.hop_complexities),
            "group_sizes": list(self.group_sizes),
            "kept_count": self.kept_count,
            "pending_count": self.pending_count,
            "kept_rate": round(self.kept_rate, 4),
            "usage_totals": dict(self.usage_totals),
            "policy_cost": self.policy_cost,
            "formal_rag_eligible": self.formal_rag_eligible,
            "seed_outcomes": [
                {
                    "seed_id": item.seed_id,
                    "canonical_name": item.canonical_name,
                    "status": item.status,
                    "stop_reason": item.stop_reason,
                    "question_ids": list(item.question_ids),
                    "hops_observed": list(item.hops_observed),
                    "coverage_gaps": list(item.coverage_gaps),
                    "error": item.error,
                    "elapsed_ms": item.elapsed_ms,
                }
                for item in self.seed_outcomes
            ],
            "excluded_questions": list(self.excluded_questions),
            "notes": list(self.notes),
        }

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return target


def summarize_questions(
    report: AcceptanceReport,
    questions: Sequence[QuestionSpec],
) -> AcceptanceReport:
    """Fill the route counts, the group statistics and the disposition counts."""
    routes: dict[str, int] = {}
    hops: set[int] = set()
    for question in questions:
        key = question.verified_route.value
        routes[key] = routes.get(key, 0) + 1
        complexity = question.taxonomy.hop_complexity
        if complexity is not None:
            hops.add(complexity)
        requirements = question.evidence_requirements
        for path_id in requirements.required_path_ids:
            report.hop_paths.append(path_id)
    report.route_counts = routes
    report.hop_complexities = sorted(hops)
    report.summarized_question_count = len(questions)
    groups: dict[str, list[QuestionSpec]] = {}
    for question in questions:
        key = question.review_group_key
        if key:
            groups.setdefault(key, []).append(question)
    report.group_count = len(groups)
    report.group_sizes = sorted(
        (len(members) for members in groups.values()), reverse=True
    )
    report.kept_count = sum(
        1
        for question in questions
        if question.review_disposition is ReviewDisposition.KEEP_INDEPENDENT
    )
    report.pending_count = sum(
        1
        for question in questions
        if question.review_disposition is ReviewDisposition.PENDING
    )
    return report


def attach_seed_details(
    report: AcceptanceReport,
    questions: Sequence[QuestionSpec],
) -> AcceptanceReport:
    """Record, per seed, the hops it reached and the coverage it lacked.

    The hop count and the gaps come from the questions themselves, so the
    report states what the run actually produced rather than what the
    manifest asked for.
    """
    by_seed: dict[str, list[QuestionSpec]] = {}
    for question in questions:
        for outcome in report.seed_outcomes:
            if question.question_id in outcome.question_ids:
                by_seed.setdefault(outcome.seed_id, []).append(question)
    for outcome in report.seed_outcomes:
        members = by_seed.get(outcome.seed_id, [])
        hops: set[int] = set()
        gaps: set[int] = set()
        for question in members:
            complexity = question.taxonomy.hop_complexity
            if complexity is not None:
                hops.add(complexity)
            # A question whose literature requirement is unmet has a gap at
            # the hop its text support should have covered.
            required = question.evidence_requirements
            if required.required_literature_evidence_ids:
                continue
            if complexity and complexity > 1:
                gaps.add(complexity - 1)
        outcome.hops_observed = sorted(hops)
        outcome.coverage_gaps = sorted(gaps)
    return report


def finalize(report: AcceptanceReport) -> AcceptanceReport:
    """Decide the verdict.

    The verdict is about the run, not about the data. A run that produced
    nothing is a failed run, and saying so is the point of the report.
    """
    report.finished_at = utc_now().isoformat()
    if report.environment_problems:
        report.verdict = "not_reproducible"
        report.notes.append(
            "the environment does not match the pinned manifest"
        )
        return report
    if report.question_count == 0:
        report.verdict = "failed"
        report.notes.append("the run produced no question")
        return report
    if not report.formal_rag_eligible:
        report.notes.append(
            "the literature provider is not eligible for a formal run: "
            f"{report.notes[-1] if report.notes else 'see the manifest'}"
        )
    report.verdict = "passed"
    return report


def build_report(
    manifest: EvalManifest,
    *,
    mode: str,
    environment_problems: Sequence[str] = (),
) -> AcceptanceReport:
    """A fresh report for one run."""
    return AcceptanceReport(
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash(),
        started_at=utc_now().isoformat(),
        mode=mode,
        environment_problems=list(environment_problems),
        formal_rag_eligible=manifest.formal_rag_eligible(),
    )