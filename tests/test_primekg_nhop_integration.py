"""Real PrimeKG: a three-hop walk that is persisted and reloaded.

Step 3 of the real-generation-agent plan. The exit criterion for this step is
that at least one real three-hop path can be persisted and re-validated from
the evidence registry, against the real 1.63 GiB snapshot, read-only.

Excluded from the default run. Execute with:
    python -m pytest tests -m primekg_integration -q -o addopts=""
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from medical_qa_data_factory.domain import (
    EvidenceDirection,
    KGEvidence,
)
from medical_qa_data_factory.domain.common import utc_now
from medical_qa_data_factory.generation.contracts import EvidencePath
from medical_qa_data_factory.generation.path_persistence import PathPersister
from medical_qa_data_factory.knowledge_graph import EntityLookup
from medical_qa_data_factory.knowledge_graph.frontier import (
    FrontierBudget,
    NHopPathBuilder,
)
from medical_qa_data_factory.knowledge_graph.providers.primekg import (
    PrimeKGSQLiteProvider,
)
from medical_qa_data_factory.knowledge_graph.registry import load_provider_config
from medical_qa_data_factory.repository import LocalArtifactRepository

pytestmark = pytest.mark.primekg_integration


def _real_database() -> Path | None:
    try:
        config = load_provider_config()
    except Exception:  # noqa: BLE001 - config may be absent in CI
        return None
    settings = config["providers"].get(config.get("default_provider", ""), {})
    if settings.get("driver") != "primekg_sqlite":
        return None
    path = Path(settings["database_path"])
    if not path.is_absolute():
        path = Path(config["_project_root"]) / path
    return path if path.exists() else None


DATABASE = _real_database()


def _provider() -> PrimeKGSQLiteProvider:
    if DATABASE is None:
        pytest.skip("real PrimeKG database is not available")
    return PrimeKGSQLiteProvider(DATABASE, sha256=os.environ.get("PRIMEKG_SHA256"))


def _aspirin(provider: PrimeKGSQLiteProvider):
    resolution = provider.resolve_entity(
        EntityLookup(mention="aspirin", native_id="D001241", entity_types=["exposure"])
    )
    assert resolution.selected is not None
    return resolution.selected


def _evidence_for(provider: PrimeKGSQLiteProvider, edge) -> KGEvidence:
    return KGEvidence(
        evidence_id=f"kg.{edge.edge_id[:24]}",
        head=edge.head.ref(source=edge.provider_id),
        relation=edge.relation,
        tail=edge.tail.ref(source=edge.provider_id),
        direction=EvidenceDirection.FORWARD,
        source_database=edge.provider_id,
        source_record_id=edge.edge_id,
        confidence=1.0,
        retrieval_action={"provider_id": edge.provider_id},
        retrieved_at=utc_now(),
    )


def test_a_real_three_hop_walk_is_persisted_and_reloaded(tmp_path) -> None:
    """The Step 3 exit criterion: a real 3-hop path survives a round trip."""
    if DATABASE is None:
        pytest.skip("real PrimeKG database is not available")
    provider = _provider()
    try:
        seed = _aspirin(provider)
        builder = NHopPathBuilder(
            provider,
            budget=FrontierBudget(max_hops=3, max_frontier=4, hop_limit=50),
        )
        result = builder.build(seed, max_hops=3)
        assert result.paths, "the real graph must yield at least one 3-hop walk"
        # Paths are ordered by hash, not by length, so the longest one is
        # selected explicitly.
        path = max(result.paths, key=lambda item: len(item.steps))
        assert len(path.steps) == 3
        # Every step is continuous and the walk does not return to its start.
        for left, right in zip(path.steps, path.steps[1:]):
            assert left.to_entity_id == right.from_entity_id
        assert path.end_entity_id != path.start_entity_id
        assert path.snapshot_id
    finally:
        provider.close()


def test_a_real_three_hop_walk_survives_the_persistence_round_trip(tmp_path) -> None:
    """Evidence first, path second, then reload and re-validate."""
    if DATABASE is None:
        pytest.skip("real PrimeKG database is not available")
    provider = _provider()
    try:
        seed = _aspirin(provider)
        builder = NHopPathBuilder(
            provider,
            budget=FrontierBudget(max_hops=3, max_frontier=4, hop_limit=50),
        )
        result = builder.build(seed, max_hops=3)
        assert result.paths
        path = max(result.paths, key=lambda item: len(item.steps))
        assert len(path.steps) == 3
        # Bind every edge of the walk to a persisted KGEvidence.
        edges = {edge.edge_id: edge for edge in result.all_edges}
        evidence = [
            _evidence_for(provider, edges[edge_id])
            for edge_id in path.source_edge_ids
            if edge_id in edges
        ]
        assert len(evidence) == len(path.source_edge_ids)
        repository = LocalArtifactRepository(tmp_path / "ws")
        persister = PathPersister(
            repository=repository,
            episode_id="ep1",
            task_id="t1",
            bundle_id="b1",
        )
        persisted = persister.persist([path], evidence)
        assert persisted.rejected == []
        assert persisted.paths
        assert len(persisted.paths[0].steps) == 3
        # The path is on disk and reloads.
        reloaded = repository.load(
            "paths",
            persisted.paths[0].path_id,
            persisted.paths[0].path_version,
            EvidencePath,
        )
        assert len(reloaded.steps) == 3
        assert reloaded.source_evidence_ids == persisted.paths[0].source_evidence_ids
    finally:
        provider.close()


def test_the_real_database_is_not_modified_by_the_n_hop_builder(tmp_path) -> None:
    """The builder must not write to the PrimeKG database."""
    if DATABASE is None:
        pytest.skip("real PrimeKG database is not available")
    before = DATABASE.stat()
    provider = _provider()
    try:
        builder = NHopPathBuilder(
            provider,
            budget=FrontierBudget(max_hops=3, max_frontier=3, hop_limit=30),
        )
        builder.build(_aspirin(provider), max_hops=3)
    finally:
        provider.close()
    after = DATABASE.stat()
    assert (after.st_size, after.st_mtime) == (before.st_size, before.st_mtime)