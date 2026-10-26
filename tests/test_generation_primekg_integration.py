"""Real-snapshot integration: identity isolation and a full generation run.

Excluded from the default run. Execute with:
    python -m pytest tests -m primekg_integration -q -o addopts=""
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from medical_qa_data_factory.generation.contracts import (
    EntityGenerationRequest,
    UpstreamEntitySeed,
)
from medical_qa_data_factory.generation.repository import GenerationRepository
from medical_qa_data_factory.generation.service import EntityGenerationService
from medical_qa_data_factory.knowledge_graph import EntityLookup, NeighborQuery
from medical_qa_data_factory.knowledge_graph.path_builder import BoundedPathBuilder
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


def _request(task_id: str, workspace: Path, count: int = 3) -> EntityGenerationRequest:
    return EntityGenerationRequest(
        task_id=task_id,
        workspace=str(workspace),
        seed=UpstreamEntitySeed(
            provider_id="primekg",
            native_id="D001241",
            entity_type="exposure",
            canonical_name="Aspirin",
        ),
        question_count=count,
    )


def test_disease_8315_is_prostate_cancer_not_brap() -> None:
    """The known cross-type contamination case must stay fixed."""
    provider = _provider()
    try:
        resolution = provider.resolve_entity(
            EntityLookup(mention="8315", native_id="8315", entity_types=["disease"])
        )
        assert {item.entity.canonical_id for item in resolution.candidates} == {
            "primekg:disease:8315"
        }
        entity = resolution.selected
        assert entity is not None
        assert entity.name == "prostate cancer"
        result = provider.neighbors(NeighborQuery(entity=entity, limit=50))
        names = {edge.head.name for edge in result.edges} | {
            edge.tail.name for edge in result.edges
        }
        assert "BRAP" not in names
    finally:
        provider.close()


def test_provider_construction_does_not_rescan_the_snapshot() -> None:
    """A 1.63 GiB snapshot must never be hashed inside a normal query."""
    import medical_qa_data_factory.knowledge_graph.providers.primekg as module

    calls: list[Path] = []
    original = module._sha256_file

    def _tracking(path: Path, **kwargs):
        calls.append(Path(path))
        return original(path, **kwargs)

    module._sha256_file = _tracking
    try:
        provider = _provider()
        provider.close()
    finally:
        module._sha256_file = original
    assert calls == []


def test_real_database_is_not_modified_by_generation(tmp_path) -> None:
    if DATABASE is None:
        pytest.skip("real PrimeKG database is not available")
    before = DATABASE.stat()
    EntityGenerationService(workspace=tmp_path / "ws", provider_id="primekg").run(
        _request("smoke-readonly", tmp_path / "ws")
    )
    after = DATABASE.stat()
    assert (after.st_size, after.st_mtime) == (before.st_size, before.st_mtime)


def test_smoke_run_produces_traceable_reviewable_questions(tmp_path) -> None:
    """Engineering acceptance: at least one credible, recomputable candidate."""
    if DATABASE is None:
        pytest.skip("real PrimeKG database is not available")
    result = EntityGenerationService(
        workspace=tmp_path / "ws", provider_id="primekg"
    ).run(_request("smoke-traceable", tmp_path / "ws"))
    assert result.question_ids, "the smoke run must reach the human review queue"
    repository = LocalArtifactRepository(tmp_path / "ws")
    bundle = repository.load_latest_bundle(result.bundle_ids[0])
    evidence_ids = {item.evidence_id for item in bundle.kg_evidence}
    claim_ids = {item.claim_id for item in bundle.claims}
    for question_id in result.question_ids:
        spec = repository.load_latest_question(question_id)
        assert spec.evidence_requirements.required_kg_evidence_ids
        assert set(spec.evidence_requirements.required_kg_evidence_ids) <= evidence_ids
        assert set(spec.evidence_requirements.required_claim_ids) <= claim_ids
        edge = next(
            item
            for item in bundle.kg_evidence
            if item.evidence_id in spec.evidence_requirements.required_kg_evidence_ids
        )
        answer_ids = {value.get("canonical_id") for value in spec.gold_answer.values}
        assert answer_ids and answer_ids <= {
            edge.head.canonical_id,
            edge.tail.canonical_id,
        }
        assert bundle.seed_entities[0].canonical_id not in answer_ids
        assert spec.gold_answer.short_answer.lower() not in spec.canonical_question.lower()
        failed = [
            check.check_type
            for check in spec.validation_checks
            if check.status.value == "failed"
        ]
        assert not failed, failed


def test_smoke_run_persists_episode_and_stop_reason(tmp_path) -> None:
    if DATABASE is None:
        pytest.skip("real PrimeKG database is not available")
    result = EntityGenerationService(
        workspace=tmp_path / "ws", provider_id="primekg"
    ).run(_request("smoke-episode", tmp_path / "ws"))
    episode = GenerationRepository(tmp_path / "ws").load_latest_episode(result.episode_id)
    assert episode.stop_reason is not None
    assert result.stop_reason


def test_real_primekg_two_hop_paths_are_ordered_and_continuous(tmp_path) -> None:
    """Step 2 acceptance: a real, replayable two-hop path from a verified seed."""
    if DATABASE is None:
        pytest.skip("real PrimeKG database is not available")
    provider = _provider()
    try:
        seed = provider.resolve_entity(
            EntityLookup(
                mention="8315",
                native_id="8315",
                entity_types=["disease"],
                limit=10,
            )
        ).selected
        assert seed is not None
        before = DATABASE.stat()
        # The real snapshot stores proteins under the type "gene/protein";
        # both drug and disease point *into* the protein, so the second hop is
        # walked backwards.
        result = BoundedPathBuilder(provider, max_intermediates=3).build(
            seed,
            first_relations=["disease_protein"],
            first_target_types=["gene/protein"],
            second_relations=["drug_protein"],
            max_paths=5,
        )
        after = DATABASE.stat()
        # Read-only: neither the size nor the mtime may move.
        assert (after.st_size, after.st_mtime) == (before.st_size, before.st_mtime)
        assert result.tool_calls >= 2, "a real expansion must run both hops"
        assert result.paths, "aspirin should reach a disease through a protein"
        for path in result.paths:
            # Every path must be replayable step by step.
            assert path.start_entity_id == seed.canonical_id
            assert [step.position for step in path.steps] == [0, 1]
            assert path.steps[0].to_entity_id == path.steps[1].from_entity_id
            assert path.end_entity_id != path.start_entity_id
            assert path.intermediate_entity_ids == [path.steps[0].to_entity_id]
            assert path.source_edge_ids == [s.edge_id for s in path.steps]
            # Each step must point at a real edge id from its own query.
            first_edge_ids = {edge.edge_id for edge in result.first_hop_edges}
            second_edge_ids = {edge.edge_id for edge in result.second_hop_edges}
            assert path.steps[0].edge_id in first_edge_ids
            assert path.steps[1].edge_id in second_edge_ids
            assert all(step.query_trace_id for step in path.steps)
        digests = [path.path_hash for path in result.paths]
        assert len(set(digests)) == len(digests), "paths must be deduplicated"
    finally:
        provider.close()


def test_real_primekg_rejects_an_unavailable_relation_filter(tmp_path) -> None:
    """A filter that matches nothing is an empty result, not an error."""
    if DATABASE is None:
        pytest.skip("real PrimeKG database is not available")
    provider = _provider()
    try:
        seed = provider.resolve_entity(
            EntityLookup(
                mention="Aspirin",
                native_id="D001241",
                entity_types=["exposure"],
                limit=10,
            )
        ).selected
        assert seed is not None
        result = BoundedPathBuilder(provider).build(
            seed, first_relations=["no_such_relation"], max_paths=5
        )
        assert result.paths == []
        assert result.error_codes == []
    finally:
        provider.close()
