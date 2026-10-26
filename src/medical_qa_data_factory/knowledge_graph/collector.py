"""Application layer: query planning plus GraphEdge -> KGEvidence mapping."""
from __future__ import annotations

from typing import Any

from ..domain import (
    EndpointKind,
    EntityRef,
    EvidenceDirection,
    InvocationConfig,
    KGEvidence,
    LinkStatus,
    MeasurementSource,
    ToolSnapshot,
    UsageRecord,
    UsageScope,
)
from ..domain.common import new_id, utc_now
from ..ports import KGCollectionResult
from .contracts import EntityLookup, GraphEdge, GraphQueryResult, NeighborQuery
from .errors import GraphQueryError, GraphUnresolvedEntityError
from .ports import KnowledgeGraphProvider


DIRECTION_MAP = {
    "outgoing": EvidenceDirection.FORWARD,
    "incoming": EvidenceDirection.BACKWARD,
    "both": EvidenceDirection.BIDIRECTIONAL,
}


class KnowledgeGraphEvidenceCollector:
    """Keeps ``KGTool.collect()`` compatible while delegating to any provider."""

    def __init__(
        self,
        provider: KnowledgeGraphProvider,
        *,
        tool_name: str | None = None,
        endpoint_kind: EndpointKind = EndpointKind.PYTHON,
        default_limit: int = 100,
    ) -> None:
        self.provider = provider
        self.tool_name = tool_name or f"kg_{provider.provider_id}"
        self.endpoint_kind = endpoint_kind
        self.default_limit = default_limit

    def _resolution_for(self, seed: EntityRef) -> tuple[Any, bool]:
        lookup = EntityLookup(
            mention=seed.canonical_name or seed.raw_mention,
            entity_types=[seed.entity_type] if seed.entity_type else [],
        )
        resolution = self.provider.resolve_entity(lookup)
        if resolution.selected is not None:
            return resolution.selected, resolution.ambiguous
        if resolution.candidates:
            # Multiple candidates must never be silently collapsed to the first
            # one; disambiguation belongs to the upstream data source.
            raise GraphQueryError(
                "seed entity is ambiguous in the graph provider: "
                f"{sorted(item.entity.canonical_id for item in resolution.candidates)}"
            )
        return None, False

    def collect(self, seed_entity: EntityRef) -> KGCollectionResult:
        started_at = utc_now()
        entity, ambiguous = self._resolution_for(seed_entity)
        if entity is None:
            raise GraphUnresolvedEntityError(
                f"seed entity not found: {seed_entity.raw_mention}"
            )
        result: GraphQueryResult = self.provider.neighbors(
            NeighborQuery(entity=entity, direction="both", limit=self.default_limit)
        )
        return self._to_collection_result(
            seed_entity=seed_entity,
            result=result,
            started_at=started_at,
            ambiguous=ambiguous,
        )

    def _to_collection_result(
        self,
        *,
        seed_entity: EntityRef,
        result: GraphQueryResult,
        started_at: Any,
        ambiguous: bool,
    ) -> KGCollectionResult:
        snapshot = result.snapshot
        tool_snapshot = ToolSnapshot(
            tool_name=self.tool_name,
            tool_version=snapshot.adapter_version,
            data_snapshot_id=snapshot.data_snapshot_id,
            config_hash=snapshot.config_hash,
            endpoint_kind=self.endpoint_kind,
            retrieved_at=started_at,
        )
        invocation = InvocationConfig(
            invocation_id=new_id("invocation"),
            provider=snapshot.provider_id,
            model_id=None,
            request_parameters={
                "operation": result.trace.operation,
                **result.trace.normalized_request,
            },
            config_hash=snapshot.config_hash,
        )
        direction = DIRECTION_MAP.get(
            str(result.trace.normalized_request.get("direction", "both")),
            EvidenceDirection.BIDIRECTIONAL,
        )
        retrieval_action = {
            "provider_id": snapshot.provider_id,
            "query_id": result.trace.query_id,
            "operation": result.trace.operation,
            "direction": result.trace.normalized_request.get("direction", "both"),
            "relations": result.trace.normalized_request.get("relations", []),
            "target_types": result.trace.normalized_request.get("target_types", []),
            "limit": result.trace.normalized_request.get("limit"),
            "truncated": result.truncated,
            "elapsed_ms": result.trace.elapsed_ms,
            "adapter_version": snapshot.adapter_version,
            "config_hash": snapshot.config_hash,
            "entity_ambiguous": ambiguous,
        }
        evidence = [
            self._to_kg_evidence(edge, direction=direction, retrieval_action=retrieval_action)
            for edge in result.edges
        ]
        usage = UsageRecord(
            usage_id=new_id("usage"),
            usage_scope=UsageScope.GENERATION,
            invocation_id=invocation.invocation_id,
            tool_name=self.tool_name,
            tool_calls=1,
            kg_relation_accesses=len(result.edges),
            elapsed_ms=result.trace.elapsed_ms,
            measurement_source=MeasurementSource.TOOL,
            meter_version="kg-collector-v1",
            recorded_at=utc_now(),
        )
        return KGCollectionResult(
            evidence=evidence,
            invocation=invocation,
            usage=usage,
            snapshot=tool_snapshot,
        )

    @staticmethod
    def _to_kg_evidence(
        edge: GraphEdge, *, direction: EvidenceDirection, retrieval_action: dict[str, Any]
    ) -> KGEvidence:
        head = edge.head.ref(source=edge.provider_id)
        tail = edge.tail.ref(source=edge.provider_id)
        metadata = dict(edge.metadata)
        if edge.source:
            # PrimeKG's `source` is the upstream citation, not the graph provider.
            metadata["upstream_source"] = edge.source
        return KGEvidence(
            evidence_id=new_id("kg"),
            head=head,
            relation=edge.relation,
            tail=tail,
            direction=direction,
            source_database=edge.provider_id,
            source_record_id=edge.edge_id,
            confidence=1.0,
            retrieval_action=dict(retrieval_action),
            retrieved_at=utc_now(),
            metadata=metadata,
        )
