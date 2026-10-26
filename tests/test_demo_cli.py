import json

from medical_qa_data_factory.cli import main
from medical_qa_data_factory.repository import LocalArtifactRepository


def _run_demo(tmp_path, capsys=None):
    main(
        [
            "demo",
            "run",
            "--output",
            str(tmp_path),
            "--task-id",
            "demo-task",
            "--seed",
            "aspirin",
        ]
    )
    if capsys is not None:
        capsys.readouterr()
    return tmp_path


def test_demo_run_cli_creates_local_artifacts(tmp_path, capsys):
    exit_code = main(
        [
            "demo",
            "run",
            "--output",
            str(tmp_path),
            "--task-id",
            "demo-task",
            "--seed",
            "aspirin",
        ]
    )

    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert summary["status"] == "completed"
    assert summary["bundle_count"] == 1
    assert summary["question_count"] == 1
    assert list((tmp_path / "tasks" / "demo-task").glob("*.json"))


def test_validate_status_reports_required_checks(tmp_path, capsys):
    workspace = _run_demo(tmp_path, capsys)

    exit_code = main(["validate", "status", "--workspace", str(workspace)])

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report
    recorded = {item["check_type"]: item for item in report[0]["required_checks"]}
    assert recorded["question_leakage"]["status"] == "passed"
    assert report[0]["validation_level"] == "machine_validated"


def test_validate_run_creates_new_immutable_version(tmp_path, capsys):
    workspace = _run_demo(tmp_path, capsys)
    repository = LocalArtifactRepository(workspace)
    question_id = repository.list_latest_questions()[0].question_id
    before = len(repository.list_versions("questions", question_id))

    exit_code = main(["validate", "run", "--workspace", str(workspace)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["required_checks"]
    assert payload["validated"][0]["failed_required_checks"] == []
    assert len(repository.list_versions("questions", question_id)) == before + 1
