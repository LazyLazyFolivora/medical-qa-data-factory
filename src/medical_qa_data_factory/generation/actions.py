"""Model proposals, sealed actions, and the policy gate.

Step 5 of the real-generation-agent plan. The chain of custody is:

    ModelActionProposal  ->  ExplorationDecision  ->  SealedAction  ->  execution

The model may only propose. It never names its own decision id, turn, action
hash, budget or entity identity, and it never sees the tool name. The server
validates the proposal, seals an exact request, and only a sealed action may
reach the executor. An invalid proposal produces a policy rejection and zero
tool calls.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Protocol

from pydantic import Field, model_validator

from ..domain.common import ContractModel, UtcDateTime, new_id, utc_now
from .contracts import (
    ExplorationAction,
    ExplorationActionType,
    StopReason,
    validate_action,
)


#: Policy version for the action chain.
ACTION_POLICY_VERSION = "actions-v1"


class PolicyRejectionCode(str, Enum):
    """Why a proposal was refused before any tool ran."""

    UNKNOWN_ACTION_TYPE = "unknown_action_type"
    UNKNOWN_ENTITY = "unknown_entity"
    UNKNOWN_OBSERVATION = "unknown_observation"
    RELATION_NOT_ALLOWED = "relation_not_allowed"
    TARGET_TYPE_NOT_ALLOWED = "target_type_not_allowed"
    DIRECTION_NOT_ALLOWED = "direction_not_allowed"
    LIMIT_TOO_LARGE = "limit_too_large"
    HOP_BUDGET_EXCEEDED = "hop_budget_exceeded"
    CURSOR_INVALID = "cursor_invalid"
    NEED_NOT_FOUND = "need_not_found"
    DUPLICATE_ACTION = "duplicate_action"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MALFORMED_PROPOSAL = "malformed_proposal"
    FINISH_WITHOUT_EVIDENCE = "finish_without_evidence"


class ModelActionProposal(ContractModel):
    """What the model proposes. It carries no server-assigned identity.

    The four fields are the whole contract: one action, what the action should
    reveal, which gap it addresses, and the observations it is based on. There
    is no free-text reasoning field, because a stored chain of thought is
    neither auditable nor necessary.
    """

    action: dict[str, Any] = Field(default_factory=dict)
    expected_information: str = Field(min_length=1)
    evidence_gap: str = Field(min_length=1)
    cited_observation_ids: list[str] = Field(default_factory=list)

    def validated_action(self) -> ExplorationAction:
        """Validate the raw action against the whitelisted union."""
        return validate_action(self.action)


class AgentActionProposal(ContractModel):
    """Strict model-facing action schema.

    ``ModelActionProposal`` keeps the permissive historical shape used by
    policy tests. The model boundary uses this model so JSON Schema describes
    the discriminated action union instead of an unconstrained object.
    """

    action: ExplorationAction
    expected_information: str = Field(min_length=1)
    evidence_gap: str = Field(min_length=1)
    cited_observation_ids: list[str] = Field(default_factory=list)

    def as_model_proposal(self) -> ModelActionProposal:
        return ModelActionProposal(
            action=self.action.model_dump(mode="json"),
            expected_information=self.expected_information,
            evidence_gap=self.evidence_gap,
            cited_observation_ids=list(self.cited_observation_ids),
        )


class ExplorationDecision(ContractModel):
    """The server's record of one accepted decision.

    The ids and hashes are all server-assigned. The model never supplies
    them, so a model cannot forge a decision id or claim an action hash.
    """

    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    turn: int = Field(ge=1)
    action: ExplorationAction
    expected_information: str = Field(min_length=1)
    evidence_gap: str = Field(min_length=1)
    cited_observation_ids: list[str] = Field(default_factory=list)
    action_hash: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    created_at: UtcDateTime


class SealedAction(ContractModel):
    """An exact, server-approved request.

    The sealed action is the only thing the executor may run. It pins the
    entities, the filters, the cursor, the limit and the expected snapshot, so
    what runs is exactly what was approved.
    """

    sealed_action_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    canonical_action_hash: str = Field(min_length=1)
    action_type: ExplorationActionType
    #: The exact tool call, with every parameter resolved.
    request: dict[str, Any] = Field(default_factory=dict)
    request_digest: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    provider_capability_version: str | None = None
    #: The budget reservation this action is charged against.
    budget_reservation_id: str | None = None
    policy_version: str = Field(min_length=1)
    created_at: UtcDateTime


class PolicyRejection(ContractModel):
    """A refused proposal. The executor is never reached."""

    rejection_id: str = Field(min_length=1)
    decision_id: str | None = None
    run_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    turn: int = Field(ge=1)
    code: PolicyRejectionCode
    message: str = Field(min_length=1)
    action_type: str | None = None
    created_at: UtcDateTime


def canonical_action_hash(action: ExplorationAction) -> str:
    """A stable hash of the action's meaning.

    Two proposals that differ only in key order or in a parameter the policy
    ignores produce the same hash, so a repeated proposal is recognisable as
    a repeat.
    """
    payload = action.model_dump(mode="json", exclude_none=True)
    text = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_digest(payload: Any) -> str:
    """A stable digest of an exact request."""
    text = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunPolicy:
    """The limits a run may not exceed.

    A request may tighten these bounds, never widen them.
    """

    policy_version: str = ACTION_POLICY_VERSION
    max_turns: int = 12
    max_tool_calls: int = 24
    max_results_per_query: int = 50
    max_hops: int = 3
    #: The live edge budget. ``None`` preserves legacy callers that only
    #: enforce the per-query limit.
    remaining_edge_budget: int | None = None
    allowed_relations: tuple[str, ...] = ()
    allowed_target_types: tuple[str, ...] = ()
    allowed_directions: tuple[str, ...] = ("incoming", "outgoing", "both")
    #: Action types this run can actually execute. An empty tuple keeps the
    #: legacy gate permissive.
    allowed_action_types: tuple[str, ...] = ()
    max_literature_results: int = 20
    #: The literature needs that exist in this run. A literature action must
    #: serve one of them.
    open_need_ids: tuple[str, ...] = ()
    #: The action hashes already executed, so a repeat is refused.
    executed_action_hashes: frozenset[str] = frozenset()
    #: The tool capabilities the provider actually offers.
    available_tool_names: tuple[str, ...] = ()
    #: Whether the provider can pin an index snapshot.
    can_pin_snapshot: bool = False
    remaining_tool_calls: int = 24
    #: Whether any evidence has been collected yet. A finish before that is
    #: refused.
    has_evidence: bool = False
    kg_evidence_count: int = 0
    literature_evidence_count: int = 0
    min_total_evidence: int = 1
    min_kg_evidence: int = 0
    min_literature_evidence: int = 0

    def with_execution(
        self, action_hash: str, *, consumed_tool_call: bool = True
    ) -> "RunPolicy":
        """The policy after one action was executed."""
        return RunPolicy(
            policy_version=self.policy_version,
            max_turns=self.max_turns,
            max_tool_calls=self.max_tool_calls,
            max_results_per_query=self.max_results_per_query,
            max_hops=self.max_hops,
            remaining_edge_budget=self.remaining_edge_budget,
            allowed_relations=self.allowed_relations,
            allowed_target_types=self.allowed_target_types,
            allowed_directions=self.allowed_directions,
            allowed_action_types=self.allowed_action_types,
            max_literature_results=self.max_literature_results,
            open_need_ids=self.open_need_ids,
            executed_action_hashes=self.executed_action_hashes | {action_hash},
            available_tool_names=self.available_tool_names,
            can_pin_snapshot=self.can_pin_snapshot,
            remaining_tool_calls=max(
                self.remaining_tool_calls - (1 if consumed_tool_call else 0), 0
            ),
            has_evidence=self.has_evidence,
            kg_evidence_count=self.kg_evidence_count,
            literature_evidence_count=self.literature_evidence_count,
            min_total_evidence=self.min_total_evidence,
            min_kg_evidence=self.min_kg_evidence,
            min_literature_evidence=self.min_literature_evidence,
        )

    def with_evidence(
        self, *, kg_evidence_count: int = 0, literature_evidence_count: int = 0
    ) -> "RunPolicy":
        """The policy after evidence was collected."""
        total_kg = self.kg_evidence_count + max(kg_evidence_count, 0)
        total_lit = self.literature_evidence_count + max(
            literature_evidence_count, 0
        )
        return dataclasses.replace(
            self,
            has_evidence=True,
            kg_evidence_count=total_kg,
            literature_evidence_count=total_lit,
        )


class PolicyGate:
    """Validates a proposal and seals it, or refuses it.

    The gate is the only place that decides whether a tool may run. It never
    consults the model and never lets the model widen a budget.
    """

    def __init__(self, policy: RunPolicy) -> None:
        self.policy = policy

    def evaluate(
        self,
        proposal: ModelActionProposal,
        *,
        run_id: str,
        episode_id: str,
        turn: int,
        known_entity_ids: Iterable[str],
        known_observation_ids: Iterable[str] = (),
    ) -> tuple[SealedAction | None, PolicyRejection | None]:
        """Evaluate one proposal."""
        known_entities = set(known_entity_ids)
        known_observations = set(known_observation_ids)

        try:
            action = proposal.validated_action()
        except Exception as exc:
            return None, self._reject(
                run_id=run_id,
                episode_id=episode_id,
                turn=turn,
                code=PolicyRejectionCode.MALFORMED_PROPOSAL,
                message=f"the proposed action is not a known action: {exc}",
            )

        for observation_id in proposal.cited_observation_ids:
            if observation_id not in known_observations:
                return None, self._reject(
                    run_id=run_id,
                    episode_id=episode_id,
                    turn=turn,
                    code=PolicyRejectionCode.UNKNOWN_OBSERVATION,
                    message=(
                        f"the proposal cites observation {observation_id!r}, which "
                        "is not part of this run"
                    ),
                    action_type=action.action_type.value,
                )

        action_hash = canonical_action_hash(action)
        if (
            self.policy.allowed_action_types
            and action.action_type.value not in self.policy.allowed_action_types
        ):
            return None, self._reject(
                run_id=run_id,
                episode_id=episode_id,
                turn=turn,
                code=PolicyRejectionCode.CAPABILITY_UNAVAILABLE,
                message=(
                    f"the action {action.action_type.value!r} is not available "
                    "in this run"
                ),
                action_type=action.action_type.value,
            )
        if action_hash in self.policy.executed_action_hashes:
            return None, self._reject(
                run_id=run_id,
                episode_id=episode_id,
                turn=turn,
                code=PolicyRejectionCode.DUPLICATE_ACTION,
                message="this exact action was already executed in this run",
                action_type=action.action_type.value,
            )
        if self.policy.remaining_tool_calls <= 0:
            return None, self._reject(
                run_id=run_id,
                episode_id=episode_id,
                turn=turn,
                code=PolicyRejectionCode.BUDGET_EXHAUSTED,
                message="the run has no tool calls left",
                action_type=action.action_type.value,
            )

        request, refusal = self._seal_request(action, known_entities=known_entities)
        if refusal is not None:
            code, message = refusal
            return None, self._reject(
                run_id=run_id,
                episode_id=episode_id,
                turn=turn,
                code=code,
                message=message,
                action_type=action.action_type.value,
            )

        decision = ExplorationDecision(
            decision_id=new_id("decision"),
            run_id=run_id,
            episode_id=episode_id,
            turn=turn,
            action=action,
            expected_information=proposal.expected_information,
            evidence_gap=proposal.evidence_gap,
            cited_observation_ids=list(proposal.cited_observation_ids),
            action_hash=action_hash,
            policy_version=self.policy.policy_version,
            created_at=utc_now(),
        )
        tool_name = _tool_name_for(action)
        if self.policy.available_tool_names and (
            tool_name not in self.policy.available_tool_names
        ):
            return None, self._reject(
                run_id=run_id,
                episode_id=episode_id,
                turn=turn,
                code=PolicyRejectionCode.CAPABILITY_UNAVAILABLE,
                message=f"the tool {tool_name!r} is not available in this run",
                action_type=action.action_type.value,
            )
        sealed = SealedAction(
            sealed_action_id=new_id("sealed"),
            decision_id=decision.decision_id,
            canonical_action_hash=action_hash,
            action_type=action.action_type,
            request=request,
            request_digest=request_digest(request),
            tool_name=tool_name,
            provider_capability_version=(
                "weknora-v0.10" if tool_name.startswith("literature") else None
            ),
            policy_version=self.policy.policy_version,
            created_at=utc_now(),
        )
        return sealed, None

    def _seal_request(
        self, action: ExplorationAction, *, known_entities: set[str]
    ) -> tuple[dict[str, Any], tuple[PolicyRejectionCode, str] | None]:
        """Turn an action into an exact request, or refuse it.

        The refusal is a code and a message; the surrounding gate turns it into
        a full rejection record with the run identity.
        """
        if action.action_type in (
            ExplorationActionType.QUERY_NEIGHBORS,
            ExplorationActionType.EXPAND_ENTITY,
        ):
            if action.subject_entity_id not in known_entities:
                return {}, (
                    PolicyRejectionCode.UNKNOWN_ENTITY,
                    f"the subject entity {action.subject_entity_id!r} is not "
                    "known in this run",
                )
            if action.direction not in self.policy.allowed_directions:
                return {}, (
                    PolicyRejectionCode.DIRECTION_NOT_ALLOWED,
                    f"direction {action.direction!r} is not allowed",
                )
            if action.max_results > self.policy.max_results_per_query:
                return {}, (
                    PolicyRejectionCode.LIMIT_TOO_LARGE,
                    f"max_results {action.max_results} exceeds the policy "
                    f"limit {self.policy.max_results_per_query}",
                )
            if (
                self.policy.remaining_edge_budget is not None
                and action.max_results > self.policy.remaining_edge_budget
            ):
                return {}, (
                    PolicyRejectionCode.BUDGET_EXHAUSTED,
                    f"max_results {action.max_results} exceeds the remaining "
                    f"edge budget {self.policy.remaining_edge_budget}",
                )
            if self.policy.allowed_relations and action.relations:
                refused = [
                    relation
                    for relation in action.relations
                    if relation not in self.policy.allowed_relations
                ]
                if refused:
                    return {}, (
                        PolicyRejectionCode.RELATION_NOT_ALLOWED,
                        f"relations not allowed: {sorted(refused)}",
                    )
            if self.policy.allowed_target_types and action.target_types:
                refused = [
                    item
                    for item in action.target_types
                    if item not in self.policy.allowed_target_types
                ]
                if refused:
                    return {}, (
                        PolicyRejectionCode.TARGET_TYPE_NOT_ALLOWED,
                        f"target types not allowed: {sorted(refused)}",
                    )
            return (
                {
                    "operation": action.action_type.value,
                    "subject_entity_id": action.subject_entity_id,
                    "direction": action.direction,
                    "relations": list(action.relations),
                    "target_types": list(action.target_types),
                    "max_results": action.max_results,
                    "cursor": getattr(action, "cursor", None),
                },
                None,
            )
        if action.action_type is ExplorationActionType.QUERY_PATHS:
            if action.start_entity_id not in known_entities:
                return {}, (
                    PolicyRejectionCode.UNKNOWN_ENTITY,
                    f"the start entity {action.start_entity_id!r} is not known "
                    "in this run",
                )
            if action.max_hops > self.policy.max_hops:
                return {}, (
                    PolicyRejectionCode.HOP_BUDGET_EXCEEDED,
                    f"max_hops {action.max_hops} exceeds the policy limit "
                    f"{self.policy.max_hops}",
                )
            if action.max_paths > self.policy.max_results_per_query:
                return {}, (
                    PolicyRejectionCode.LIMIT_TOO_LARGE,
                    f"max_paths {action.max_paths} exceeds the policy limit "
                    f"{self.policy.max_results_per_query}",
                )
            if (
                self.policy.remaining_edge_budget is not None
                and action.max_paths > self.policy.remaining_edge_budget
            ):
                return {}, (
                    PolicyRejectionCode.BUDGET_EXHAUSTED,
                    f"max_paths {action.max_paths} exceeds the remaining "
                    f"edge budget {self.policy.remaining_edge_budget}",
                )
            return (
                {
                    "operation": action.action_type.value,
                    "start_entity_id": action.start_entity_id,
                    "target_entity_id": action.target_entity_id,
                    "max_hops": action.max_hops,
                    "relations": list(action.relations),
                    "target_types": list(action.target_types),
                    "max_paths": action.max_paths,
                },
                None,
            )
        if action.action_type is ExplorationActionType.RETRIEVE_LITERATURE:
            explicit_need = action.evidence_need_id
            if explicit_need is not None and explicit_need not in self.policy.open_need_ids:
                return {}, (
                    PolicyRejectionCode.NEED_NOT_FOUND,
                    f"the evidence need {explicit_need!r} is not open",
                )
            if not action.entity_ids and not explicit_need:
                return {}, (
                    PolicyRejectionCode.NEED_NOT_FOUND,
                    "a literature action must serve an existing evidence need",
                )
            unknown = [
                item for item in action.entity_ids if item not in known_entities
            ]
            if unknown:
                return {}, (
                    PolicyRejectionCode.UNKNOWN_ENTITY,
                    f"unknown entities in the literature action: {unknown}",
                )
            if action.max_results > self.policy.max_literature_results:
                return {}, (
                    PolicyRejectionCode.LIMIT_TOO_LARGE,
                    f"max_results {action.max_results} exceeds the literature "
                    f"limit {self.policy.max_literature_results}",
                )
            if self.policy.open_need_ids and not explicit_need:
                served = [
                    item for item in action.entity_ids if item in self.policy.open_need_ids
                ]
                if not served:
                    return {}, (
                        PolicyRejectionCode.NEED_NOT_FOUND,
                        "the literature action serves no open evidence need",
                    )
            return (
                {
                    "operation": action.action_type.value,
                    "query": action.query,
                    "need_ids": (
                        [explicit_need]
                        if explicit_need
                        else [
                            item
                            for item in action.entity_ids
                            if item in self.policy.open_need_ids
                        ]
                        or list(action.entity_ids)
                    ),
                    "entity_ids": list(action.entity_ids),
                    "max_results": action.max_results,
                },
                None,
            )
        # finish
        if not self.policy.has_evidence:
            return {}, (
                PolicyRejectionCode.FINISH_WITHOUT_EVIDENCE,
                "the run may not finish before any evidence was collected",
            )
        total = self.policy.kg_evidence_count + self.policy.literature_evidence_count
        # Legacy scripted policies may set only ``has_evidence``. Treat that
        # as the minimum one unit without weakening explicit source minimums.
        if self.policy.has_evidence and total == 0:
            total = 1
        if (
            total < self.policy.min_total_evidence
            or self.policy.kg_evidence_count < self.policy.min_kg_evidence
            or self.policy.literature_evidence_count
            < self.policy.min_literature_evidence
        ):
            return {}, (
                PolicyRejectionCode.FINISH_WITHOUT_EVIDENCE,
                "the run may not finish before the generation spec's evidence "
                f"minimums are met (total={total}, kg="
                f"{self.policy.kg_evidence_count}, literature="
                f"{self.policy.literature_evidence_count})",
            )
        return (
            {
                "operation": action.action_type.value,
                "stop_reason": action.stop_reason.value,
            },
            None,
        )

    def _reject(
        self,
        *,
        run_id: str,
        episode_id: str,
        turn: int,
        code: PolicyRejectionCode,
        message: str,
        action_type: str | None = None,
    ) -> PolicyRejection:
        return PolicyRejection(
            rejection_id=new_id("rejection"),
            run_id=run_id,
            episode_id=episode_id,
            turn=turn,
            code=code,
            message=message,
            action_type=action_type,
            created_at=utc_now(),
        )


def _tool_name_for(action: ExplorationAction) -> str:
    if action.action_type is ExplorationActionType.RETRIEVE_LITERATURE:
        return "literature_search"
    if action.action_type is ExplorationActionType.FINISH:
        return "none"
    return "kg_neighbors"
