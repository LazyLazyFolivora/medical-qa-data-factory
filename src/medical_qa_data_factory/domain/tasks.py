"""Generation task contract."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field

from .common import (
    SCHEMA_VERSION,
    ActorType,
    ChangeRecord,
    ContractModel,
    EntityRef,
    GenerationMode,
    InvocationConfig,
    ToolSnapshot,
    UsageRecord,
    UtcDateTime,
    new_id,
    utc_now,
)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    NEEDS_INPUT = "needs_input"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GenerationTask(ContractModel):
    schema_version: str = SCHEMA_VERSION
    task_id: str = Field(min_length=1)
    record_version: str = Field(min_length=1)
    generation_mode: GenerationMode
    status: TaskStatus
    seed_entities: list[EntityRef] = Field(min_length=1)
    target_question_count: int | None = Field(default=None, ge=1)
    taxonomy_targets: dict[str, Any] = Field(default_factory=dict)
    source_policy: dict[str, Any] = Field(default_factory=dict)
    generation_limits: dict[str, Any] = Field(default_factory=dict)
    tool_snapshots: list[ToolSnapshot] = Field(default_factory=list)
    invocation_configs: list[InvocationConfig] = Field(default_factory=list)
    current_stage: str = "seed_selected"
    artifact_ids: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    generation_usage: list[UsageRecord] = Field(default_factory=list)
    created_at: UtcDateTime
    updated_at: UtcDateTime
    changes: list[ChangeRecord] = Field(default_factory=list)

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        generation_mode: GenerationMode,
        seed_entities: list[EntityRef],
        target_question_count: int | None = None,
    ) -> "GenerationTask":
        now = utc_now()
        version = new_id("version")
        change = ChangeRecord(
            change_id=new_id("change"),
            actor_type=ActorType.SYSTEM,
            operation="create",
            summary="create generation task",
            created_at=now,
            parent_version=None,
            new_version=version,
        )
        return cls(
            task_id=task_id,
            record_version=version,
            generation_mode=generation_mode,
            status=TaskStatus.PENDING,
            seed_entities=seed_entities,
            target_question_count=target_question_count,
            created_at=now,
            updated_at=now,
            changes=[change],
        )

