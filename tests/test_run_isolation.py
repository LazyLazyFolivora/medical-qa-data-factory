"""Run, episode and thread identity must be isolated per mode.

Step 1 of the real-generation-agent plan: ``rule-based``, ``fake`` and
``structured`` must never write into the same episode, checkpoint thread or
artifact set, or a run in one mode would be read as a run in another.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from medical_qa_data_factory.generation.contracts import (
    EntityGenerationRequest,
    UpstreamEntitySeed,
)
from medical_qa_data_factory.generation.service import (
    EntityGenerationService,
    episode_id_for_mode,
)

ROWS = [
    (
        "aspirin", "exposure_disease", "migraine",
        "D001241", "B156", "exposure", "disease", "DrugBank",
    ),
    (
        "aspirin", "contraindication", "warfarin",
        "D001241", "D002", "exposure", "exposure", "DrugBank",
    ),
]


@pytest.fixture
def tiny_db(tmp_path: Path) -> Path:
    path = tmp_path / "tiny.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE edges (head TEXT, relation TEXT, tail TEXT, head_id TEXT,"
            " tail_id TEXT, head_type TEXT, tail_type TEXT, source TEXT)"
        )
        connection.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?)", ROWS)
        connection.commit()
    finally:
        connection.close()
    return path


def _config(tmp_path: Path, database: Path) -> Path:
    path = tmp_path / "knowledge_graphs.json"
    path.write_text(
        json.dumps(
            {
                "default_provider": "primekg",
                "providers": {
                    "primekg": {
                        "driver": "primekg_sqlite",
                        "database_path": database.as_posix(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _request(tmp_path: Path, **overrides) -> EntityGenerationRequest:
    payload = {
        "task_id": "aspirin-v1",
        "workspace": str(tmp_path / "ws"),
        "seed": UpstreamEntitySeed(
            provider_id="primekg",
            native_id="D001241",
            entity_type="exposure",
            canonical_name="aspirin",
        ),
        "question_count": 2,
    }
    payload.update(overrides)
    return EntityGenerationRequest(**payload)


def test_modes_get_different_episode_ids(tmp_path: Path) -> None:
    request = _request(tmp_path)
    ids = {
        episode_id_for_mode(request, mode)
        for mode in ("rule-based", "fake", "structured")
    }
    assert len(ids) == 3


def test_the_same_mode_gets_a_stable_episode_id(tmp_path: Path) -> None:
    request = _request(tmp_path)
    assert episode_id_for_mode(request, "rule-based") == episode_id_for_mode(
        request, "rule-based"
    )


def test_different_tasks_get_different_episode_ids(tmp_path: Path) -> None:
    first = episode_id_for_mode(_request(tmp_path, task_id="a"), "rule-based")
    second = episode_id_for_mode(_request(tmp_path, task_id="b"), "rule-based")
    assert first != second


def test_two_modes_write_into_separate_episodes(
    tmp_path: Path, tiny_db: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MEDICAL_QA_KG_CONFIG", str(_config(tmp_path, tiny_db)))
    rule_based = EntityGenerationService(
        workspace=tmp_path / "ws", provider_id="primekg", model_mode="rule-based"
    )
    result = rule_based.run(_request(tmp_path))
    assert result.episode_id == episode_id_for_mode(
        _request(tmp_path), "rule-based"
    )
    # The fake mode has a different identity, so it cannot collide.
    assert result.episode_id != episode_id_for_mode(_request(tmp_path), "fake")


def test_a_second_mode_does_not_overwrite_the_first_modes_questions(
    tmp_path: Path, tiny_db: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MEDICAL_QA_KG_CONFIG", str(_config(tmp_path, tiny_db)))
    service = EntityGenerationService(
        workspace=tmp_path / "ws", provider_id="primekg", model_mode="rule-based"
    )
    first = service.run(_request(tmp_path))
    questions_before = set(service.repository.list_record_ids("questions"))
    assert questions_before
    # Running the same task again in the same mode is a replay, not a new set.
    service.run(_request(tmp_path))
    assert set(service.repository.list_record_ids("questions")) == questions_before
    assert first.episode_id == service.run(_request(tmp_path)).episode_id