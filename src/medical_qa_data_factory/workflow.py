"""Pure generation-task state transitions."""
from __future__ import annotations

from medical_qa_data_factory.domain import (
    ActorType,
    ChangeRecord,
    GenerationTask,
    TaskStatus,
)
from medical_qa_data_factory.domain.common import new_id, utc_now


class InvalidTaskTransition(ValueError):
    """Raised when a task status transition is not allowed."""


ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.NEEDS_INPUT,
        TaskStatus.COMPLETED,
        TaskStatus.PARTIAL,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.NEEDS_INPUT: {
        TaskStatus.RUNNING,
        TaskStatus.PARTIAL,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.PARTIAL: {
        TaskStatus.RUNNING,
        TaskStatus.COMPLETED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


def transition_task(
    task: GenerationTask,
    new_status: TaskStatus,
    *,
    actor_type: ActorType = ActorType.SYSTEM,
    summary: str,
    current_stage: str | None = None,
) -> GenerationTask:
    """Return a new task version with an audited status transition."""
    if new_status not in ALLOWED_TRANSITIONS[task.status]:
        raise InvalidTaskTransition(f"invalid transition: {task.status.value} -> {new_status.value}")

    return revise_task(
        task,
        actor_type=actor_type,
        operation="status_transition",
        summary=summary,
        status=new_status,
        current_stage=current_stage or task.current_stage,
    )


def revise_task(
    task: GenerationTask,
    *,
    actor_type: ActorType = ActorType.SYSTEM,
    operation: str,
    summary: str,
    **updates: object,
) -> GenerationTask:
    """Create a validated, audited task version without changing identity."""
    protected = {"task_id", "record_version", "created_at", "changes"}
    invalid = protected.intersection(updates)
    if invalid:
        raise ValueError(f"protected task fields cannot be updated: {sorted(invalid)}")

    now = utc_now()
    new_version = new_id("version")
    change = ChangeRecord(
        change_id=new_id("change"),
        actor_type=actor_type,
        operation=operation,
        summary=summary,
        created_at=now,
        parent_version=task.record_version,
        new_version=new_version,
    )
    payload = task.model_dump()
    payload.update(updates)
    payload.update(
        record_version=new_version,
        updated_at=now,
        changes=[*task.changes, change],
    )
    return GenerationTask.model_validate(payload)


