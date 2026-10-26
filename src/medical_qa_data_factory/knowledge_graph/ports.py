"""Provider port protocol. No SQL, file paths, or HTTP details leak here."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .contracts import (
    EntityLookup,
    EntityResolution,
    EntitySearchQuery,
    EntitySearchResult,
    GraphCapabilities,
    GraphHealth,
    GraphQueryResult,
    GraphSnapshot,
    NeighborQuery,
    PathQuery,
)


@runtime_checkable
class KnowledgeGraphProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    def capabilities(self) -> GraphCapabilities: ...

    def healthcheck(self) -> GraphHealth: ...

    def snapshot(self) -> GraphSnapshot: ...

    def resolve_entity(self, query: EntityLookup) -> EntityResolution: ...

    def search_entities(self, query: EntitySearchQuery) -> EntitySearchResult: ...

    def neighbors(self, query: NeighborQuery) -> GraphQueryResult: ...

    def paths(self, query: PathQuery) -> GraphQueryResult: ...

    def close(self) -> None: ...
