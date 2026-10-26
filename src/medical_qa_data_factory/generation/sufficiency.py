"""The sufficiency judge: the model proposes, the code decides.

Step 6 of the real-generation-agent plan. The judge answers one question: can
the current evidence carry a question?

The model's verdict is advisory. The deterministic envelope is what actually
decides, because the model cannot see the whole evidence registry, the
snapshot, the coverage report or the budget. A model saying "sufficient" must
not be able to bypass a broken path, a missing text support, an unresolved
conflict or an exhausted budget.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, Sequence

from pydantic import Field

from ..domain.common import ContractModel, UtcDateTime
from .probe import (
    AnswerabilityProbe,
    GraphEvaluator,
    ProbeExecution,
    ProbeStatus,
)
from .run_identity import RequiredEvidenceMode


class SufficiencyVerdict(str, Enum):
    """The three outcomes the judge can produce."""

    CONTINUE = "continue"
    SUFFICIENT = "sufficient"
    INSUFFICIENT_TERMINAL = "insufficient_terminal"


class GuardrailCode(str, Enum):
    """The deterministic reasons a proposal was overridden."""

    NO_PROBE = "no_probe"
    PROBE_NOT_RUN = "probe_not_run"
    PROBE_NO_ANSWER = "probe_no_answer"
    PROBE_AMBIGUOUS = "probe_ambiguous"
    PROBE_INCOMPLETE = "probe_incomplete"
    PROBE_SNAPSHOT_MISMATCH = "probe_snapshot_mismatch"
    MISSING_TEXT_SUPPORT = "missing_text_support"
    UNRESOLVED_CONFLICT = "unresolved_conflict"
    NO_BUDGET = "no_budget"
    NO_VALID_ACTION = "no_valid_action"
    PATH_BROKEN = "path_broken"
    CARDINALITY_MISMATCH = "cardinality_mismatch"


class SufficiencyProposal(ContractModel):
    """What the model proposes about sufficiency.

    It is a proposal, like every other model output: the id, the turn and the
    final verdict belong to the server.
    """

    verdict: str = Field(pattern="^(insufficient|sufficient|generate)$")
    evidence_gaps: list[str] = Field(default_factory=list)
    cited_evidence_ids: list[str] = Field(default_factory=list)
    cited_path_ids: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)


class SufficiencyEnvelope(ContractModel):
    """The server's decision about sufficiency."""

    decision_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    turn: int = Field(ge=1)
    verdict: SufficiencyVerdict
    evidence_gaps: list[str] = Field(default_factory=list)
    cited_evidence_ids: list[str] = Field(default_factory=list)
    cited_path_ids: list[str] = Field(default_factory=list)
    suggested_action_type: str | None = None
    stop_reason: str | None = None
    rationale: str = Field(min_length=1)
    #: The guardrails that overrode the model's proposal.
    guardrail_codes: list[str] = Field(default_factory=list)
    #: The probe that was executed, and its result.
    probe_id: str | None = None
    probe_digest: str | None = None
    probe_status: str | None = None
    answer_ids: list[str] = Field(default_factory=list)
    created_at: UtcDateTime


@dataclass
class CoverageState:
    """The coverage facts the judge needs.

    These come from the coverage report, not from the model. A hop without a
    supporting text passage is a gap, whatever the model believes.
    """

    #: The hops that have at least one supporting text passage.
    supported_hop_positions: set[int] = field(default_factory=set)
    #: The hops that still need text support.
    uncovered_hop_positions: set[int] = field(default_factory=set)
    #: The evidence ids that contradict a need.
    contradicting_evidence_ids: set[str] = field(default_factory=set)
    #: Gaps that block the question.
    blocking_gaps: list[str] = field(default_factory=list)
    #: The mode the run is required to satisfy.
    required_evidence_mode: RequiredEvidenceMode = RequiredEvidenceMode.KG_ONLY
    #: Whether the run may still act.
    has_valid_action: bool = True
    remaining_tool_calls: int = 0

    def text_coverage_complete(self) -> bool:
        """Whether every hop of a multi-hop path has text support."""
        if self.required_evidence_mode is not RequiredEvidenceMode.HYBRID_PER_HOP:
            return True
        return not self.uncovered_hop_positions


class SufficiencyJudgePort(Protocol):
    """The model side of the judge."""

    def propose(
        self, payload: dict[str, Any]
    ) -> SufficiencyProposal:  # pragma: no cover - interface
        raise NotImplementedError


class SufficiencyJudge:
    """Turns a proposal and the hard facts into a decision."""

    def __init__(
        self,
        *,
        evaluator: GraphEvaluator | None = None,
        model: SufficiencyJudgePort | None = None,
    ) -> None:
        self.evaluator = evaluator
        self.model = model

    def judge(
        self,
        *,
        episode_id: str,
        turn: int,
        probe: AnswerabilityProbe | None,
        coverage: CoverageState,
        proposal: SufficiencyProposal | None = None,
    ) -> SufficiencyEnvelope:
        """Decide whether the evidence is sufficient.

        The deterministic guardrails run first. A model that says "sufficient"
        cannot move past a broken probe or a missing text support.
        """
        gaps: list[str] = []
        guardrails: list[str] = []
        execution: ProbeExecution | None = None

        if probe is None:
            guardrails.append(GuardrailCode.NO_PROBE.value)
            gaps.append("no answerability probe was built for the current path")
        else:
            execution = self._execute(probe)
            if execution.status is ProbeStatus.NOT_RUN:
                guardrails.append(GuardrailCode.PROBE_NOT_RUN.value)
                gaps.append("the answerability probe was not executed")
            elif execution.status is ProbeStatus.NO_ANSWER:
                guardrails.append(GuardrailCode.PROBE_NO_ANSWER.value)
                gaps.append(
                    "the probe found no answer on the fixed snapshot"
                )
            elif execution.status is ProbeStatus.AMBIGUOUS:
                guardrails.append(GuardrailCode.PROBE_AMBIGUOUS.value)
                gaps.append(
                    execution.failure_reason
                    or "the probe found more than one answer"
                )
            elif execution.status is ProbeStatus.INCOMPLETE:
                guardrails.append(GuardrailCode.PROBE_INCOMPLETE.value)
                gaps.append(
                    execution.failure_reason
                    or "the probe result was incomplete"
                )

        if not coverage.text_coverage_complete():
            guardrails.append(GuardrailCode.MISSING_TEXT_SUPPORT.value)
            gaps.append(
                "hops without supporting text evidence: "
                f"{sorted(coverage.uncovered_hop_positions)}"
            )
        if coverage.contradicting_evidence_ids:
            guardrails.append(GuardrailCode.UNRESOLVED_CONFLICT.value)
            gaps.append(
                "unresolved contradicting evidence: "
                f"{sorted(coverage.contradicting_evidence_ids)}"
            )
        if coverage.blocking_gaps:
            gaps.extend(coverage.blocking_gaps)
        if coverage.remaining_tool_calls <= 0 and gaps:
            guardrails.append(GuardrailCode.NO_BUDGET.value)
        if not coverage.has_valid_action and gaps:
            guardrails.append(GuardrailCode.NO_VALID_ACTION.value)

        stop_reason: str | None = None
        if not guardrails:
            verdict = SufficiencyVerdict.SUFFICIENT
            rationale = (
                proposal.rationale
                if proposal is not None
                else "the probe returned a clean answer and the coverage is complete"
            )
            stop_reason = "evidence_sufficient"
        elif coverage.remaining_tool_calls <= 0 or not coverage.has_valid_action:
            verdict = SufficiencyVerdict.INSUFFICIENT_TERMINAL
            rationale = (
                "the evidence cannot carry a question and no further action is "
                "available: " + "; ".join(gaps)
            )
            stop_reason = "no_valid_action"
        else:
            verdict = SufficiencyVerdict.CONTINUE
            rationale = "; ".join(gaps)

        if proposal is not None and proposal.evidence_gaps:
            for gap in proposal.evidence_gaps:
                if gap not in gaps:
                    gaps.append(gap)

        return SufficiencyEnvelope(
            decision_id=_new_id(),
            episode_id=episode_id,
            turn=turn,
            verdict=verdict,
            evidence_gaps=gaps,
            cited_evidence_ids=list(
                proposal.cited_evidence_ids if proposal else []
            ),
            cited_path_ids=list(proposal.cited_path_ids if proposal else []),
            stop_reason=stop_reason,
            rationale=rationale,
            guardrail_codes=guardrails,
            probe_id=probe.probe_id if probe else None,
            probe_digest=probe.dependency_digest if probe else None,
            probe_status=execution.status.value if execution else None,
            answer_ids=list(execution.answer_ids) if execution else [],
            created_at=_now(),
        )

    def _execute(self, probe: AnswerabilityProbe) -> ProbeExecution:
        if self.evaluator is None:
            return ProbeExecution(
                probe_id=probe.probe_id, status=ProbeStatus.NOT_RUN
            )
        return self.evaluator.evaluate(probe)


def _new_id() -> str:
    from ..domain.common import new_id

    return new_id("sufficiency")


def _now():
    from ..domain.common import utc_now

    return utc_now()


def coverage_state_from_assessments(
    *,
    hop_positions: Sequence[int],
    supported: Sequence[int],
    contradicting: Sequence[str] = (),
    required_evidence_mode: RequiredEvidenceMode = RequiredEvidenceMode.KG_ONLY,
    blocking_gaps: Sequence[str] = (),
    has_valid_action: bool = True,
    remaining_tool_calls: int = 0,
) -> CoverageState:
    """Build the coverage facts from the assessment records."""
    supported_set = set(supported)
    all_hops = set(hop_positions)
    uncovered = all_hops - supported_set
    if required_evidence_mode is not RequiredEvidenceMode.HYBRID_PER_HOP:
        uncovered = set()
    return CoverageState(
        supported_hop_positions=supported_set,
        uncovered_hop_positions=uncovered,
        contradicting_evidence_ids=set(contradicting),
        blocking_gaps=list(blocking_gaps),
        required_evidence_mode=required_evidence_mode,
        has_valid_action=has_valid_action,
        remaining_tool_calls=remaining_tool_calls,
    )