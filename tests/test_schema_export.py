import json
from pathlib import Path

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
        "ctd-evidence-catalog-entry.schema.json",
        "ctd-generation-episode.schema.json",
        "ctd-task-run-spec.schema.json",
        "primekg-ctd-disease-mapping.schema.json",
        "primekg-ctd-pubmed-result.schema.json",
        "generated-question-text.schema.json",
        "question-semantic-view.schema.json",
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
    assert "Exported 70 schemas" in capsys.readouterr().out


def test_export_includes_evidence_first_objective_question_contracts(tmp_path):
    paths = export_json_schemas(tmp_path)
    names = {path.name for path in paths}
    expected = {
        "evidence-provider-capability.schema.json",
        "evidence-provider-attempt.schema.json",
        "evidence-provider-request.schema.json",
        "evidence-provider-result.schema.json",
        "evidence-material.schema.json",
        "evidence-anchor.schema.json",
        "entity-resolution.schema.json",
        "candidate-claim.schema.json",
        "candidate-claim-batch.schema.json",
        "question-hypothesis.schema.json",
        "question-hypothesis-batch.schema.json",
        "blind-answer-query.schema.json",
        "verification-plan.schema.json",
        "verification-attempt.schema.json",
        "verification-coverage-report.schema.json",
        "answer-resolution.schema.json",
        "frozen-question-contract.schema.json",
        "structured-derivation-trace.schema.json",
        "public-question-audit-result.schema.json",
        "answer-evidence-audit-result.schema.json",
        "objective-validation-report.schema.json",
        "generation-goal.schema.json",
        "subagent-task.schema.json",
        "subagent-result.schema.json",
        "main-agent-decision.schema.json",
        "run-manifest.schema.json",
        "checkpoint.schema.json",
        "operation-record.schema.json",
        "metric-spec.schema.json",
        "objective-acceptance-suite.schema.json",
    }

    assert expected <= names
    blind_schema = json.loads(
        (tmp_path / "blind-answer-query.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert blind_schema["additionalProperties"] is False
    assert "proposed_answer_values" not in blind_schema["properties"]
    assert "discovery_anchor_ids" not in blind_schema["properties"]


def test_repository_contains_the_versioned_objective_contract_schemas():
    schema_dir = Path(__file__).parents[1] / "schemas" / "v1"
    expected = {
        "evidence-provider-attempt.schema.json",
        "evidence-material.schema.json",
        "blind-answer-query.schema.json",
        "objective-validation-report.schema.json",
        "objective-acceptance-suite.schema.json",
    }

    assert all((schema_dir / name).is_file() for name in expected)
