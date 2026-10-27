from __future__ import annotations

import sqlite3

from medical_qa_data_factory.generation.logical_query import LogicalDirection
from medical_qa_data_factory.generation.task_contracts import (
    ProviderSnapshotRef,
    ValidationEnvironment,
)
from medical_qa_data_factory.generation.task_service import (
    ConstrainedTaskGenerationService,
    RelationPairProfile,
)
from medical_qa_data_factory.knowledge_graph import EntityLookup
from medical_qa_data_factory.knowledge_graph.providers.primekg import (
    PrimeKGSQLiteProvider,
)


def _database(tmp_path):
    path = tmp_path / "primekg-ctd-edges.sqlite"
    with sqlite3.connect(path) as connection:
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
                (
                    "Drug A",
                    "exposure_disease",
                    "Disease One",
                    "C000001",
                    "D1",
                    "chemical",
                    "disease",
                    "CTD",
                ),
                (
                    "Drug A",
                    "exposure_disease",
                    "Disease Two",
                    "C000001",
                    "D2",
                    "chemical",
                    "disease",
                    "MONDO",
                ),
                (
                    "Disease One",
                    "disease_protein",
                    "Protein One",
                    "D1",
                    "P1",
                    "disease",
                    "gene/protein",
                    "DisGeNET",
                ),
                (
                    "Disease Two",
                    "disease_protein",
                    "Protein Two",
                    "D2",
                    "P2",
                    "disease",
                    "gene/protein",
                    "DisGeNET",
                ),
            ],
        )
    return path


def _environment(provider: PrimeKGSQLiteProvider) -> ValidationEnvironment:
    snapshot = provider.snapshot()
    return ValidationEnvironment(
        environment_id="primekg-ctd-world",
        providers=[
            ProviderSnapshotRef(
                provider_id=snapshot.provider_id,
                snapshot_id=snapshot.data_snapshot_id,
                manifest_hash=snapshot.sha256,
                completeness="full_paginated_snapshot_query",
            )
        ],
        completeness_scope="all matching edges in the complete PrimeKG snapshot",
    )


def _profile() -> RelationPairProfile:
    return RelationPairProfile(
        "exposure_disease",
        LogicalDirection.FORWARD,
        "disease_protein",
        LogicalDirection.FORWARD,
        primary_source="CTD",
    )


def test_ctd_edge_can_seed_a_world_validated_constrained_question(
    tmp_path,
) -> None:
    provider = PrimeKGSQLiteProvider(_database(tmp_path), max_limit=1000)
    try:
        seed = provider.resolve_entity(EntityLookup(mention="Drug A")).selected
        assert seed is not None
        result = ConstrainedTaskGenerationService(
            provider,
            relation_profiles=(_profile(),),
        ).generate(
            seed,
            environment=_environment(provider),
        )
    finally:
        provider.close()

    assert len(result.published) == 1
    published = result.published[0]
    assert published.planner_input.question == (
        "与Drug A存在暴露关联、并且与Protein One相关的疾病是哪一种？"
    )
    assert published.validation_report.world_answer_ids == [
        "primekg:disease:D1"
    ]


def test_ctd_source_filter_excludes_mondo_only_edges(tmp_path) -> None:
    path = _database(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE edges SET source='MONDO' WHERE tail_id='D1'"
        )
    provider = PrimeKGSQLiteProvider(path, max_limit=1000)
    try:
        seed = provider.resolve_entity(EntityLookup(mention="Drug A")).selected
        assert seed is not None
        result = ConstrainedTaskGenerationService(
            provider,
            relation_profiles=(_profile(),),
        ).generate(
            seed,
            environment=_environment(provider),
        )
    finally:
        provider.close()

    assert result.published == []
    assert result.candidates_seen == 0
