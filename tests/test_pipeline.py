from medical_qa_data_factory.demo_adapters import (
    DemoDraftAgent,
    DemoKGTool,
    DemoLiteratureTool,
)
from medical_qa_data_factory.domain import (
    EndpointKind,
    EntityRef,
    GenerationMode,
    GenerationTask,
    LinkStatus,
    TaskStatus,
    UsageScope,
    ValidationLevel,
)
from medical_qa_data_factory.pipeline import DataFactoryPipeline
from medical_qa_data_factory.repository import LocalArtifactRepository


def seed() -> EntityRef:
    return EntityRef(
        entity_ref_id="seed-aspirin",
        raw_mention="阿司匹林",
        canonical_name="aspirin",
        canonical_id="demo:aspirin",
        namespace="demo",
        entity_type="drug",
        aliases=["阿司匹林"],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="manual",
    )


def test_demo_pipeline_runs_seed_to_draft_question(tmp_path):
    repository = LocalArtifactRepository(tmp_path)
    pipeline = DataFactoryPipeline(
        repository=repository,
        kg_tool=DemoKGTool(),
        literature_tool=DemoLiteratureTool(),
        draft_agent=DemoDraftAgent(),
    )
    task = GenerationTask.create(
        task_id="task-demo",
        generation_mode=GenerationMode.AUTOMATIC,
        seed_entities=[seed()],
        target_question_count=1,
    )

    result = pipeline.run(task)

    assert result.task.status is TaskStatus.COMPLETED
    assert len(result.bundles) == 1
    assert len(result.questions) == 1
    assert result.bundles[0].status is ValidationLevel.DRAFT
    assert (
        result.questions[0].validation_level is ValidationLevel.MACHINE_VALIDATED
    )
    assert all(
        check.validator_version.startswith("validators.")
        for check in result.questions[0].validation_checks
    )
    assert all(
        snapshot.endpoint_kind is EndpointKind.MOCK
        for snapshot in result.bundles[0].tool_snapshots
    )
    assert all(
        usage.usage_scope is UsageScope.GENERATION
        for usage in result.task.generation_usage
    )
    assert repository.list_versions("bundles", result.bundles[0].bundle_id)
    assert repository.list_versions("questions", result.questions[0].question_id)


def test_tool_failure_is_recorded_and_task_becomes_partial(tmp_path):
    class FailingLiteratureTool(DemoLiteratureTool):
        def collect(self, seed_entity):
            raise RuntimeError("literature endpoint unavailable")

    pipeline = DataFactoryPipeline(
        repository=LocalArtifactRepository(tmp_path),
        kg_tool=DemoKGTool(),
        literature_tool=FailingLiteratureTool(),
        draft_agent=DemoDraftAgent(),
    )
    task = GenerationTask.create(
        task_id="task-partial",
        generation_mode=GenerationMode.AUTOMATIC,
        seed_entities=[seed()],
    )

    result = pipeline.run(task)

    assert result.task.status is TaskStatus.PARTIAL
    assert result.task.errors[0]["source"] == "literature"
    assert result.task.errors[0]["error_type"] == "RuntimeError"
    assert result.bundles[0].literature_evidence == []


def test_all_evidence_tools_failing_marks_task_failed(tmp_path):
    class FailingKGTool(DemoKGTool):
        def collect(self, seed_entity):
            raise RuntimeError("kg unavailable")

    class FailingLiteratureTool(DemoLiteratureTool):
        def collect(self, seed_entity):
            raise RuntimeError("literature unavailable")

    pipeline = DataFactoryPipeline(
        repository=LocalArtifactRepository(tmp_path),
        kg_tool=FailingKGTool(),
        literature_tool=FailingLiteratureTool(),
        draft_agent=DemoDraftAgent(),
    )
    task = GenerationTask.create(
        task_id="task-failed",
        generation_mode=GenerationMode.AUTOMATIC,
        seed_entities=[seed()],
    )

    result = pipeline.run(task)

    assert result.task.status is TaskStatus.FAILED
    assert len(result.task.errors) == 2
    assert result.questions == []


def test_every_seed_produces_questions(tmp_path):
    """Regression: the per-seed question loop was outside the seed loop."""
    second = seed().model_copy(update={"canonical_id": "demo:ibuprofen"})
    pipeline = DataFactoryPipeline(
        repository=LocalArtifactRepository(tmp_path),
        kg_tool=DemoKGTool(),
        literature_tool=DemoLiteratureTool(),
        draft_agent=DemoDraftAgent(),
    )
    task = GenerationTask.create(
        task_id="task-multi-seed",
        generation_mode=GenerationMode.AUTOMATIC,
        seed_entities=[seed(), second],
        target_question_count=2,
    )

    result = pipeline.run(task)

    assert len(result.bundles) == 2
    assert len(result.questions) == 2
    assert len(result.task.artifact_ids) == 4


def test_non_retryable_error_keeps_retryable_false(tmp_path):
    """An undeclared exception must never be reported as retryable."""

    class FailingKGTool(DemoKGTool):
        def collect(self, seed_entity):
            raise RuntimeError("boom")

    pipeline = DataFactoryPipeline(
        repository=LocalArtifactRepository(tmp_path),
        kg_tool=FailingKGTool(),
        literature_tool=DemoLiteratureTool(),
        draft_agent=DemoDraftAgent(),
    )
    task = GenerationTask.create(
        task_id="task-retryable",
        generation_mode=GenerationMode.AUTOMATIC,
        seed_entities=[seed()],
    )

    result = pipeline.run(task)

    kg_error = next(i for i in result.task.errors if i["source"] == "kg")
    assert kg_error["retryable"] is False
    assert kg_error["code"] == "internal_error"


def test_declared_retryable_error_is_propagated(tmp_path):
    class RetryableKG(DemoKGTool):
        def collect(self, seed_entity):
            error = RuntimeError("database is locked")
            error.code = "kg_query_error"  # type: ignore[attr-defined]
            error.retryable = True  # type: ignore[attr-defined]
            raise error

    pipeline = DataFactoryPipeline(
        repository=LocalArtifactRepository(tmp_path),
        kg_tool=RetryableKG(),
        literature_tool=DemoLiteratureTool(),
        draft_agent=DemoDraftAgent(),
    )
    task = GenerationTask.create(
        task_id="task-retryable-true",
        generation_mode=GenerationMode.AUTOMATIC,
        seed_entities=[seed()],
    )

    result = pipeline.run(task)
    kg_error = next(i for i in result.task.errors if i["source"] == "kg")
    assert kg_error["retryable"] is True
    assert kg_error["code"] == "kg_query_error"

