"""Role-typed subagent dispatch with one registered handler per role."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .objective_question_contracts import SubagentResult, SubagentTask
from .objective_question_runtime import ArtifactEnvelope
from .subagent_contracts import SubagentRole


@dataclass(frozen=True)
class DispatchOutcome:
    result: SubagentResult
    artifacts: list[ArtifactEnvelope]


SubagentHandler = Callable[[SubagentTask, dict[str, Any]], DispatchOutcome]


class SubagentDispatcher:
    def __init__(self) -> None:
        self._handlers: dict[SubagentRole, SubagentHandler] = {}

    def register(
        self,
        role: SubagentRole,
        handler: SubagentHandler,
    ) -> None:
        if role in self._handlers:
            raise ValueError(f"a handler is already registered for {role.value}")
        self._handlers[role] = handler

    def dispatch(
        self,
        task: SubagentTask,
        *,
        payload: dict[str, Any],
    ) -> DispatchOutcome:
        try:
            role = SubagentRole(task.subagent_role)
        except ValueError as exc:
            raise ValueError(
                f"unknown subagent role {task.subagent_role!r}"
            ) from exc
        handler = self._handlers.get(role)
        if handler is None:
            raise KeyError(role.value)
        outcome = handler(task, payload)
        if outcome.result.subtask_id != task.subtask_id:
            raise ValueError("subagent result references another task")
        artifact_ids = {artifact.artifact_id for artifact in outcome.artifacts}
        if not set(outcome.result.output_artifact_ids) <= artifact_ids:
            raise ValueError("subagent result references missing artifacts")
        return outcome
