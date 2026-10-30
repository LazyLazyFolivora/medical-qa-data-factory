"""Typed contracts for discoverable, policy-gated agent skills."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator

from ...domain.common import ContractModel, UtcDateTime, utc_now
from ..contracts import ExplorationAction, ExplorationActionType


class SkillCostEstimate(ContractModel):
    """Conservative cost metadata exposed before a skill is selected."""

    tool_calls: int = Field(default=1, ge=0)
    max_records: int | None = Field(default=None, ge=1)
    relative_cost: Literal["low", "medium", "high"] = "low"


class SkillDefinition(ContractModel):
    """Stable metadata used to decide whether a skill is currently useful."""

    skill_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    action_type: ExplorationActionType
    authority: Literal["evidence_acquisition"] = "evidence_acquisition"
    required_tool_names: list[str] = Field(default_factory=list)
    required_artifact_types: list[str] = Field(default_factory=list)
    produced_artifact_types: list[str] = Field(default_factory=list)
    addressed_gap_types: list[str] = Field(default_factory=list)
    seed_entity_types: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    cost: SkillCostEstimate = Field(default_factory=SkillCostEstimate)

    @model_validator(mode="after")
    def evidence_skills_cannot_finish_or_duplicate_metadata(
        self,
    ) -> "SkillDefinition":
        if self.action_type is ExplorationActionType.FINISH:
            raise ValueError("an evidence-acquisition skill cannot finish a run")
        for field_name in (
            "required_tool_names",
            "required_artifact_types",
            "produced_artifact_types",
            "addressed_gap_types",
            "seed_entity_types",
        ):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must not contain duplicates")
        return self


class SkillRuntimeContext(ContractModel):
    """Server-owned state used for deterministic skill retrieval."""

    seed_entity_type: str | None = None
    gap_types: list[str] = Field(default_factory=list)
    artifact_types: list[str] = Field(default_factory=list)
    available_tool_names: list[str] = Field(default_factory=list)
    allowed_action_types: list[str] = Field(default_factory=list)
    remaining_tool_calls: int = Field(default=0, ge=0)
    remaining_edge_budget: int | None = Field(default=None, ge=0)


class SkillAvailability(ContractModel):
    """Planner-facing summary of one skill that is usable now."""

    skill_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    action_type: ExplorationActionType
    input_schema: dict[str, Any] = Field(default_factory=dict)
    cost: SkillCostEstimate
    addressed_gap_types: list[str] = Field(default_factory=list)


class SkillSelection(ContractModel):
    """The small model-facing decision surface for invoking a skill."""

    skill_id: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class SkillInvocation(ContractModel):
    """Server-prepared invocation whose action still passes the policy gate."""

    invocation_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    turn: int = Field(ge=1)
    state_digest: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    skill_version: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    action: ExplorationAction
    produced_artifact_types: list[str] = Field(default_factory=list)
    created_at: UtcDateTime = Field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        *,
        episode_id: str,
        turn: int,
        state_digest: str,
        definition: SkillDefinition,
        arguments: dict[str, Any],
        action: ExplorationAction,
    ) -> "SkillInvocation":
        identity = json.dumps(
            {
                "episode_id": episode_id,
                "turn": turn,
                "state_digest": state_digest,
                "skill_id": definition.skill_id,
                "skill_version": definition.version,
                "arguments": arguments,
                "action": action.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        return cls(
            invocation_id=f"skill-invocation-{digest}",
            episode_id=episode_id,
            turn=turn,
            state_digest=state_digest,
            skill_id=definition.skill_id,
            skill_version=definition.version,
            arguments=arguments,
            action=action,
            produced_artifact_types=list(
                definition.produced_artifact_types
            ),
        )


class SkillOutcome(ContractModel):
    """Auditable effect of a skill after gate and provider execution."""

    invocation_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    status: Literal["success", "warning", "error", "rejected"]
    summary: str = Field(min_length=1)
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    path_artifact_ids: list[str] = Field(default_factory=list)
    produced_artifact_types: list[str] = Field(default_factory=list)
    evidence_gain: int = Field(default=0, ge=0)
    error_code: str | None = None
    next_skill_hints: list[str] = Field(default_factory=list)
    created_at: UtcDateTime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def success_has_traceable_positive_gain(self) -> "SkillOutcome":
        expected_artifacts = list(
            dict.fromkeys(
                [*self.evidence_artifact_ids, *self.path_artifact_ids]
            )
        )
        if self.artifact_ids != expected_artifacts:
            raise ValueError(
                "artifact_ids must equal evidence and path artifact IDs"
            )
        if self.status == "success" and (
            self.evidence_gain <= 0 or not self.artifact_ids
        ):
            raise ValueError(
                "a successful skill outcome requires positive evidence gain"
            )
        if self.evidence_gain != len(self.artifact_ids):
            raise ValueError("evidence_gain must equal auditable artifact count")
        return self
