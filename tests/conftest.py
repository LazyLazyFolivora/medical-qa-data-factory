"""Shared fixtures. Unit tests never touch the 1.63 GiB production database."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


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
