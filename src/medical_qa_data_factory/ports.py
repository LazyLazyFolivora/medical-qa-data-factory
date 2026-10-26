"""Dependency-free interfaces for external evidence tools and draft agents."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .domain import (
    Claim,
    EntityRef,
    EvidenceBundle,
    InvocationConfig,
    KGEvidence,
    LiteratureEvidence,
    QuestionSpec,
    ToolSnapshot,
    UsageRecord,
)


@dataclass(frozen=True)
class KGCollectionResult:
    evidence: list[KGEvidence]
    invocation: InvocationConfig
    usage: UsageRecord
    snapshot: ToolSnapshot


@dataclass(frozen=True)
class LiteratureCollectionResult:
    evidence: list[LiteratureEvidence]
    invocation: InvocationConfig
    usage: UsageRecord
    snapshot: ToolSnapshot


class KGTool(Protocol):
    def collect(self, seed_entity: EntityRef) -> KGCollectionResult: ...


class LiteratureTool(Protocol):
    def collect(self, seed_entity: EntityRef) -> LiteratureCollectionResult: ...


class DraftAgent(Protocol):
    def propose_claims(
        self,
        seed_entity: EntityRef,
        kg_evidence: list[KGEvidence],
        literature_evidence: list[LiteratureEvidence],
    ) -> list[Claim]: ...

    def draft_questions(
        self, bundle: EvidenceBundle, limit: int | None = None
    ) -> list[QuestionSpec]: ...

