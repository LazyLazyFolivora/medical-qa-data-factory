from medical_qa_data_factory.generation.task_candidates import (
    ConstrainedIntersectionCandidateSource,
)
from medical_qa_data_factory.generation.task_compiler import TaskCompiler
from medical_qa_data_factory.generation.task_validation import TaskValidator
from medical_qa_data_factory.generation.world_evaluation import (
    PrimeKGWorldEvaluator,
)
from medical_qa_data_factory.generation.task_contracts import (
    ProviderSnapshotRef,
    ValidationEnvironment,
)
from medical_qa_data_factory.knowledge_graph import EntityLookup, NeighborQuery
from medical_qa_data_factory.knowledge_graph.providers.primekg import (
    PrimeKGSQLiteProvider,
)

from test_generation_world_evaluation import _database


def test_real_graph_edges_compile_into_a_world_validated_constraint_task(tmp_path):
    provider = PrimeKGSQLiteProvider(
        _database(tmp_path), default_limit=1, max_limit=1
    )
    try:
        drug = provider.resolve_entity(
            EntityLookup(mention="Drug A")
        ).selected
        assert drug is not None
        target_edges = []
        cursor = None
        while True:
            page = provider.neighbors(
                NeighborQuery(
                    entity=drug,
                    direction="outgoing",
                    relations=["targets"],
                    limit=1,
                    cursor=cursor,
                )
            )
            target_edges.extend(page.edges)
            if not page.truncated:
                break
            cursor = page.next_cursor
        protein_x_edge = next(
            edge for edge in target_edges if edge.tail.native_id == "P1"
        )
        association = provider.neighbors(
            NeighborQuery(
                entity=protein_x_edge.tail,
                direction="outgoing",
                relations=["associated_with"],
                limit=10,
            )
        ).edges[0]

        snapshot_id = provider.snapshot().data_snapshot_id
        candidate = ConstrainedIntersectionCandidateSource().from_edges(
            seed_edge=protein_x_edge,
            constraint_edge=association,
            snapshot_id=snapshot_id,
        )
        world = ValidationEnvironment(
            environment_id="world-real-slice",
            providers=[
                ProviderSnapshotRef(
                    provider_id="primekg",
                    snapshot_id=snapshot_id,
                    manifest_hash=provider.snapshot().sha256,
                    completeness="full_snapshot_query",
                )
            ],
            completeness_scope="all matching edges in the frozen graph",
        )
        task = TaskCompiler().compile(candidate, world)
        report = TaskValidator(
            PrimeKGWorldEvaluator(provider, page_size=1)
        ).validate(task)
    finally:
        provider.close()

    assert candidate.proposed_answer_ids == ["primekg:gene:P1"]
    assert len(task.query.constraints) == 1
    assert report.passed is True
    assert report.world_answer_ids == ["primekg:gene:P1"]
    assert len(task.information_requirements) == 2


def test_compiled_candidate_is_rejected_when_constraint_intersection_is_not_unique(
    tmp_path,
):
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
        drug = provider.resolve_entity(EntityLookup(mention="Drug A")).selected
        assert drug is not None
        targets = provider.neighbors(
            NeighborQuery(
                entity=drug,
                direction="outgoing",
                relations=["targets"],
                limit=10,
            )
        ).edges
        protein_x = next(edge for edge in targets if edge.tail.native_id == "P1")
        association = provider.neighbors(
            NeighborQuery(
                entity=protein_x.tail,
                direction="outgoing",
                relations=["associated_with"],
                limit=10,
            )
        ).edges[0]
        snapshot = provider.snapshot()
        candidate = ConstrainedIntersectionCandidateSource().from_edges(
            seed_edge=protein_x,
            constraint_edge=association,
            snapshot_id=snapshot.data_snapshot_id,
        )
        world = ValidationEnvironment(
            environment_id="world-ambiguous",
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
        task = TaskCompiler().compile(candidate, world)
        report = TaskValidator(PrimeKGWorldEvaluator(provider)).validate(task)
    finally:
        provider.close()

    assert report.passed is False
    assert "world_cardinality_mismatch" in report.reason_codes
    assert report.world_answer_ids == ["primekg:gene:P1", "primekg:gene:P2"]
