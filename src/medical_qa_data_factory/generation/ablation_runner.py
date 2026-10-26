"""The ablation runner: three isolated solver episodes.

Step 8 of the real-generation-agent plan. Each mode gets its own episode with
its own tool allow-list and its own budget, and none of them may see the gold
answer, the gold evidence or the writer's rationale.

The runner is deliberately thin. It owns the isolation and the bookkeeping;
the actual solving is done by a solver port that is told which mode it is
serving and which tools it may use.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from ..domain.common import utc_now
from ..domain.questions import AblationMode
from .ablation import (
    AblationEpisode,
    AblationOutcome,
    MODE_TOOLS,
)


class AblationSolverPort(Protocol):
    """Solves a question under one mode's restrictions."""

    def solve(
        self,
        *,
        mode: AblationMode,
        allowed_tools: Sequence[str],
        question_text: str,
        budget: float,
    ) -> dict[str, Any]: ...


@dataclass
class IsolatedAblationRunner:
    """Runs the three ablation episodes in isolation.

    The solver is handed the question text and the tool allow-list. It is
    never handed the gold answer, the gold evidence or the writer's
    rationale, so it cannot confirm the answer it is supposed to be finding.
    """

    solver: AblationSolverPort
    #: The budget each episode gets. The three runs are independent, so each
    #: one gets the full budget.
    budget_per_episode: float = 10.0
    #: The provider snapshot each mode used.
    snapshot_ids: dict[AblationMode, list[str]] = field(default_factory=dict)
    trace_ids: dict[AblationMode, list[str]] = field(default_factory=dict)
    #: The question text the solver is asked. The gold is not part of it.
    question_text: str = ""

    def run(self, mode: AblationMode, *, episode_id: str) -> AblationEpisode:
        started = utc_now()
        allowed = list(MODE_TOOLS[mode])
        try:
            outcome = self.solver.solve(
                mode=mode,
                allowed_tools=allowed,
                question_text=self.question_text,
                budget=self.budget_per_episode,
            )
        except Exception as exc:
            return AblationEpisode(
                episode_id=episode_id,
                mode=mode,
                allowed_tools=allowed,
                snapshot_ids=list(self.snapshot_ids.get(mode, [])),
                trace_ids=list(self.trace_ids.get(mode, [])),
                gold_hidden=True,
                outcome=AblationOutcome.FAILED,
                failure_reason=f"{type(exc).__name__}: {exc}",
                started_at=started,
                completed_at=utc_now(),
            )
        answer_ids = [str(item) for item in (outcome.get("answer_ids") or [])]
        evidence_ids = [str(item) for item in (outcome.get("evidence_ids") or [])]
        answered = bool(answer_ids) or bool(outcome.get("answered"))
        return AblationEpisode(
            episode_id=episode_id,
            mode=mode,
            allowed_tools=allowed,
            snapshot_ids=list(self.snapshot_ids.get(mode, [])),
            trace_ids=list(self.trace_ids.get(mode, [])),
            gold_hidden=True,
            outcome=(
                AblationOutcome.ANSWERED if answered else AblationOutcome.NOT_ANSWERED
            ),
            answer_ids=answer_ids,
            evidence_ids=evidence_ids,
            evidence_coverage=float(outcome.get("evidence_coverage") or 0.0),
            failure_reason=outcome.get("failure_reason"),
            policy_cost=outcome.get("policy_cost"),
            started_at=started,
            completed_at=utc_now(),
        )


@dataclass
class GoldHidingSolver:
    """Wraps a solver and removes anything that came from the gold.

    This is a belt-and-braces measure: the inner solver is never given the
    gold, and if it somehow returns it, the wrapper drops it before the
    result reaches the report.
    """

    inner: AblationSolverPort
    gold_answer_ids: frozenset[str] = frozenset()
    gold_evidence_ids: frozenset[str] = frozenset()
    #: The fields that may never reach the inner solver.
    forbidden_fields: tuple[str, ...] = (
        "gold_answer",
        "gold_evidence",
        "selected_evidence",
        "writer_rationale",
        "rationale",
        "answer",
    )

    def solve(
        self,
        *,
        mode: AblationMode,
        allowed_tools: Sequence[str],
        question_text: str,
        budget: float,
    ) -> dict[str, Any]:
        result = dict(
            self.inner.solve(
                mode=mode,
                allowed_tools=allowed_tools,
                question_text=question_text,
                budget=budget,
            )
        )
        answer_ids = [
            str(item)
            for item in (result.get("answer_ids") or [])
            if str(item) not in self.gold_answer_ids
        ]
        evidence_ids = [
            str(item)
            for item in (result.get("evidence_ids") or [])
            if str(item) not in self.gold_evidence_ids
        ]
        # A field that leaked the gold is dropped, not sanitised.
        for field in self.forbidden_fields:
            result.pop(field, None)
        result["answer_ids"] = answer_ids
        result["evidence_ids"] = evidence_ids
        return result