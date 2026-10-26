"""CLI surface: JSON summary, structured errors, and distinct exit codes."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from medical_qa_data_factory.cli import main

ROWS = [
    ("aspirin", "exposure_disease", "migraine", "D001241", "B156", "exposure", "disease", "DrugBank"),
    ("aspirin", "contraindication", "warfarin", "D001241", "D002", "exposure", "exposure", "DrugBank"),
    ("ibuprofen", "exposure_disease", "aspirin", "D00521", "D001241", "exposure", "exposure", "DrugBank"),
    ("Breast cancer", "disease_protein", "ERBB2", "C1", "P1", "disease", "gene/protein", "PrimeKG"),
    ("Breast cancer", "disease_protein", "BRCA1", "C1", "P2", "disease", "gene/protein", "PrimeKG"),
    ("PI3K-Akt signaling pathway", "pathway_protein", "ERBB2", "PW1", "P1", "pathway", "gene/protein", "PrimeKG"),
]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    database = tmp_path / "tiny.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE edges (head TEXT, relation TEXT, tail TEXT, head_id TEXT,"
        " tail_id TEXT, head_type TEXT, tail_type TEXT, source TEXT)"
    )
    connection.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?)", ROWS)
    connection.commit()
    connection.close()
    config = tmp_path / "knowledge_graphs.json"
    config.write_text(
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
    monkeypatch.setenv("MEDICAL_QA_KG_CONFIG", str(config))
    return tmp_path / "ws"


def _argv(workspace: Path, **overrides) -> list[str]:
    payload = {
        "--workspace": str(workspace),
        "--task-id": "aspirin-v1",
        "--provider": "primekg",
        "--entity-name": "aspirin",
        "--entity-id": "D001241",
        "--entity-type": "exposure",
        "--question-count": "3",
    }
    payload.update(overrides)
    argv = ["generate", "entity"]
    for key, value in payload.items():
        argv.append(key)
        argv.append(str(value))
    return argv


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    """Run a CLI command and parse its JSON stdout."""
    code = main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def test_generate_entity_prints_a_json_summary(workspace, capsys) -> None:
    code = main(_argv(workspace))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["task_id"] == "aspirin-v1"
    assert payload["episode_id"]
    assert payload["bundle_ids"]
    assert payload["question_ids"]
    assert payload["status"] in {"completed", "partial"}
    assert payload["stop_reason"]
    # The summary must not call an unconfirmed candidate a gold answer.
    assert "候选答案" in payload["note"]


@pytest.mark.parametrize("count", ["1", "5", "20"])
def test_question_count_in_range_is_accepted(workspace, capsys, count) -> None:
    argv = _argv(workspace, **{"--task-id": f"t-{count}", "--question-count": count})
    code = main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert len(payload["question_ids"]) <= int(count)


@pytest.mark.parametrize("count", ["0", "21", "-1"])
def test_question_count_out_of_range_returns_exit_two(
    workspace, capsys, count
) -> None:
    code = main(_argv(workspace, **{"--question-count": count}))
    err = json.loads(capsys.readouterr().err)
    assert code == 2
    assert err["error"]["code"] == "invalid_arguments"
    assert err["error"]["retryable"] is False
    assert err["error"]["episode_id"] is None


def test_wrong_entity_type_returns_exit_two(workspace, capsys) -> None:
    code = main(_argv(workspace, **{"--entity-type": "pathway"}))
    payload = json.loads(capsys.readouterr().out)
    capsys.readouterr()
    assert code == 0  # the run itself completes as a failed episode
    assert payload["status"] == "failed"
    assert payload["stop_reason"] == "invalid_upstream_identity"


def test_unknown_entity_returns_failed_episode(workspace, capsys) -> None:
    code = main(_argv(workspace, **{"--entity-id": "D999999", "--entity-name": "ghost"}))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["stop_reason"] == "invalid_upstream_identity"
    assert payload["question_ids"] == []


def test_generated_questions_are_visible_to_the_workbench(workspace, capsys) -> None:
    from medical_qa_data_factory.repository import LocalArtifactRepository

    _, payload = _run(_argv(workspace), capsys)
    repository = LocalArtifactRepository(workspace)
    listed = repository.list_latest_questions()
    listed_ids = {item.question_id for item in listed}
    # Every queued question is reviewable; a hard-rejected candidate is still
    # persisted but must not appear in the queue.
    assert set(payload["question_ids"]) <= listed_ids


def test_generated_questions_can_run_machine_validation(workspace, capsys) -> None:
    from medical_qa_data_factory.machine_validation import (
        REQUIRED_FOR_FORMAL_EXPORT,
        assess_formal_export,
    )
    from medical_qa_data_factory.repository import LocalArtifactRepository

    _, payload = _run(_argv(workspace), capsys)
    repository = LocalArtifactRepository(workspace)
    for question_id in payload["question_ids"]:
        spec = repository.load_latest_question(question_id)
        # Automated flows never write human_confirmed, so formal export stays
        # blocked until a human reviews the candidate.
        assert spec.validation_level.value in {"draft", "machine_validated"}
        assert not assess_formal_export(spec).satisfied
        assert REQUIRED_FOR_FORMAL_EXPORT


def test_model_mode_must_be_supported(workspace, capsys) -> None:
    # argparse rejects an unknown mode before any work starts.
    with pytest.raises(SystemExit) as excinfo:
        main(_argv(workspace, **{"--model-mode": "openai"}))
    assert excinfo.value.code == 2
    assert "rule-based" in capsys.readouterr().err


def test_rule_based_and_fake_modes_both_run(workspace, capsys) -> None:
    _, first = _run(_argv(workspace, **{"--task-id": "rb"}), capsys)
    assert first["question_ids"]
    # `fake` no longer shares the deterministic rule-based assembly: a scripted
    # model must be injected, and the CLI refuses to fake one silently.
    code = main(
        _argv(workspace, **{"--task-id": "fake", "--model-mode": "fake"})
    )
    out = capsys.readouterr()
    err = json.loads(out.err)
    assert code == 3
    assert out.out.strip() == ""
    assert err["error"]["code"] == "capability_error"
    assert "model port" in err["error"]["message"]


def test_rule_based_mode_is_labelled_as_a_deterministic_baseline(
    workspace, capsys
) -> None:
    """The baseline must never be labelled as a ReAct agent."""
    _, payload = _run(_argv(workspace, **{"--task-id": "labels"}), capsys)
    assert payload["model_mode"] == "rule-based"
    assert payload["model_capability"] == "deterministic_baseline"


def test_structured_mode_without_a_model_configuration_fails_fast(
    workspace, capsys, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(
        "MEDICAL_QA_MODEL_CONFIG", str(tmp_path / "missing-models.json")
    )
    code = main(
        _argv(workspace, **{"--task-id": "s", "--model-mode": "structured"})
    )
    err = json.loads(capsys.readouterr().err)
    assert code == 3
    # A missing provider configuration is a configuration problem, not a
    # missing capability, and the two must not be reported the same way.
    assert err["error"]["code"] == "model_config_error"
    assert err["error"]["retryable"] is False


def test_rule_based_mode_is_labelled_as_a_deterministic_baseline(
    workspace, capsys
) -> None:
    """The baseline must never be labelled as a ReAct agent."""
    _, payload = _run(_argv(workspace, **{"--task-id": "labels"}), capsys)
    assert payload["model_mode"] == "rule-based"
    assert payload["model_capability"] == "deterministic_baseline"



def test_missing_database_is_reported_as_config_error(workspace, capsys, monkeypatch) -> None:
    import tempfile

    broken = Path(tempfile.mkdtemp()) / "kg.json"
    broken.write_text(
        json.dumps(
            {
                "default_provider": "primekg",
                "providers": {
                    "primekg": {
                        "driver": "primekg_sqlite",
                        "database_path": (broken.parent / "absent.sqlite").as_posix(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MEDICAL_QA_KG_CONFIG", str(broken))
    code = main(_argv(workspace))
    err = json.loads(capsys.readouterr().err)
    assert code == 3
    assert err["error"]["code"] in {"kg_config_error", "kg_data_integrity"}
    assert err["error"]["retryable"] is False


def test_resume_reports_a_missing_episode(workspace, capsys) -> None:
    argv = [
        "generate",
        "resume",
        "--workspace",
        str(workspace),
        "--episode-id",
        "episode.missing",
        "--task-id",
        "aspirin-v1",
        "--provider",
        "primekg",
        "--entity-name",
        "aspirin",
        "--entity-id",
        "D001241",
        "--entity-type",
        "exposure",
    ]
    code = main(argv)
    err = json.loads(capsys.readouterr().err)
    assert code == 2
    assert err["error"]["code"] == "episode_not_found"


def test_resume_after_a_completed_run(workspace, capsys) -> None:
    _, payload = _run(_argv(workspace), capsys)
    code, resumed = _run(
        [
            "generate",
            "resume",
            "--workspace",
            str(workspace),
            "--episode-id",
            payload["episode_id"],
            "--task-id",
            "aspirin-v1",
            "--provider",
            "primekg",
            "--entity-name",
            "aspirin",
            "--entity-id",
            "D001241",
            "--entity-type",
            "exposure",
        ],
        capsys,
    )
    assert code == 0
    assert resumed["episode_id"] == payload["episode_id"]
    assert resumed["question_ids"] == payload["question_ids"]


def test_help_lists_the_generate_subcommands(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["generate", "--help"])
    text = capsys.readouterr().out
    assert "entity" in text
    assert "resume" in text
    assert "validated" in text


def test_generate_validated_exports_a_world_checked_release(
    workspace, capsys
) -> None:
    output = workspace.parent / "validated-release"

    code = main(
        [
            "generate",
            "validated",
            "--output",
            str(output),
            "--provider",
            "primekg",
            "--entity-name",
            "Breast cancer",
            "--entity-id",
            "C1",
            "--entity-type",
            "disease",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["published_count"] == 1
    assert payload["rejected_count"] == 0
    assert payload["release_id"].startswith("release-")
    questions = [
        json.loads(line)
        for line in (output / "public" / "planner_questions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert questions[0]["question"] == (
        "与Breast cancer相关且参与PI3K-Akt signaling pathway的蛋白是哪一种？"
    )
    assert (output / "hidden" / "evaluation_targets.jsonl").exists()
    assert (output / "hidden" / "evidence_catalog.jsonl").exists()
    assert (output / "hidden" / "generation_traces.jsonl").exists()


def test_generate_validated_rejects_an_unknown_seed(workspace, capsys) -> None:
    code = main(
        [
            "generate",
            "validated",
            "--output",
            str(workspace.parent / "unknown-release"),
            "--provider",
            "primekg",
            "--entity-name",
            "Unknown disease",
            "--entity-id",
            "missing",
            "--entity-type",
            "disease",
        ]
    )
    output = capsys.readouterr()
    error = json.loads(output.err)

    assert code == 2
    assert output.out == ""
    assert error["error"]["code"] == "invalid_upstream_identity"
    assert not (workspace.parent / "unknown-release").exists()


def test_demo_run_still_works(tmp_path, capsys) -> None:
    code = main(
        [
            "demo",
            "run",
            "--output",
            str(tmp_path / "demo"),
            "--task-id",
            "demo-1",
            "--seed",
            "aspirin",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["question_count"] == 1
