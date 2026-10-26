"""Replay and resume must never duplicate an immutable artifact."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from medical_qa_data_factory.generation.contracts import (
    EntityGenerationRequest,
    UpstreamEntitySeed,
)
from medical_qa_data_factory.generation.repository import GenerationRepository
from medical_qa_data_factory.generation.service import EntityGenerationService
from medical_qa_data_factory.repository import LocalArtifactRepository

ROWS = [
    ("aspirin", "exposure_disease", "migraine", "D001241", "B156", "exposure", "disease", "DrugBank"),
    ("aspirin", "contraindication", "warfarin", "D001241", "D002", "exposure", "exposure", "DrugBank"),
    ("ibuprofen", "exposure_disease", "aspirin", "D00521", "D001241", "exposure", "exposure", "DrugBank"),
]


@pytest.fixture
def primekg_db(tmp_path: Path) -> Path:
    path = tmp_path / "tiny.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE edges (head TEXT, relation TEXT, tail TEXT, head_id TEXT,"
        " tail_id TEXT, head_type TEXT, tail_type TEXT, source TEXT)"
    )
    connection.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?)", ROWS)
    connection.commit()
    connection.close()
    return path


def _service(
    tmp_path: Path, primekg_db: Path, monkeypatch
) -> EntityGenerationService:
    config = tmp_path / "knowledge_graphs.json"
    config.write_text(
        json.dumps(
            {
                "default_provider": "primekg",
                "providers": {
                    "primekg": {
                        "driver": "primekg_sqlite",
                        "database_path": primekg_db.as_posix(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MEDICAL_QA_KG_CONFIG", str(config))
    return EntityGenerationService(workspace=tmp_path / "ws", provider_id="primekg")


def _request(tmp_path: Path, task_id: str = "a1", **overrides) -> EntityGenerationRequest:
    payload = {
        "task_id": task_id,
        "workspace": str(tmp_path / "ws"),
        "seed": UpstreamEntitySeed(
            provider_id="primekg",
            native_id="D001241",
            entity_type="exposure",
            canonical_name="aspirin",
        ),
        "question_count": 3,
    }
    payload.update(overrides)
    return EntityGenerationRequest(**payload)


def _versions(repository: LocalArtifactRepository) -> dict[str, list[str]]:
    return {
        question_id: repository.list_versions("questions", question_id)
        for question_id in repository.list_record_ids("questions")
    }


def test_same_input_produces_the_same_question_ids(tmp_path, primekg_db, monkeypatch) -> None:
    service = _service(tmp_path, primekg_db, monkeypatch)
    first = service.run(_request(tmp_path, "a1"))
    second = service.run(_request(tmp_path, "a1"))
    assert first.episode_id == second.episode_id
    assert first.question_ids == second.question_ids


def test_replay_does_not_create_new_question_versions(
    tmp_path, primekg_db, monkeypatch
) -> None:
    service = _service(tmp_path, primekg_db, monkeypatch)
    repository = LocalArtifactRepository(tmp_path / "ws")
    service.run(_request(tmp_path, "a1"))
    before = _versions(repository)
    service.run(_request(tmp_path, "a1"))
    assert before == _versions(repository)


def test_repeated_run_keeps_episode_history(
    tmp_path, primekg_db, monkeypatch
) -> None:
    service = _service(tmp_path, primekg_db, monkeypatch)
    episodes = GenerationRepository(tmp_path / "ws")
    first = service.run(_request(tmp_path, "a1"))
    versions = episodes.artifacts.list_versions("episodes", first.episode_id)
    service.run(_request(tmp_path, "a1"))
    for version in versions:
        episodes.load_episode(first.episode_id, version)
    assert episodes.load_latest_episode(first.episode_id).episode_id == first.episode_id


def test_different_task_ids_do_not_collide(tmp_path, primekg_db, monkeypatch) -> None:
    service = _service(tmp_path, primekg_db, monkeypatch)
    first = service.run(_request(tmp_path, "a1"))
    second = service.run(_request(tmp_path, "a2"))
    assert first.episode_id != second.episode_id
    assert set(first.question_ids).isdisjoint(second.question_ids)


def test_crash_after_artifact_write_before_checkpoint_does_not_duplicate(
    tmp_path, primekg_db, monkeypatch
) -> None:
    """Simulate a crash after questions are written but before the checkpoint."""
    service = _service(tmp_path, primekg_db, monkeypatch)
    request = _request(tmp_path, "a1")
    first = service.run(request)
    repository = LocalArtifactRepository(tmp_path / "ws")
    before = _versions(repository)
    # Remove only the LangGraph checkpoint; every business artifact stays put.
    checkpoint = tmp_path / "ws" / "checkpoints" / "generation.sqlite"
    checkpoint.unlink()
    assert not checkpoint.exists()
    replayed = service.run(request)
    assert before == _versions(repository)
    assert replayed.question_ids == first.question_ids


def test_stable_ids_depend_only_on_episode_and_item_key() -> None:
    from medical_qa_data_factory.generation.graph import stable_id

    assert stable_id("ep1", "question", "a|b") == stable_id("ep1", "question", "a|b")
    assert stable_id("ep1", "question", "a|b") != stable_id("ep2", "question", "a|b")
    assert stable_id("ep1", "question", "a|b") != stable_id("ep1", "claim", "a|b")


def test_checkpoints_live_at_the_documented_path(tmp_path, primekg_db, monkeypatch) -> None:
    from medical_qa_data_factory.generation.service import checkpoint_path

    _service(tmp_path, primekg_db, monkeypatch).run(_request(tmp_path, "a1"))
    expected = tmp_path / "ws" / "checkpoints" / "generation.sqlite"
    assert checkpoint_path(tmp_path / "ws") == expected
    assert expected.exists()


def test_checkpoint_is_separate_from_business_records(
    tmp_path, primekg_db, monkeypatch
) -> None:
    _service(tmp_path, primekg_db, monkeypatch).run(_request(tmp_path, "a1"))
    workspace = tmp_path / "ws"
    assert (workspace / "episodes").is_dir()
    assert (workspace / "questions").is_dir()
    assert (workspace / "checkpoints" / "generation.sqlite").is_file()


def test_resume_reports_a_missing_episode(tmp_path, primekg_db, monkeypatch) -> None:
    from medical_qa_data_factory.generation.service import GenerationError

    service = _service(tmp_path, primekg_db, monkeypatch)
    with pytest.raises(GenerationError) as excinfo:
        service.resume(_request(tmp_path, "a1"), "episode.does-not-exist")
    assert excinfo.value.code == "episode_not_found"


def test_resume_reports_a_missing_checkpoint(tmp_path, primekg_db, monkeypatch) -> None:
    from medical_qa_data_factory.generation.service import GenerationError

    service = _service(tmp_path, primekg_db, monkeypatch)
    result = service.run(_request(tmp_path, "a1"))
    (tmp_path / "ws" / "checkpoints" / "generation.sqlite").unlink()
    with pytest.raises(GenerationError) as excinfo:
        service.resume(_request(tmp_path, "a1"), result.episode_id)
    assert excinfo.value.code == "checkpoint_not_found"


def test_resume_continues_an_existing_episode(tmp_path, primekg_db, monkeypatch) -> None:
    service = _service(tmp_path, primekg_db, monkeypatch)
    request = _request(tmp_path, "a1")
    first = service.run(request)
    resumed = service.resume(request, first.episode_id)
    assert resumed.episode_id == first.episode_id
    assert resumed.question_ids == first.question_ids


def test_resume_rejects_a_request_for_a_different_task(
    tmp_path, primekg_db, monkeypatch
) -> None:
    from medical_qa_data_factory.generation.service import GenerationError

    service = _service(tmp_path, primekg_db, monkeypatch)
    first = service.run(_request(tmp_path, "a1"))

    with pytest.raises(GenerationError) as excinfo:
        service.resume(_request(tmp_path, "other-task"), first.episode_id)

    assert excinfo.value.code == "episode_mismatch"


def test_answer_and_references_are_recomputable(tmp_path, primekg_db, monkeypatch) -> None:
    result = _service(tmp_path, primekg_db, monkeypatch).run(_request(tmp_path, "a1"))
    repository = LocalArtifactRepository(tmp_path / "ws")
    bundle = repository.load_latest_bundle(result.bundle_ids[0])
    for question_id in result.question_ids:
        spec = repository.load_latest_question(question_id)
        edge = next(
            item
            for item in bundle.kg_evidence
            if item.evidence_id in spec.evidence_requirements.required_kg_evidence_ids
        )
        answer_ids = {value.get("canonical_id") for value in spec.gold_answer.values}
        assert answer_ids <= {edge.head.canonical_id, edge.tail.canonical_id}
