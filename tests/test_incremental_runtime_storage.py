"""Storage growth, atomicity, recovery and legacy compatibility contracts."""
from contextlib import closing
from copy import deepcopy
import os

import psycopg2
from psycopg2 import sql
import pytest

from medical_qa_data_factory.generation.runtime_storage import (
    ContentStore, PostgresRuntimeStore, StorageConflict, StorageIntegrityError,
    apply_delta, difference, pack_snapshot, unpack_snapshot,
)
from medical_qa_data_factory.generation.objective_question_runtime import (
    ArtifactEnvelope, ObjectiveRuntimeRepository, RuntimeBudget,
)
from medical_qa_data_factory.generation.objective_question_contracts import RunState
from medical_qa_data_factory.web.production_db import ProductionDatabase
from test_generation_runtime_safety import _manifest, _operation


def test_delta_handles_removal_null_and_unchanged_material():
    old = {"artifacts": {"a": {"payload": "large"}}, "budget": 1, "obsolete": None}
    new = {"artifacts": {"a": {"payload": "large"}}, "budget": None}
    delta = difference(old, new)
    assert "artifacts" not in delta
    assert apply_delta(old, delta) == new
    assert old["budget"] == 1


def test_same_payload_different_artifact_ids_share_content(tmp_path):
    content = ContentStore(tmp_path, threshold=10)
    raw = {"artifacts": {
        "a": {"artifact_id": "a", "payload": {"text": "x" * 10000}},
        "b": {"artifact_id": "b", "payload": {"text": "x" * 10000}},
    }}
    packed = pack_snapshot(raw, content)
    assert packed["artifacts"]["a"]["payload"] == packed["artifacts"]["b"]["payload"]
    assert len([p for p in tmp_path.rglob("*") if p.is_file()]) == 1
    assert unpack_snapshot(packed, content) == raw


def test_content_corruption_and_missing_file_are_explicit(tmp_path):
    content = ContentStore(tmp_path, threshold=1)
    ref = content.pack({"text": "payload"})
    path = content.path(ref["blob"])
    path.write_bytes(b"corrupt")
    with pytest.raises(StorageIntegrityError, match="digest differs"):
        content.unpack(ref)
    with pytest.raises(StorageIntegrityError, match="digest differs"):
        content.pack({"text": "payload"})
    path.unlink()
    with pytest.raises(StorageIntegrityError, match="unavailable"):
        content.unpack(ref)


def test_content_reference_cannot_escape_root(tmp_path):
    with pytest.raises(StorageIntegrityError):
        ContentStore(tmp_path).unpack({"blob": "../../secret", "bytes": 1})


@pytest.fixture
def database(tmp_path):
    dsn = os.environ.get("MEDICAL_QA_TEST_POSTGRES_URL")
    if not dsn:
        pytest.skip("set MEDICAL_QA_TEST_POSTGRES_URL for PostgreSQL integration tests")
    database = ProductionDatabase(tmp_path, dsn)
    database.migrate()
    try:
        yield database
    finally:
        database.close()
        with closing(psycopg2.connect(dsn)) as connection:
            with connection, connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(database.schema)))


def make_repo(tmp_path, database):
    return ObjectiveRuntimeRepository.create(
        tmp_path / "run-1", manifest=_manifest(),
        budget=RuntimeBudget(max_cost=10, max_model_calls=20),
        storage_database=database, content_root=tmp_path / "content",
    )


def test_large_material_is_written_once_across_many_commits(tmp_path, database):
    repo = make_repo(tmp_path, database)
    repo.commit_operation(expected_state_revision=1, record=_operation(), artifacts=[
        ArtifactEnvelope(artifact_id="large", artifact_type="evidence_material",
                         payload={"text": "material" * 50000}),
    ])
    files = [p for p in (tmp_path / "content").rglob("*") if p.is_file()]
    before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in files}
    for _ in range(30):
        current = repo.load()
        repo.set_run_state(expected_state_revision=current.state_revision, target=RunState.MATERIALS_COLLECTED)
    after = {str(p): (p.stat().st_size, p.stat().st_mtime_ns)
             for p in (tmp_path / "content").rglob("*") if p.is_file()}
    assert before == after
    assert not (repo.root / "objective_runtime" / "commits").exists()
    events = repo._store.history()
    assert all("artifacts" not in row["delta"] for row in events[2:])
    assert repo.verify_integrity()
    reopened = ObjectiveRuntimeRepository(repo.root, run_id="run-1")
    assert reopened.load() == repo.load()


def test_stale_writer_cannot_overwrite_checkpoint(tmp_path, database):
    repo = make_repo(tmp_path, database)
    old = repo.load().model_dump(mode="json")
    new = deepcopy(old)
    new.update(commit_sequence=2, state_revision=2)
    repo._store.append(new, 1)
    with pytest.raises(StorageConflict):
        repo._store.append(new, 1)
    assert len(repo._store.history()) == 2
    assert repo.verify_integrity()


def test_failed_event_insert_rolls_back_checkpoint(tmp_path, database):
    repo = make_repo(tmp_path, database)
    with database.transaction() as cursor:
        cursor.execute("ALTER TABLE runtime_events ADD CONSTRAINT test_fail CHECK(sequence < 2)")
    with pytest.raises(psycopg2.IntegrityError):
        repo.set_run_state(expected_state_revision=1, target=RunState.MATERIALS_COLLECTED)
    assert repo.load().commit_sequence == 1
    assert repo.verify_integrity()


def test_pause_resume_and_progress_without_commit_files(tmp_path, database):
    repo = make_repo(tmp_path, database)
    repo.set_run_state(expected_state_revision=1, target=RunState.MATERIALS_COLLECTED)
    repo.request_pause()
    assert repo.pause_at_boundary()
    repo = ObjectiveRuntimeRepository(repo.root, run_id="run-1")
    assert repo.resume_from_recovery().run_state is RunState.MATERIALS_COLLECTED
    from medical_qa_data_factory.web.app import _objective_progress_timing
    assert _objective_progress_timing(repo.root, terminal=False)[0] is not None


def test_event_tampering_detected(tmp_path, database):
    repo = make_repo(tmp_path, database)
    with database.transaction() as cursor:
        cursor.execute("UPDATE runtime_events SET delta='{}'::jsonb WHERE run_id='run-1'")
    with pytest.raises(StorageIntegrityError, match="chain differs"):
        repo._store.verify()


def test_marker_is_credential_free_and_survives_root_move(tmp_path, database):
    repo = make_repo(tmp_path, database)
    marker = PostgresRuntimeStore.marker(repo.root).read_text()
    assert database.dsn not in marker
    assert '"schema"' in marker
    new_root = tmp_path / "renamed-run"
    repo.root.rename(new_root)
    assert ObjectiveRuntimeRepository(new_root, run_id="run-1").load().run_id == "run-1"


def test_blob_write_failure_does_not_advance_checkpoint(tmp_path, database, monkeypatch):
    import medical_qa_data_factory.generation.runtime_storage as storage
    repo = make_repo(tmp_path, database)
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(storage, "atomic_write", fail)
    with pytest.raises(OSError, match="disk full"):
        repo.commit_operation(expected_state_revision=1, record=_operation(), artifacts=[
            ArtifactEnvelope(artifact_id="large", artifact_type="evidence_material",
                             payload={"text": "x" * 50000}),
        ])
    assert repo.load().commit_sequence == 1
    assert repo.verify_integrity()


def test_two_writers_only_one_can_commit_same_revision(tmp_path, database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    repo = make_repo(tmp_path, database)
    payload = repo.load().model_dump(mode="json")
    payload.update(commit_sequence=2, state_revision=2)
    barrier = Barrier(2)
    def write():
        barrier.wait(timeout=10)
        try:
            PostgresRuntimeStore(database, "run-1", repo._store.content).append(payload, 1)
            return "committed"
        except StorageConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write) for _ in range(2)]
        assert sorted(future.result(timeout=20) for future in futures) == ["committed", "conflict"]
    assert repo.verify_integrity()
