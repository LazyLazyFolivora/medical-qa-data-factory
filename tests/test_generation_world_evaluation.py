from datetime import datetime, timezone
import sqlite3

from medical_qa_data_factory.generation.logical_query import (
    LogicalDirection,
    LogicalQuery,
    QueryConstraint,
    QueryStep,
)
from medical_qa_data_factory.generation.world_evaluation import (
    PrimeKGWorldEvaluator,
)
from medical_qa_data_factory.knowledge_graph.providers.primekg import (
    PrimeKGSQLiteProvider,
)


def _database(tmp_path):
    path = tmp_path / "world.sqlite"
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
                ("Drug A", "targets", "Protein X", "D1", "P1", "drug", "gene", "test"),
                ("Drug A", "targets", "Protein Y", "D1", "P2", "drug", "gene", "test"),
                ("Protein X", "associated_with", "Disease One", "P1", "C1", "gene", "disease", "test"),
                ("Protein Y", "associated_with", "Disease Two", "P2", "C2", "gene", "disease", "test"),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _query(snapshot_id: str, *, filters=None):
    query = LogicalQuery(
        query_id="query-world",
        path_id="path-world",
        path_version="v1",
        probe_id="probe-world",
        probe_digest="probe-digest",
        start_entity_id="primekg:drug:D1",
        steps=[
            QueryStep(
                position=0,
                relation="targets",
                direction=LogicalDirection.FORWARD,
                target_entity_type="gene",
            ),
            QueryStep(
                position=1,
                relation="associated_with",
                direction=LogicalDirection.FORWARD,
                target_entity_type="disease",
            ),
        ],
        expected_cardinality="one",
        filters=filters or {},
        snapshot_id=snapshot_id,
        dependency_digest="pending",
        created_at=datetime.now(timezone.utc),
    )
    return query.model_copy(update={"dependency_digest": query.digest()})


def test_world_evaluator_follows_every_bridge_and_every_page(tmp_path):
    provider = PrimeKGSQLiteProvider(
        _database(tmp_path), default_limit=1, max_limit=1
    )
    try:
        evaluator = PrimeKGWorldEvaluator(provider, page_size=1)
        answer = evaluator.answer(_query(provider.snapshot().data_snapshot_id))
    finally:
        provider.close()

    assert answer.status == "ambiguous"
    assert answer.answer_ids == [
        "primekg:disease:C1",
        "primekg:disease:C2",
    ]
    assert answer.truncated is False


def test_world_evaluator_rejects_a_different_snapshot(tmp_path):
    provider = PrimeKGSQLiteProvider(_database(tmp_path))
    try:
        answer = PrimeKGWorldEvaluator(provider).answer(_query("other"))
    finally:
        provider.close()

    assert answer.status == "incomplete"
    assert answer.answer_ids == []
    assert "snapshot" in (answer.failure_reason or "")


def test_world_evaluator_fails_closed_for_unsupported_filters(tmp_path):
    provider = PrimeKGSQLiteProvider(_database(tmp_path))
    try:
        evaluator = PrimeKGWorldEvaluator(provider)
        answer = evaluator.answer(
            _query(
                provider.snapshot().data_snapshot_id,
                filters={"population": "adults"},
            )
        )
    finally:
        provider.close()

    assert answer.status == "incomplete"
    assert answer.answer_ids == []
    assert "unsupported query semantics" in (answer.failure_reason or "")


def test_constraint_intersection_can_make_a_world_answer_unique(tmp_path):
    provider = PrimeKGSQLiteProvider(
        _database(tmp_path), default_limit=1, max_limit=1
    )
    try:
        base = _query(provider.snapshot().data_snapshot_id)
        constrained = base.model_copy(
            update={
                "steps": base.steps[:1],
                "constraints": [
                    QueryConstraint(
                        constraint_id="constraint-disease-one",
                        steps=[
                            QueryStep(
                                position=0,
                                relation="associated_with",
                                direction=LogicalDirection.FORWARD,
                                target_entity_type="disease",
                            )
                        ],
                        terminal_entity_id="primekg:disease:C1",
                    )
                ],
                "dependency_digest": "pending",
            }
        )
        constrained = constrained.model_copy(
            update={"dependency_digest": constrained.digest()}
        )
        answer = PrimeKGWorldEvaluator(provider, page_size=1).answer(constrained)
    finally:
        provider.close()

    assert answer.status == "ok"
    assert answer.answer_ids == ["primekg:gene:P1"]


def test_world_evaluator_rejects_a_start_entity_from_another_provider(tmp_path):
    provider = PrimeKGSQLiteProvider(_database(tmp_path))
    try:
        query = _query(provider.snapshot().data_snapshot_id).model_copy(
            update={"start_entity_id": "other:drug:D1"}
        )
        query = query.model_copy(update={"dependency_digest": query.digest()})
        answer = PrimeKGWorldEvaluator(provider).answer(query)
    finally:
        provider.close()

    assert answer.status == "incomplete"
    assert "provider" in (answer.failure_reason or "")
