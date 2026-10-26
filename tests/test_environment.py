"""The supported environment must be enforced before any work starts."""
from __future__ import annotations

import pytest

from medical_qa_data_factory.environment import (
    REQUIRED_PACKAGES,
    SUPPORTED_ENV_NAME,
    assert_environment,
    check_environment,
    describe_environment,
)


def test_supported_environment_is_qa_env() -> None:
    assert SUPPORTED_ENV_NAME == "qa_env"


def test_required_packages_cover_the_generation_imports() -> None:
    names = {name for name, _ in REQUIRED_PACKAGES}
    # The generation graph imports these directly; a missing or stale one must
    # be reported by the preflight rather than by a partial import.
    assert {"pydantic", "langgraph", "langgraph-checkpoint-sqlite"} <= names


def test_missing_conda_environment_is_reported(monkeypatch) -> None:
    monkeypatch.delenv("CONDA_DEFAULT_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    codes = {problem.code for problem in check_environment()}
    assert "environment_not_conda" in codes


def test_wrong_conda_environment_is_reported(monkeypatch) -> None:
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "base")
    monkeypatch.setenv("CONDA_PREFIX", "/opt/conda")
    problems = check_environment()
    codes = {problem.code for problem in problems}
    assert "environment_unsupported" in codes
    assert any(SUPPORTED_ENV_NAME in problem.message for problem in problems)


def test_wrong_environment_name_is_advisory_not_blocking(monkeypatch) -> None:
    """A bare interpreter with the right dependencies must still run."""
    monkeypatch.delenv("CONDA_DEFAULT_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    blocking = [
        problem for problem in check_environment() if problem.blocking
    ]
    assert blocking == []
    assert_environment()  # must not raise


def test_missing_dependency_is_blocking(monkeypatch) -> None:
    monkeypatch.setenv("CONDA_DEFAULT_ENV", SUPPORTED_ENV_NAME)
    monkeypatch.setattr(
        "medical_qa_data_factory.environment._installed_version",
        lambda distribution: None if distribution == "pydantic" else "9.9.9",
    )
    problems = check_environment()
    assert any(problem.code == "dependency_missing" for problem in problems)
    assert all(problem.blocking for problem in problems)
    with pytest.raises(RuntimeError, match="pydantic is not installed"):
        assert_environment()


def test_stale_dependency_is_blocking(monkeypatch) -> None:
    monkeypatch.setenv("CONDA_DEFAULT_ENV", SUPPORTED_ENV_NAME)
    monkeypatch.setattr(
        "medical_qa_data_factory.environment._installed_version",
        lambda distribution: "1.0.0",
    )
    problems = check_environment()
    assert any(problem.code == "dependency_too_old" for problem in problems)
    with pytest.raises(RuntimeError, match="older than the required"):
        assert_environment()


def test_supported_environment_passes_the_name_check(monkeypatch) -> None:
    monkeypatch.setenv("CONDA_DEFAULT_ENV", SUPPORTED_ENV_NAME)
    monkeypatch.setenv("CONDA_PREFIX", rf"D:\Anaconda\envs\{SUPPORTED_ENV_NAME}")
    assert not [
        problem
        for problem in check_environment()
        if problem.code.startswith("environment_")
    ]


def test_assert_environment_raises_only_for_blocking_problems(monkeypatch) -> None:
    monkeypatch.delenv("CONDA_DEFAULT_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    # The Conda name alone is advisory, so this must not raise.
    assert_environment()
    # A missing dependency is blocking, so this must raise.
    monkeypatch.setattr(
        "medical_qa_data_factory.environment._installed_version",
        lambda distribution: None,
    )
    with pytest.raises(RuntimeError, match="unsupported runtime environment"):
        assert_environment()


def test_describe_environment_is_json_serializable() -> None:
    import json

    payload = describe_environment()
    assert set(payload) >= {"supported_env", "active_env", "ok", "problems", "packages"}
    # The CLI and the workbench both dump this as JSON, so it must survive it.
    json.dumps(payload)
