import pytest

from medical_qa_data_factory.domain import EntityRef, GenerationMode, GenerationTask, LinkStatus
from medical_qa_data_factory.repository import (
    LocalArtifactRepository,
    RecordAlreadyExistsError,
)


def task() -> GenerationTask:
    seed = EntityRef(
        entity_ref_id="seed-1",
        raw_mention="aspirin",
        canonical_name="aspirin",
        canonical_id="demo:aspirin",
        namespace="demo",
        entity_type="drug",
        aliases=[],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="manual",
    )
    return GenerationTask.create(
        task_id="task-1",
        generation_mode=GenerationMode.MANUAL,
        seed_entities=[seed],
    )


def test_repository_saves_and_loads_immutable_record_version(tmp_path):
    repository = LocalArtifactRepository(tmp_path)
    original = task()

    path = repository.save_task(original)
    loaded = repository.load_task(original.task_id, original.record_version)

    assert path.exists()
    assert loaded == original
    assert repository.list_versions("tasks", original.task_id) == [
        original.record_version
    ]


def test_repository_never_overwrites_same_version(tmp_path):
    repository = LocalArtifactRepository(tmp_path)
    original = task()
    repository.save_task(original)

    with pytest.raises(RecordAlreadyExistsError):
        repository.save_task(original)


def test_repository_rejects_path_like_record_ids(tmp_path):
    repository = LocalArtifactRepository(tmp_path)
    original = task().model_copy(update={"task_id": "../outside"})

    with pytest.raises(ValueError, match="safe identifier"):
        repository.save_task(original)

