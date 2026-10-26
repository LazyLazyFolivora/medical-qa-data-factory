"""The ReAct acceptance runner must execute the ReAct graph it reports."""
from __future__ import annotations

from typing import Any

from medical_qa_data_factory.domain.common import utc_now
from medical_qa_data_factory.evaluation.manifest import EvalSeed
from medical_qa_data_factory.evaluation import react_run
from medical_qa_data_factory.knowledge_graph.contracts import (
    EntityResolution,
    GraphEdge,
    GraphEntity,
    GraphQueryResult,
    GraphQueryTrace,
    GraphSnapshot,
)


def _entity(native_id: str, kind: str) -> GraphEntity:
    return GraphEntity(
        provider_id="fake",
        native_id=native_id,
        canonical_id=f"fake:{kind}:{native_id}",
        name=native_id,
        entity_type=kind,
    )


def _trace(operation: str) -> GraphQueryTrace:
    return GraphQueryTrace(
        query_id=f"trace-{operation}",
        provider_id="fake",
        operation=operation,
        started_at=utc_now(),
    )


class _Provider:
    provider_id = "fake"

    def __init__(self) -> None:
        self.seed = _entity("seed", "drug")
        self.middle = _entity("middle", "gene")
        self.end = _entity("end", "disease")
        self.calls: list[str] = []
        self.snapshot_value = GraphSnapshot(
            provider_id="fake",
            adapter_version="fake-v1",
            data_snapshot_id="snapshot-1",
            config_hash="config-1",
        )

    def resolve_entity(self, query: Any) -> EntityResolution:
        return EntityResolution(selected=self.seed, trace=_trace("resolve"))

    def neighbors(self, query: Any) -> GraphQueryResult:
        subject = query.entity.canonical_id
        self.calls.append(subject)
        adjacency = {
            self.seed.canonical_id: [
                GraphEdge(
                    provider_id="fake",
                    edge_id="edge-1",
                    head=self.seed,
                    relation="targets",
                    tail=self.middle,
                )
            ],
            self.middle.canonical_id: [
                GraphEdge(
                    provider_id="fake",
                    edge_id="edge-2",
                    head=self.middle,
                    relation="associated_with",
                    tail=self.end,
                )
            ],
        }
        return GraphQueryResult(
            edges=adjacency.get(subject, []),
            trace=_trace("neighbors"),
            snapshot=self.snapshot_value,
        )


def test_react_acceptance_executes_the_react_graph(monkeypatch) -> None:
    provider = _Provider()
    calls = 0
    original = react_run.build_react_graph

    def build(*args: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(react_run, "build_react_graph", build)
    outcome = react_run._run_seed(
        provider,
        EvalSeed(
            seed_id="seed",
            provider_id="fake",
            native_id="seed",
            entity_type="drug",
            canonical_name="seed",
            max_hops=2,
        ),
        provider_id="fake",
    )

    assert calls == 1
    assert outcome.status == "completed"
    assert outcome.stop_reason == "evidence_sufficient"
    assert outcome.path_hop_counts == [2]
    assert outcome.tool_calls == 2
    assert provider.calls == [provider.seed.canonical_id, provider.middle.canonical_id]
