import json

from medical_qa_data_factory.generation.task_contracts import (
    ProviderSnapshotRef,
    ValidationEnvironment,
)
from medical_qa_data_factory.generation.task_release import TaskReleaseExporter
from medical_qa_data_factory.generation.task_service import (
    ConstrainedTaskGenerationService,
)
from medical_qa_data_factory.knowledge_graph import EntityLookup
from medical_qa_data_factory.knowledge_graph.providers.primekg import (
    PrimeKGSQLiteProvider,
)

from test_generation_world_evaluation import _database


def test_release_separates_public_questions_from_hidden_evaluation_truth(tmp_path):
    provider = PrimeKGSQLiteProvider(_database(tmp_path))
    try:
        seed = provider.resolve_entity(EntityLookup(mention="Drug A")).selected
        assert seed is not None
        snapshot = provider.snapshot()
        environment = ValidationEnvironment(
            environment_id="world-release",
            providers=[
                ProviderSnapshotRef(
                    provider_id="primekg",
                    snapshot_id=snapshot.data_snapshot_id,
                    manifest_hash=snapshot.sha256,
                    completeness="full_snapshot_query",
                )
            ],
            completeness_scope="all matching edges in the frozen graph",
        )
        result = ConstrainedTaskGenerationService(provider).generate(
            seed, environment=environment
        )
    finally:
        provider.close()

    output = tmp_path / "release"
    summary = TaskReleaseExporter().export(result, output)

    public_expected = {
        "planner_questions.jsonl",
        "dataset_manifest.json",
    }
    hidden_expected = {
        "evaluation_targets.jsonl",
        "validated_tasks.jsonl",
        "validation_reports.jsonl",
        "rejected_candidates.jsonl",
        "evidence_catalog.jsonl",
        "generation_traces.jsonl",
        "dataset_manifest.json",
        "quality_report.json",
    }
    assert {"public", "hidden"} == {path.name for path in output.iterdir()}
    assert public_expected == {
        path.name for path in (output / "public").iterdir()
    }
    assert hidden_expected == {
        path.name for path in (output / "hidden").iterdir()
    }
    planner_rows = [
        json.loads(line)
        for line in (output / "public" / "planner_questions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    target_rows = [
        json.loads(line)
        for line in (output / "hidden" / "evaluation_targets.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert planner_rows
    assert {row["qid"] for row in planner_rows} == {
        row["qid"] for row in target_rows
    }
    assert all("answer_contract" not in row for row in planner_rows)
    assert all("information_requirements" not in row for row in planner_rows)
    assert all(row["answer_contract"] for row in target_rows)
    assert summary.published_count == len(planner_rows)
    manifest = json.loads(
        (output / "public" / "dataset_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["environment_id"] == "world-release"
    assert manifest["files"]["planner_questions.jsonl"]["sha256"]
    assert "evaluation_targets.jsonl" not in manifest["files"]
    hidden_manifest = json.loads(
        (output / "hidden" / "dataset_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert hidden_manifest["files"]["evidence_catalog.jsonl"]["sha256"]
    assert (
        output / "hidden" / "generation_traces.jsonl"
    ).read_text(encoding="utf-8")

    public_payload = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (output / "public").rglob("*")
        if path.is_file()
    )
    hidden_answers = {
        answer
        for row in target_rows
        for answer in (
            row["answer_contract"]["canonical_answer_ids"]
            + row["answer_contract"]["accepted_aliases"]
        )
    }
    assert all(answer not in public_payload for answer in hidden_answers)
