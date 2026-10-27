import json

from medical_qa_data_factory.cli import main
from medical_qa_data_factory.schema_export import export_json_schemas


def test_export_json_schemas_creates_versioned_contracts(tmp_path):
    paths = export_json_schemas(tmp_path)

    names = {path.name for path in paths}
    assert "generation-task.schema.json" in names
    assert "evidence-bundle.schema.json" in names
    assert "question-spec.schema.json" in names
    for expected in (
        "task-spec.schema.json",
        "validated-task.schema.json",
        "validation-environment.schema.json",
        "validation-report.schema.json",
        "planner-question-input.schema.json",
        "evaluation-target.schema.json",
        "source-neutral-evidence-record.schema.json",
        "source-neutral-evidence-store.schema.json",
        "normalized-claim.schema.json",
        "pubmed-corpus-manifest.schema.json",
        "pubmed-evidence-span.schema.json",
        "ctd-evidence-extraction.schema.json",
        "ctd-evidence-fusion.schema.json",
        "ctd-world-evaluation.schema.json",
        "ctd-candidate-proposal.schema.json",
        "ctd-qualifier-counterfactual.schema.json",
    ):
        assert expected in names, expected
    question_schema = json.loads(
        (tmp_path / "question-spec.schema.json").read_text(encoding="utf-8")
    )
    assert question_schema["title"] == "QuestionSpec"
    assert "canonical_question" in question_schema["properties"]

    planner_schema = json.loads(
        (tmp_path / "planner-question-input.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert "answer_contract" not in planner_schema["properties"]
    assert "information_requirements" not in planner_schema["properties"]


def test_schema_export_cli(tmp_path, capsys):
    exit_code = main(["schema", "export", "--output", str(tmp_path)])

    assert exit_code == 0
    assert (tmp_path / "generation-task.schema.json").exists()
    assert "Exported 29 schemas" in capsys.readouterr().out
