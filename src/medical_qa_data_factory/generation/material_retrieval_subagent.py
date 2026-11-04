"""Provider-scoped material retrieval with no factual interpretation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence_provider import (
    EvidenceProviderResult,
)
from .evidence_search_agent import EvidenceSearchAgent
from .objective_question_contracts import (
    EvidenceAnchor,
    EvidenceMaterial,
    SubagentResult,
    SubagentTask,
)
from .objective_question_persistence import ObjectiveQuestionPersistence
from .subagent_contracts import (
    SubagentRole,
    default_objective_role_registry,
    validate_objective_subagent_task,
)


@dataclass(frozen=True)
class MaterialRetrievalOutcome:
    result: SubagentResult
    provider_result: EvidenceProviderResult
    materials: list[EvidenceMaterial]
    anchors: list[EvidenceAnchor]


class MaterialRetrievalSubagent:
    role = SubagentRole.MATERIAL_RETRIEVAL

    def __init__(
        self,
        persistence: ObjectiveQuestionPersistence,
        *,
        model: Any | None = None,
        max_search_rounds: int = 4,
        max_queries_per_round: int = 2,
        preview_chars: int = 2000,
    ) -> None:
        self.persistence = persistence
        self.model = model
        self.max_search_rounds = max_search_rounds
        self.max_queries_per_round = max_queries_per_round
        self.preview_chars = preview_chars
        self.capability = default_objective_role_registry()[self.role]

    def run(
        self,
        task: SubagentTask,
        *,
        provider: Any,
        query: dict[str, Any],
    ) -> MaterialRetrievalOutcome:
        validate_objective_subagent_task(task, self.capability)
        outcome = EvidenceSearchAgent(
            model=self.model,
            persistence=self.persistence,
            max_rounds=int(
                task.goal.get(
                    "max_search_rounds",
                    self.max_search_rounds,
                )
            ),
            max_queries_per_round=int(
                task.goal.get(
                    "max_queries_per_round",
                    self.max_queries_per_round,
                )
            ),
            preview_chars=int(
                task.goal.get(
                    "preview_chars",
                    self.preview_chars,
                )
            ),
        ).run(task, provider=provider, initial_query=query)
        return MaterialRetrievalOutcome(
            result=outcome.result,
            provider_result=outcome.provider_result,
            materials=outcome.materials,
            anchors=outcome.anchors,
        )
