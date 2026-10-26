"""Source ablation: what would a solver with only one source have answered?

Step 8 of the real-generation-agent plan. A question's route label must not be
the label it was generated with. Three runs answer that:

* ``kg_only`` - a solver that may only touch the knowledge graph.
* ``literature_only`` - a solver that may only touch the literature.
* ``combined`` - a solver that may use both.

Each run gets its own episode, its own tool allow-list and its own budget, and
none of them may see the gold answer, the gold evidence or the writer's
rationale. The route is then computed from the three results by a
deterministic rule, so it is reproducible rather than asserted.

What the ablation finds never relaxes the generation requirement. A run that
was required to produce ``hybrid_per_hop`` evidence is not downgraded because
the ablation showed the knowledge graph alone would have sufficed: that
finding is about the question, not about the run's obligations.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, Sequence

from pydantic import Field

from ..domain.common import ContractModel, UtcDateTime
from ..domain.questions import (
    AblationMode,
    AblationResult,
    AblationStatus,
    Route,
)
from .run_identity import RequiredEvidenceMode


#: The ablation protocol version. A change to the rules must bump it.
ABLATION_CONFIG_VERSION = "ablation-v1"


class AblationOutcome(str, Enum):
    """Whether a mode was able to answer the question."""

    ANSWERED = "answered"
    NOT_ANSWERED = "not_answered"
    FAILED = "failed"


class AblationEpisode(ContractModel):
    """One isolated ablation run."""

    episode_id: str = Field(min_length=1)
    mode: AblationMode
    allowed_tools: list[str] = Field(default_factory=list)
    snapshot_ids: list[str] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)
    #: Whether the gold was hidden. A run that saw the gold may not verify a
    #: route.
    gold_hidden: bool = True
    outcome: AblationOutcome = AblationOutcome.NOT_ANSWERED
    answer_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_coverage: float = Field(default=0.0, ge=0, le=1)
    failure_reason: str | None = None
    policy_cost: float | None = None
    started_at: UtcDateTime
    completed_at: UtcDateTime


class AblationRunnerPort(Protocol):
    """Runs one ablation episode in isolation."""

    def run(
        self, mode: AblationMode, *, episode_id: str
    ) -> AblationEpisode: ...


class AblationReport(ContractModel):
    """The three runs and the route they determine."""

    question_id: str = Field(min_length=1)
    question_version: str = Field(min_length=1)
    #: The digest of the frozen question the runs were based on.
    question_dependency_digest: str = Field(min_length=1)
    ablation_config_version: str = ABLATION_CONFIG_VERSION
    episodes: list[AblationEpisode] = Field(default_factory=list)
    results: list[AblationResult] = Field(default_factory=list)
    #: The route the three runs determine.
    verified_route: Route = Route.UNKNOWN
    #: Why the route is what it is.
    route_rationale: str = ""
    #: The generation requirement of the run that produced the question. It is
    #: recorded so nobody can read the ablation as having relaxed it.
    required_evidence_mode: RequiredEvidenceMode | None = None
    created_at: UtcDateTime


def question_digest(payload: Any) -> str:
    """The digest of the frozen question the ablation runs are based on."""
    text = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: The tool allow-list per mode. A mode may not use a tool outside its list.
MODE_TOOLS: dict[AblationMode, tuple[str, ...]] = {
    AblationMode.KG_ONLY: ("kg_neighbors", "kg_paths"),
    AblationMode.LITERATURE_ONLY: ("literature_search", "literature_read_chunk"),
    AblationMode.COMBINED: (
        "kg_neighbors",
        "kg_paths",
        "literature_search",
        "literature_read_chunk",
    ),
}


def run_ablation(
    *,
    question_id: str,
    question_version: str,
    question_payload: Any,
    runner: AblationRunnerPort,
    gold_answer_ids: Sequence[str] = (),
    required_evidence_mode: RequiredEvidenceMode | None = None,
) -> AblationReport:
    """Run the three isolated episodes and compute the verified route.

    The three episodes never share state: each has its own id, its own tool
    allow-list and its own budget, and the runner is told which mode it is
    serving.
    """
    from ..domain.common import utc_now

    digest = question_digest(question_payload)
    episodes: list[AblationEpisode] = []
    results: list[AblationResult] = []
    for mode in (AblationMode.KG_ONLY, AblationMode.LITERATURE_ONLY, AblationMode.COMBINED):
        episode_id = f"ablation.{question_id}.{mode.value}"
        episode = runner.run(mode, episode_id=episode_id)
        episodes.append(episode)
        results.append(_to_result(episode, mode, digest, gold_answer_ids))
    route, rationale = compute_verified_route(episodes)
    return AblationReport(
        question_id=question_id,
        question_version=question_version,
        question_dependency_digest=digest,
        ablation_config_version=ABLATION_CONFIG_VERSION,
        episodes=episodes,
        results=results,
        verified_route=route,
        route_rationale=rationale,
        required_evidence_mode=required_evidence_mode,
        created_at=utc_now(),
    )


def _to_result(
    episode: AblationEpisode,
    mode: AblationMode,
    digest: str,
    gold_answer_ids: Sequence[str],
) -> AblationResult:
    """Turn one episode into a persisted ablation result."""
    answer_correct: bool | None = None
    if episode.outcome is AblationOutcome.ANSWERED and gold_answer_ids:
        answer_correct = sorted(episode.answer_ids) == sorted(gold_answer_ids)
    return AblationResult(
        mode=mode,
        status=(
            AblationStatus.COMPLETED
            if episode.outcome is not AblationOutcome.FAILED
            else AblationStatus.FAILED
        ),
        answer_correct=answer_correct,
        evidence_coverage=episode.evidence_coverage,
        final_decision=(
            "complete"
            if episode.outcome is AblationOutcome.ANSWERED
            else "insufficient"
        ),
        failure_reason=episode.failure_reason,
        episode_policy_cost=(
            {"normalized": episode.policy_cost} if episode.policy_cost is not None else None
        ),
        ablation_config_version=ABLATION_CONFIG_VERSION,
        question_dependency_digest=digest,
        episode_id=episode.episode_id,
        allowed_tools=list(episode.allowed_tools),
        gold_hidden=episode.gold_hidden,
        snapshot_ids=list(episode.snapshot_ids),
        trace_ids=list(episode.trace_ids),
        predicted_answer={"answer_ids": list(episode.answer_ids)},
        predicted_answer_ids=list(episode.answer_ids),
        gold_answer_ids=list(gold_answer_ids),
        evidence_ids=list(episode.evidence_ids),
    )


def compute_verified_route(
    episodes: Sequence[AblationEpisode],
) -> tuple[Route, str]:
    """Compute the route from the three episodes.

    The rule is deterministic and covers every combination:

    * only the combined run answered - the question needs both sources.
    * exactly one single source answered - the question needs that source.
    * both single sources answered - either will do; the question is redundant.
    * no run answered - the question is not answerable from these sources.
    * a run failed - that mode is not evidence either way.
    """
    by_mode = {episode.mode: episode for episode in episodes}
    kg = by_mode.get(AblationMode.KG_ONLY)
    literature = by_mode.get(AblationMode.LITERATURE_ONLY)
    combined = by_mode.get(AblationMode.COMBINED)
    kg_ok = kg is not None and kg.outcome is AblationOutcome.ANSWERED
    lit_ok = literature is not None and literature.outcome is AblationOutcome.ANSWERED
    both_ok = (
        combined is not None and combined.outcome is AblationOutcome.ANSWERED
    )
    if kg_ok and lit_ok:
        return (
            Route.REDUNDANT,
            "the knowledge graph alone and the literature alone both answered "
            "the question",
        )
    if kg_ok and not lit_ok and both_ok:
        return (
            Route.KG,
            "only the knowledge graph answered the question on its own",
        )
    if lit_ok and not kg_ok and both_ok:
        return (
            Route.LITERATURE,
            "only the literature answered the question on its own",
        )
    if not kg_ok and not lit_ok and both_ok:
        return (
            Route.HYBRID,
            "neither source answered the question alone, but both together did",
        )
    if kg_ok and not lit_ok and not both_ok:
        return (
            Route.KG,
            "the knowledge graph answered the question, but the combined run "
            "did not complete",
        )
    if lit_ok and not kg_ok and not both_ok:
        return (
            Route.LITERATURE,
            "the literature answered the question, but the combined run did "
            "not complete",
        )
    return (
        Route.UNANSWERABLE,
        "no source answered the question",
    )


def route_is_complete(report: AblationReport) -> bool:
    """Whether the report may verify a route.

    All three runs must be present, every one of them must have been blind to
    the gold, and the route must be determined. A run that saw the answer
    cannot be evidence about what the sources can answer on their own.
    """
    modes = {episode.mode for episode in report.episodes}
    if not {
        AblationMode.KG_ONLY,
        AblationMode.LITERATURE_ONLY,
        AblationMode.COMBINED,
    } <= modes:
        return False
    if not all(episode.gold_hidden for episode in report.episodes):
        return False
    return report.verified_route is not Route.UNKNOWN


def apply_verified_route(
    question: Any, report: AblationReport
) -> Any:
    """Write the verified route onto a question, without touching the
    generation requirement.

    The requirement is a field of the run, not of the question's difficulty.
    """
    from ..domain.common import ChangeRecord, new_id

    payload = question.model_dump()
    payload["verified_route"] = report.verified_route
    payload["ablation_results"] = [
        result.model_dump(mode="json") for result in report.results
    ]
    payload["changes"] = [
        *question.changes,
        ChangeRecord(
            change_id=new_id("change"),
            actor_type="system",
            operation="source_ablation",
            summary=(
                f"verified route {report.verified_route.value}: "
                f"{report.route_rationale}"
            ),
            created_at=report.created_at,
            parent_version=question.question_version,
            new_version=question.question_version,
        ),
    ]
    return type(question).model_validate(payload)