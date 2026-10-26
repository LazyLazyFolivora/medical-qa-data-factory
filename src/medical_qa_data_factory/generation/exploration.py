"""The exploration loop: propose, gate, execute, observe.

Step 5 and Step 6 of the real-generation-agent plan. This module wires the
three pieces together into a turn:

    planner.propose -> policy gate -> executor.execute -> observation

The loop stops when the model asks to finish, when the policy refuses every
proposal, when two consecutive turns produce no new evidence, or when the
budget runs out. A refusal never ends the run on its own: the model gets the
refusal reason and may try something else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..domain.common import ContractModel, UtcDateTime, new_id, utc_now
from .actions import (
    ModelActionProposal,
    PolicyGate,
    PolicyRejection,
    PolicyRejectionCode,
    RunPolicy,
    SealedAction,
)
from .contracts import ExplorationActionType, StopReason, ToolObservation
from .executor import ExecutionOutcome, Executor
from .planner import PlannerContext, PlannerPort, build_planner_context


class ExplorationStop(str):
    """Why the exploration loop ended."""


@dataclass
class ExplorationTurn:
    """One turn of the loop, with everything that happened in it."""

    turn: int
    proposal: ModelActionProposal | None = None
    sealed: SealedAction | None = None
    rejection: PolicyRejection | None = None
    outcome: ExecutionOutcome | None = None
    observation: ToolObservation | None = None

    @property
    def was_refused(self) -> bool:
        return self.sealed is None

    @property
    def produced_new_evidence(self) -> bool:
        if self.outcome is None:
            return False
        return bool(self.outcome.new_evidence_ids or self.outcome.new_path_ids)


@dataclass
class ExplorationResult:
    """The whole run of the exploration loop."""

    turns: list[ExplorationTurn] = field(default_factory=list)
    stop_reason: StopReason | None = None
    stop_message: str = ""
    known_entity_ids: set[str] = field(default_factory=set)
    executed_action_hashes: list[str] = field(default_factory=list)

    @property
    def observations(self) -> list[ToolObservation]:
        return [item.observation for item in self.turns if item.observation]

    @property
    def refusals(self) -> list[PolicyRejection]:
        return [item.rejection for item in self.turns if item.rejection]

    @property
    def tool_call_count(self) -> int:
        return sum(1 for item in self.turns if item.sealed is not None)

    @property
    def reached_evidence_sufficiency(self) -> bool:
        return self.stop_reason is StopReason.EVIDENCE_SUFFICIENT


class ExplorationLoop:
    """Runs bounded turns of propose, gate, execute and observe."""

    def __init__(
        self,
        *,
        planner: PlannerPort,
        executor: Executor,
        policy: RunPolicy,
        run_id: str,
        episode_id: str,
        seed_entity_id: str,
        seed_entity_name: str,
        available_tools: Sequence[str] = (),
        open_needs: Sequence[dict[str, Any]] = (),
        #: The hook that decides whether the collected evidence is enough.
        sufficiency: Any = None,
    ) -> None:
        self.planner = planner
        self.executor = executor
        self.policy = policy
        self.run_id = run_id
        self.episode_id = episode_id
        self.seed_entity_id = seed_entity_id
        self.seed_entity_name = seed_entity_name
        self.available_tools = list(available_tools)
        self.open_needs = [dict(item) for item in open_needs]
        self.sufficiency = sufficiency
        self.known_entity_ids: set[str] = {seed_entity_id}
        self.observations: list[ToolObservation] = []
        self.executed_action_hashes: list[str] = []
        self._last_refusal: str = ""

    def run(self, max_turns: int | None = None) -> ExplorationResult:
        """Run the loop until it stops."""
        result = ExplorationResult()
        limit = max_turns if max_turns is not None else self.policy.max_turns
        turns_without_evidence = 0
        turn = 0
        while turn < limit:
            turn += 1
            context = self._context(turn)
            proposal = self._propose(context)
            if proposal is None:
                result.stop_reason = StopReason.MODEL_ERROR
                result.stop_message = (
                    "the model did not return a usable proposal"
                )
                break
            gate = PolicyGate(self.policy)
            sealed, rejection = gate.evaluate(
                proposal,
                run_id=self.run_id,
                episode_id=self.episode_id,
                turn=turn,
                known_entity_ids=self.known_entity_ids,
                known_observation_ids={
                    item.observation_id for item in self.observations
                },
            )
            if sealed is None:
                assert rejection is not None
                self._last_refusal = rejection.message
                result.turns.append(
                    ExplorationTurn(
                        turn=turn, proposal=proposal, rejection=rejection
                    )
                )
                # A refusal does not end the run. The model is told why and
                # may try something else, until the turn budget runs out.
                turns_without_evidence += 1
                if turns_without_evidence >= 2:
                    result.stop_reason = StopReason.NO_VALID_ACTION
                    result.stop_message = (
                        "two consecutive proposals were refused: "
                        f"{self._last_refusal}"
                    )
                    break
                continue
            self._last_refusal = ""
            if sealed.action_type is ExplorationActionType.FINISH:
                result.turns.append(
                    ExplorationTurn(turn=turn, proposal=proposal, sealed=sealed)
                )
                result.stop_reason = StopReason.EVIDENCE_SUFFICIENT
                result.stop_message = "the model asked to finish"
                break
            outcome = self.executor.execute(
                sealed, episode_id=self.episode_id, run_id=self.run_id
            )
            self.observations.append(outcome.observation)
            self.known_entity_ids.update(outcome.discovered_entity_ids)
            self.executed_action_hashes.append(sealed.canonical_action_hash)
            self.policy = self.policy.with_execution(
                sealed.canonical_action_hash
            ).with_evidence()
            if outcome.new_evidence_ids or outcome.new_path_ids:
                turns_without_evidence = 0
            else:
                turns_without_evidence += 1
            result.turns.append(
                ExplorationTurn(
                    turn=turn,
                    proposal=proposal,
                    sealed=sealed,
                    outcome=outcome,
                    observation=outcome.observation,
                )
            )
            if turns_without_evidence >= 2:
                result.stop_reason = StopReason.NO_NEW_EVIDENCE
                result.stop_message = (
                    "two consecutive actions produced no new evidence"
                )
                break
            if self.sufficiency is not None and self._is_sufficient():
                result.stop_reason = StopReason.EVIDENCE_SUFFICIENT
                result.stop_message = "the evidence is sufficient"
                break
        else:
            result.stop_reason = StopReason.BUDGET_EXHAUSTED
            result.stop_message = f"the turn budget of {limit} was exhausted"
        result.known_entity_ids = set(self.known_entity_ids)
        result.executed_action_hashes = list(self.executed_action_hashes)
        return result

    def _propose(self, context: PlannerContext) -> ModelActionProposal | None:
        try:
            return self.planner.propose(context, ModelActionProposal)
        except Exception:
            return None

    def _is_sufficient(self) -> bool:
        if self.sufficiency is None:
            return False
        try:
            return bool(self.sufficiency(self.observations))
        except Exception:
            return False

    def _context(self, turn: int) -> PlannerContext:
        return build_planner_context(
            run_id=self.run_id,
            episode_id=self.episode_id,
            turn=turn,
            seed_entity_id=self.seed_entity_id,
            seed_entity_name=self.seed_entity_name,
            known_entities=[
                {"canonical_id": item} for item in sorted(self.known_entity_ids)
            ],
            observations=self.observations,
            open_needs=self.open_needs,
            evidence_gaps=[self._last_refusal] if self._last_refusal else [],
            executed_action_hashes=self.executed_action_hashes,
            policy=self.policy,
            available_tools=self.available_tools,
        )