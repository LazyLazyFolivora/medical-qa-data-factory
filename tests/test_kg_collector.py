"""GraphEdge -> KGEvidence mapping and provider-neutral collector behavior."""
from __future__ import annotations

import pytest

from medical_qa_data_factory.domain import (
    EntityRef,
    EvidenceDirection,
    LinkStatus,
)
from medical_qa_data_factory.knowledge_graph import (
    EntityLookup,
    EntityResolution,
    EntitySearchQuery,
    EntitySearchResult,
    GraphCapabilities,
    GraphEdge,
    GraphEntity,
    GraphHealth,
    GraphQueryResult,
    GraphQueryTrace,
    GraphSnapshot,
    KnowledgeGraphEvidenceCollector,
    NeighborQuery,
)
from medical_qa_data_factory.knowledge_graph.errors import (
    GraphCapabilityError,
    KnowledgeGraphError,
    GraphQueryError,
    GraphUnresolvedEntityError,
)
from medical_qa_data_factory.knowledge_graph.ports import KnowledgeGraphProvider


def _entity(name: str, native: str, kind: str = "drug") -> GraphEntity:
    return GraphEntity(
        provider_id="fake",
        native_id=native,
        canonical_id=f"fake:{kind}:{native}",
        name=name,
        entity_type=kind,
    )


def _trace() -> GraphQueryTrace:
    return GraphQueryTrace(
        query_id="kg_query_fixed",
        provider_id="fake",
        operation="neighbors",
        normalized_request={"direction": "both", "limit": 10},
        started_at="2026-01-01T00:00:00Z",
        elapsed_ms=3,
        records_returned=1,
        truncated=False,
    )


def _snapshot() -> GraphSnapshot:
    return GraphSnapshot(
        provider_id="fake",
        adapter_version="fake-v1",
        data_snapshot_id="snapshot-1",
        config_hash="config-1",
    )


def _edge() -> GraphEdge:
    return GraphEdge(
        provider_id="fake",
        edge_id="edge-1",
        head=_entity("aspirin", "1"),
        relation="treats",
        tail=_entity("headache", "2", "disease"),
        source="DrugBank",
    )


class FakeProvider:
    """A second provider: the pipeline and contracts must work without PrimeKG."""

    provider_id = "fake"

    def __init__(self) -> None:
        self.paths_supported = False

    def capabilities(self) -> GraphCapabilities:
        return GraphCapabilities(
            entity_lookup_by_name=True,
            entity_lookup_by_id=True,
            incoming_edges=True,
            outgoing_edges=True,
            path_query=self.paths_supported,
            max_hops=2 if self.paths_supported else 0,
        )

    def healthcheck(self) -> GraphHealth:
        return GraphHealth(provider_id="fake", healthy=True)

    def snapshot(self) -> GraphSnapshot:
        return _snapshot()

    def resolve_entity(self, query: EntityLookup) -> EntityResolution:
        entity = _entity(query.mention, "1")
        return EntityResolution(
            candidates=[],
            selected=entity,
            ambiguous=False,
            trace=_trace(),
        )

    def search_entities(self, query: EntitySearchQuery) -> EntitySearchResult:
        return EntitySearchResult(
            entities=[],
            trace=_trace(),
            snapshot=_snapshot(),
        )

    def neighbors(self, query: NeighborQuery) -> GraphQueryResult:
        return GraphQueryResult(edges=[_edge()], trace=_trace(), snapshot=_snapshot())

    def paths(self, query) -> GraphQueryResult:
        if not self.paths_supported:
            raise GraphCapabilityError("paths unsupported")
        return GraphQueryResult(edges=[], trace=_trace(), snapshot=_snapshot())

    def close(self) -> None:
        return None


def _seed() -> EntityRef:
    return EntityRef(
        entity_ref_id="seed-1",
        raw_mention="aspirin",
        canonical_name="aspirin",
        canonical_id="fake:drug:1",
        namespace="fake",
        entity_type="drug",
        aliases=[],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="test",
    )


def test_fake_provider_satisfies_port() -> None:
    assert isinstance(FakeProvider(), KnowledgeGraphProvider)


def test_collector_maps_edges_to_kg_evidence() -> None:
    collector = KnowledgeGraphEvidenceCollector(FakeProvider(), default_limit=10)
    result = collector.collect(_seed())
    assert len(result.evidence) == 1
    item = result.evidence[0]
    assert item.relation == "treats"
    assert item.source_database == "fake"
    assert item.source_record_id == "edge-1"
    assert item.direction is EvidenceDirection.BIDIRECTIONAL
    assert item.head.canonical_id == "fake:drug:1"
    assert item.metadata["upstream_source"] == "DrugBank"
    action = item.retrieval_action
    assert action["provider_id"] == "fake"
    assert action["query_id"] == "kg_query_fixed"
    assert action["truncated"] is False
    assert action["config_hash"] == "config-1"


def test_collector_records_usage_and_snapshot() -> None:
    result = KnowledgeGraphEvidenceCollector(FakeProvider()).collect(_seed())
    assert result.usage.kg_relation_accesses == 1
    assert result.snapshot.data_snapshot_id == "snapshot-1"
    assert result.snapshot.tool_version == "fake-v1"
    assert result.invocation.provider == "fake"


def test_unresolved_seed_raises_stable_error() -> None:
    class NoMatch(FakeProvider):
        def resolve_entity(self, query: EntityLookup) -> EntityResolution:
            return EntityResolution(candidates=[], ambiguous=False, trace=_trace())

    with pytest.raises(GraphUnresolvedEntityError) as excinfo:
        KnowledgeGraphEvidenceCollector(NoMatch()).collect(_seed())
    assert excinfo.value.code == "kg_unresolved_entity"
    assert excinfo.value.retryable is False


def test_ambiguous_seed_never_silently_takes_the_first_candidate() -> None:
    class Ambiguous(FakeProvider):
        def resolve_entity(self, query: EntityLookup) -> EntityResolution:
            from medical_qa_data_factory.knowledge_graph import EntityCandidate

            candidates = [
                EntityCandidate(entity=_entity("aspirin", "1"), match_kind="id", score=1.0),
                EntityCandidate(
                    entity=_entity("aspirin", "1", "gene"), match_kind="id", score=1.0
                ),
            ]
            return EntityResolution(
                candidates=candidates, selected=None, ambiguous=True, trace=_trace()
            )

    with pytest.raises(GraphQueryError) as excinfo:
        KnowledgeGraphEvidenceCollector(Ambiguous()).collect(_seed())
    assert "ambiguous" in str(excinfo.value)
