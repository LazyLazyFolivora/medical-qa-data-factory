from medical_qa_data_factory.generation.task_contracts import (
    ProviderSnapshotRef,
    ValidationEnvironment,
)
from medical_qa_data_factory.generation.task_service import (
    ConstrainedTaskGenerationService,
)
from medical_qa_data_factory.knowledge_graph import EntityLookup
from medical_qa_data_factory.knowledge_graph.providers.primekg import (
    PrimeKGSQLiteProvider,
)

from test_generation_world_evaluation import _database
import pytest


def _primekg_relation_database(tmp_path):
    import sqlite3

    path = tmp_path / "primekg-relations.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE edges (
                head TEXT, relation TEXT, tail TEXT,
                head_id TEXT, tail_id TEXT,
                head_type TEXT, tail_type TEXT, source TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("Breast cancer", "disease_protein", "ERBB2", "D1", "P1", "disease", "gene/protein", "test"),
                ("Breast cancer", "disease_protein", "PIK3CA", "D1", "P2", "disease", "gene/protein", "test"),
                ("PI3K-Akt signaling pathway", "pathway_protein", "ERBB2", "PW1", "P1", "pathway", "gene/protein", "test"),
                ("MAPK signaling pathway", "pathway_protein", "PIK3CA", "PW2", "P2", "pathway", "gene/protein", "test"),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_service_generates_world_validated_questions_from_real_provider_edges(
    tmp_path,
):
    provider = PrimeKGSQLiteProvider(
        _database(tmp_path), default_limit=1, max_limit=1
    )
    try:
        seed = provider.resolve_entity(EntityLookup(mention="Drug A")).selected
        assert seed is not None
        snapshot = provider.snapshot()
        environment = ValidationEnvironment(
            environment_id="world-service",
            providers=[
                ProviderSnapshotRef(
                    provider_id="primekg",
                    snapshot_id=snapshot.data_snapshot_id,
                    manifest_hash=snapshot.sha256,
                    completeness="full_snapshot_query",
                )
            ],
            completeness_scope="all matching edges in the frozen graph",
        )
        result = ConstrainedTaskGenerationService(
            provider, page_size=1
        ).generate(seed, environment=environment)
    finally:
        provider.close()

    assert len(result.published) == 2
    assert result.rejections == []
    assert result.generation_traces
    assert {item.edge.edge_id for item in result.evidence_catalog} >= {
        evidence_id
        for item in result.published
        for evidence_id in item.validated_task.task.candidate_evidence_ids
    }
    questions = {item.planner_input.question for item in result.published}
    assert questions == {
        "Drug A所靶向且与Disease One相关的蛋白是哪一种？",
        "Drug A所靶向且与Disease Two相关的蛋白是哪一种？",
    }
    for item in result.published:
        assert item.semantic_report.passed is True
        assert item.validation_report.passed is True
        assert item.evaluation_target.answer_contract.canonical_answer_ids
        assert "answer_contract" not in item.planner_input.model_dump()


def test_service_keeps_world_cardinality_failures_as_rejections(tmp_path):
    path = _database(tmp_path)
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "Protein Y",
                "associated_with",
                "Disease One",
                "P2",
                "C1",
                "gene",
                "disease",
                "test",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    provider = PrimeKGSQLiteProvider(path)
    try:
        seed = provider.resolve_entity(EntityLookup(mention="Drug A")).selected
        assert seed is not None
        snapshot = provider.snapshot()
        environment = ValidationEnvironment(
            environment_id="world-service-ambiguous",
            providers=[
                ProviderSnapshotRef(
                    provider_id="primekg",
                    snapshot_id=snapshot.data_snapshot_id,
                    manifest_hash=snapshot.sha256,
                    completeness="full_snapshot_query",
                )
            ],
            completeness_scope="all matching edges in the frozen graph",
        )
        result = ConstrainedTaskGenerationService(provider).generate(
            seed, environment=environment
        )
    finally:
        provider.close()

    assert any(
        "world_cardinality_mismatch" in item.reason_codes
        for item in result.rejections
    )
    assert all(item.validation_report.passed for item in result.published)


def test_service_supports_real_primekg_disease_and_pathway_relations(tmp_path):
    provider = PrimeKGSQLiteProvider(_primekg_relation_database(tmp_path))
    try:
        seed = provider.resolve_entity(
            EntityLookup(mention="Breast cancer", entity_types=["disease"])
        ).selected
        assert seed is not None
        snapshot = provider.snapshot()
        environment = ValidationEnvironment(
            environment_id="world-primekg-relations",
            providers=[
                ProviderSnapshotRef(
                    provider_id="primekg",
                    snapshot_id=snapshot.data_snapshot_id,
                    manifest_hash=snapshot.sha256,
                    completeness="full_snapshot_query",
                )
            ],
            completeness_scope="all matching edges in the frozen graph",
        )
        result = ConstrainedTaskGenerationService(provider).generate(
            seed, environment=environment
        )
    finally:
        provider.close()

    assert result.rejections == []
    assert {item.planner_input.question for item in result.published} == {
        "与Breast cancer相关且参与PI3K-Akt signaling pathway的蛋白是哪一种？",
        "与Breast cancer相关且参与MAPK signaling pathway的蛋白是哪一种？",
    }
    for item in result.published:
        constraint_claim = item.validated_task.task.information_requirements[1]
        assert constraint_claim.claim_pattern.subject_id.startswith(
            "primekg:pathway:"
        )
        assert constraint_claim.claim_pattern.object_id.startswith(
            "primekg:gene/protein:"
        )


def test_service_binds_the_environment_to_provider_and_snapshot(tmp_path):
    provider = PrimeKGSQLiteProvider(_database(tmp_path))
    try:
        seed = provider.resolve_entity(EntityLookup(mention="Drug A")).selected
        assert seed is not None
        snapshot = provider.snapshot()
        environment = ValidationEnvironment(
            environment_id="wrong-provider-world",
            providers=[
                ProviderSnapshotRef(
                    provider_id="other-provider",
                    snapshot_id=snapshot.data_snapshot_id,
                    completeness="full_snapshot_query",
                )
            ],
            completeness_scope="test",
        )

        with pytest.raises(ValueError, match="provider snapshot"):
            ConstrainedTaskGenerationService(provider).generate(
                seed, environment=environment
            )
    finally:
        provider.close()
