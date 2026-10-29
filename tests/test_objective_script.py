"""The operator script validates both workbench and generation workflows."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "objective.ps1"


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable is not None
    return executable


def test_dry_run_describes_the_vue_workbench_command(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-Mode",
            "web",
            "-Workspace",
            str(tmp_path / "web"),
            "-DryRun",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["mode"] == "web"
    assert payload["workspace"] == str(tmp_path / "web")
    assert "web" in payload["arguments"]
    assert "serve" in payload["arguments"]


def test_dry_run_builds_a_unique_objective_generation_run(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-Mode",
            "generate",
            "-EntityName",
            "BRCA1",
            "-EntityId",
            "672",
            "-EntityType",
            "gene",
            "-RunId",
            "objective-script-test",
            "-Workspace",
            str(tmp_path / "run"),
            "-Output",
            str(tmp_path / "release"),
            "-DryRun",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["mode"] == "generate"
    assert payload["run_id"] == "objective-script-test"
    assert payload["workspace"] == str(tmp_path / "run")
    assert payload["output"] == str(tmp_path / "release")
    assert "objective" in payload["arguments"]
    assert "BRCA1" in payload["arguments"]


def test_script_accepts_comma_separated_providers(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-Mode",
            "generate",
            "-EntityName",
            "ZRSR2",
            "-RunId",
            "objective-provider-list",
            "-Workspace",
            str(tmp_path / "run"),
            "-Output",
            str(tmp_path / "release"),
            "-Providers",
            "hgnc,ncbi_gene,pubmed",
            "-DryRun",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    arguments = json.loads(result.stdout)["arguments"]
    assert arguments.count("--provider") == 3
    assert "ncbi_gene" in arguments
