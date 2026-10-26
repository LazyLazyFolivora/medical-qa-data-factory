from medical_qa_data_factory.domain import (
    ActorType,
    EntityRef,
    GenerationMode,
    GenerationTask,
    LinkStatus,
    TaskStatus,
)
from medical_qa_data_factory.workflow import InvalidTaskTransition, transition_task


def seed() -> EntityRef:
    return EntityRef(
        entity_ref_id="e1",
        raw_mention="aspirin",
        canonical_name="aspirin",
        canonical_id="primekg:e1",
        namespace="primekg",
        entity_type="drug",
        aliases=[],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="manual",
    )


def test_task_transition_is_immutable_and_audited():
    task = GenerationTask.create(
        task_id="task-1",
        generation_mode=GenerationMode.HYBRID,
        seed_entities=[seed()],
    )

    running = transition_task(
        task,
        TaskStatus.RUNNING,
        actor_type=ActorType.HUMAN,
        summary="start generation",
        current_stage="evidence_collection",
    )

    assert task.status is TaskStatus.PENDING
    assert running.status is TaskStatus.RUNNING
    assert running.current_stage == "evidence_collection"
    assert len(running.changes) == 2
    assert running.changes[-1].parent_version == task.record_version
    assert running.record_version != task.record_version


def test_terminal_task_cannot_restart():
    task = GenerationTask.create(
        task_id="task-1",
        generation_mode=GenerationMode.AUTOMATIC,
        seed_entities=[seed()],
    )
    running = transition_task(task, TaskStatus.RUNNING, summary="start")
    completed = transition_task(running, TaskStatus.COMPLETED, summary="done")

    try:
        transition_task(completed, TaskStatus.RUNNING, summary="restart")
    except InvalidTaskTransition as exc:
        assert "completed -> running" in str(exc)
    else:
        raise AssertionError("terminal transition should fail")

