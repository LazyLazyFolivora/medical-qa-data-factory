"""Provider-neutral graph models and query contracts."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from ..domain.common import ContractModel, EntityRef, LinkStatus, UtcDateTime


ORDERED_PATH_SCHEMA_VERSION = "ordered-path-v1"

#: Hop policy shared with the generation layer. The MVP default is 3 and the
#: hard system ceiling is 4. These are policy values, not schema literals.
DEFAULT_MAX_HOPS = 3
MAX_HOPS_CEILING = 4


class GraphEntity(ContractModel):
    provider_id: str = Field(min_length=1)
    native_id: str | None = None
    canonical_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    entity_type: str | None = None
    aliases: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def ref(self, *, raw_mention: str | None = None, source: str | None = None) -> "EntityRef":
        return EntityRef(
            entity_ref_id=f"entity-{self.canonical_id}",
            raw_mention=raw_mention or self.name,
            canonical_name=self.name,
            canonical_id=self.canonical_id,
            namespace=self.metadata.get("namespace") or self.provider_id,
            entity_type=self.entity_type,
            aliases=list(self.aliases),
            link_status=LinkStatus.EXACT,
            link_score=1.0,
            source=source or self.provider_id,
        )


class GraphEdge(ContractModel):
    provider_id: str = Field(min_length=1)
    edge_id: str = Field(min_length=1)
    head: GraphEntity
    relation: str = Field(min_length=1)
    tail: GraphEntity
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EntityLookup(ContractModel):
    mention: str = Field(min_length=1)
    entity_types: list[str] = Field(default_factory=list)
    namespace: str | None = None
    native_id: str | None = None
    limit: int = Field(default=10, ge=1, le=100)


class EntityCandidate(ContractModel):
    entity: GraphEntity
    match_kind: Literal["id", "exact", "alias", "normalized", "fuzzy"]
    score: float = Field(ge=0, le=1)


class GraphQueryTrace(ContractModel):
    query_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    normalized_request: dict[str, Any] = Field(default_factory=dict)
    started_at: UtcDateTime
    elapsed_ms: int = 0
    rows_examined: int | None = None
    records_returned: int = 0
    truncated: bool = False


class GraphSnapshot(ContractModel):
    provider_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    data_snapshot_id: str = Field(min_length=1)
    sha256: str | None = None
    size_bytes: int | None = None
    modified_at: UtcDateTime | None = None
    config_hash: str = Field(min_length=1)


class EntityResolution(ContractModel):
    candidates: list[EntityCandidate] = Field(default_factory=list)
    selected: GraphEntity | None = None
    ambiguous: bool = False
    trace: GraphQueryTrace


class EntitySearchQuery(ContractModel):
    """Browse or search the entity universe exposed by a graph provider."""

    query: str = ""
    entity_types: list[str] = Field(default_factory=list)
    limit: int = Field(default=10, ge=1, le=100)


class EntitySearchResult(ContractModel):
    entities: list[GraphEntity] = Field(default_factory=list)
    trace: GraphQueryTrace
    snapshot: GraphSnapshot
    truncated: bool = False


class GraphQueryResult(ContractModel):
    edges: list[GraphEdge] = Field(default_factory=list)
    trace: GraphQueryTrace
    snapshot: GraphSnapshot
    next_cursor: str | None = None
    truncated: bool = False


class NeighborQuery(ContractModel):
    entity: GraphEntity
    direction: Literal["outgoing", "incoming", "both"] = "both"
    relations: list[str] = Field(default_factory=list)
    target_types: list[str] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = None


class PathQuery(ContractModel):
    start: GraphEntity
    end: GraphEntity | None = None
    max_hops: int = Field(default=DEFAULT_MAX_HOPS, ge=1, le=MAX_HOPS_CEILING)
    relations: list[str] = Field(default_factory=list)
    target_types: list[str] = Field(default_factory=list)
    max_paths: int = Field(default=100, ge=1, le=500)


class GraphCapabilities(ContractModel):
    entity_lookup_by_id: bool = False
    entity_lookup_by_name: bool = False
    alias_lookup: bool = False
    fuzzy_lookup: bool = False
    #: The provider can browse default seed candidates and search its full
    #: entity universe without requiring an existing generation task.
    entity_search: bool = False
    incoming_edges: bool = False
    outgoing_edges: bool = False
    #: Bounded, filtered expansion to a second hop, assembled project-side.
    #: Distinct from ``path_query``, which is a provider-returned full path.
    bounded_expansion: bool = False
    path_query: bool = False
    max_hops: int = 0


class OrderedPathStep(ContractModel):
    """One direction-aware hop, expressed against a provider edge id.

    This is the graph-layer view. The generation layer binds the same walk to
    a ``KGEvidence`` id and re-verifies it against the evidence registry.
    """

    position: int = Field(ge=0)
    edge_id: str = Field(min_length=1)
    traversal_direction: Literal["forward", "reverse"]
    from_entity_id: str = Field(min_length=1)
    relation: str = Field(min_length=1)
    to_entity_id: str = Field(min_length=1)
    query_trace_id: str = Field(min_length=1)


class OrderedPath(ContractModel):
    """An ordered two-hop walk assembled from bounded neighbor queries.

    Two edges are not a path. Every structural mistake that would let a bag of
    edges pass as a path is rejected here, before anything downstream can
    quote it.
    """

    schema_version: str = ORDERED_PATH_SCHEMA_VERSION
    path_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    start_entity_id: str = Field(min_length=1)
    end_entity_id: str = Field(min_length=1)
    steps: list[OrderedPathStep] = Field(min_length=2, max_length=MAX_HOPS_CEILING)
    intermediate_entity_ids: list[str] = Field(min_length=1)
    source_edge_ids: list[str] = Field(min_length=2)
    path_hash: str = Field(min_length=1)
    query_trace_ids: list[str] = Field(min_length=2)

    @model_validator(mode="after")
    def check_structure(self) -> "OrderedPath":
        if [step.position for step in self.steps] != list(range(len(self.steps))):
            raise ValueError("step positions must be contiguous from zero")
        if self.steps[0].from_entity_id != self.start_entity_id:
            raise ValueError("the first step must start at start_entity_id")
        if self.steps[-1].to_entity_id != self.end_entity_id:
            raise ValueError("the last step must end at end_entity_id")
        for left, right in zip(self.steps, self.steps[1:]):
            if left.to_entity_id != right.from_entity_id:
                raise ValueError(
                    "path is broken: step "
                    f"{left.position} ends at {left.to_entity_id} but step "
                    f"{right.position} starts at {right.from_entity_id}"
                )
        if self.intermediate_entity_ids != [
            step.to_entity_id for step in self.steps[:-1]
        ]:
            raise ValueError(
                "intermediate_entity_ids must be derived from the step joints"
            )
        if self.source_edge_ids != [step.edge_id for step in self.steps]:
            raise ValueError(
                "source_edge_ids must equal the position-ordered step edge ids"
            )
        if len(set(self.source_edge_ids)) != len(self.source_edge_ids):
            # The same edge walked twice is a loop, not two hops.
            raise ValueError("a path must not reuse the same edge id")
        if self.end_entity_id == self.start_entity_id:
            raise ValueError("a path may not end where it started")
        if sorted(self.query_trace_ids) != sorted(
            step.query_trace_id for step in self.steps
        ):
            raise ValueError("query_trace_ids must match the step query traces")
        return self


class GraphHealth(ContractModel):
    provider_id: str = Field(min_length=1)
    healthy: bool
    database_path: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    snapshot: GraphSnapshot | None = None
    error: dict[str, Any] | None = None

