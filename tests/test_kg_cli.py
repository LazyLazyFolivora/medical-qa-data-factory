"""Diagnostic CLI surface: JSON output and distinct non-zero exit codes."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from medical_qa_data_factory.cli import main


def _write_config(tmp_path: Path, database: Path) -> Path:
    config = tmp_path / "knowledge_graphs.json"
    config.write_text(
        json.dumps(
            {
                "default_provider": "primekg",
                "providers": {
                    "primekg": {
                        "driver": "primekg_sqlite",
                        "database_path": str(database),
                        "default_limit": 100,
                        "max_limit": 1000,
                        "max_hops": 2,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return config


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "tiny.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE edges (head TEXT, relation TEXT, tail TEXT, head_id TEXT,"
        " tail_id TEXT, head_type TEXT, tail_type TEXT, source TEXT)"
    )
    connection.executemany(
        "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("aspirin", "treats", "headache", "1191", "B156", "drug", "disease", "DrugBank"),
        ],
    )
    connection.commit()
    connection.close()
    return path


def _run(argv: list[str], config: Path, monkeypatch) -> int:
    monkeypatch.setenv("MEDICAL_QA_KG_CONFIG", str(config))
    return main(argv)


def test_health_command_succeeds(tmp_path, monkeypatch, capsys) -> None:
    config = _write_config(tmp_path, _database(tmp_path))
    code = _run(["kg", "health", "--provider", "primekg"], config, monkeypatch)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["healthy"] is True
    assert payload["detail"]["edge_count"] == 1


def test_resolve_command_reports_unique_match(tmp_path, monkeypatch, capsys) -> None:
    config = _write_config(tmp_path, _database(tmp_path))
    code = _run(["kg", "resolve", "--entity", "aspirin"], config, monkeypatch)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["selected"]["canonical_id"] == "primekg:drug:1191"


def test_resolve_command_reports_unmatched(tmp_path, monkeypatch, capsys) -> None:
    config = _write_config(tmp_path, _database(tmp_path))
    code = _run(["kg", "resolve", "--entity", "nope"], config, monkeypatch)
    capsys.readouterr()
    assert code == 10


def test_neighbors_command_lists_edges(tmp_path, monkeypatch, capsys) -> None:
    config = _write_config(tmp_path, _database(tmp_path))
    code = _run(
        ["kg", "neighbors", "--entity", "aspirin", "--direction", "outgoing", "--limit", "5"],
        config,
        monkeypatch,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert len(payload["edges"]) == 1
    assert payload["edges"][0]["relation"] == "treats"


def test_unknown_provider_returns_config_exit_code(tmp_path, monkeypatch, capsys) -> None:
    config = _write_config(tmp_path, _database(tmp_path))
    code = _run(["kg", "health", "--provider", "ghost"], config, monkeypatch)
    err = json.loads(capsys.readouterr().err)
    assert code == 2
    assert err["error"]["code"] == "kg_config_error"


def test_missing_database_is_config_error(tmp_path, monkeypatch, capsys) -> None:
    config = _write_config(tmp_path, tmp_path / "absent.sqlite")
    code = _run(["kg", "health"], config, monkeypatch)
    err = json.loads(capsys.readouterr().err)
    # Construction fails fast, so the config error surfaces as exit code 2.
    assert code == 2
    assert err["error"]["code"] == "kg_config_error"
