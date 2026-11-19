"""Shared fixtures. Unit tests never touch the 1.63 GiB production database."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def runtime_database_for_tests(tmp_path, monkeypatch):
    """Runtime tests use the same PostgreSQL backend as production, lazily."""
    import os
    from medical_qa_data_factory.generation.objective_question_runtime import ObjectiveRuntimeRepository
    original = ObjectiveRuntimeRepository.create.__func__
    databases = {}

    def create(cls, *args, **kwargs):
        if kwargs.get("storage_database") is None:
            dsn = os.environ.get("MEDICAL_QA_TEST_POSTGRES_URL")
            if not dsn:
                pytest.skip("runtime tests require MEDICAL_QA_TEST_POSTGRES_URL")
            root = Path(args[0] if args else kwargs["root"])
            workspace = root.parent.parent if root.parent.name == "objective_runs" else root
            if workspace not in databases:
                from medical_qa_data_factory.web.production_db import ProductionDatabase
                database = ProductionDatabase(workspace, dsn)
                database.migrate()
                databases[workspace] = database
            kwargs["storage_database"] = databases[workspace]
        return original(cls, *args, **kwargs)

    monkeypatch.setattr(ObjectiveRuntimeRepository, "create", classmethod(create))
    yield
    for database in databases.values():
        from contextlib import closing
        import psycopg2
        from psycopg2 import sql
        database.close()
        with closing(psycopg2.connect(database.dsn)) as connection:
            with connection, connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(database.schema)))


TINY_SCHEMA = """
CREATE TABLE edges (
    head TEXT,
    relation TEXT,
    tail TEXT,
    head_id TEXT,
    tail_id TEXT,
    head_type TEXT,
    tail_type TEXT,
    source TEXT
);
CREATE INDEX idx_head ON edges(head);
CREATE INDEX idx_tail ON edges(tail);
CREATE INDEX idx_head_relation ON edges(head, relation);
CREATE INDEX idx_tail_relation ON edges(tail, relation);
"""

TINY_ROWS = [
    ("aspirin", "treats", "headache", "1191", "B156", "drug", "disease", "DrugBank"),
    ("aspirin", "contraindicates", "asthma", "1191", "B0002091", "drug", "disease", "DrugBank"),
    ("headache", "has_symptom", "pain", "B156", "C0738697", "disease", "symptom", "DOID"),
    ("PTGS1", "interacts_with", "aspirin", "5742", "1191", "gene", "drug", "DrugBank"),
    ("aspirin", "treats", "headache", "1191", "B156", "drug", "disease", "DrugBank"),
    ("'quoted'", "rel'ates", "weird;name", None, None, "drug", "drug", None),
]


@pytest.fixture
def tiny_primekg(tmp_path: Path) -> Path:
    path = tmp_path / "primekg_tiny.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(TINY_SCHEMA)
        connection.executemany(
            "INSERT INTO edges (head, relation, tail, head_id, tail_id,"
            " head_type, tail_type, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            TINY_ROWS,
        )
        connection.commit()
    finally:
        connection.close()
    return path
