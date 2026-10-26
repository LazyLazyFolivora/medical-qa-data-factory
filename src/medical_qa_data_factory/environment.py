"""Runtime environment checks.

Step 0 of the real-generation-agent plan makes ``qa_env`` the only supported
development environment. Importing the package in the wrong interpreter must
fail fast with an actionable message instead of surfacing later as a confusing
partial-import error, because a wrong interpreter can easily be missing
``langgraph`` or holding an incompatible ``pydantic``.
"""
from __future__ import annotations

import importlib.metadata
import os
import sys
from dataclasses import dataclass


#: The single supported Conda environment. Everything else is rejected.
SUPPORTED_ENV_NAME = "qa_env"

#: Minimum versions for the packages the generation graph imports directly.
REQUIRED_PACKAGES: tuple[tuple[str, str], ...] = (
    ("pydantic", "2.11"),
    ("langgraph", "1.2.16"),
    ("langgraph-checkpoint-sqlite", "3.1.2"),
)


@dataclass(frozen=True)
class EnvironmentProblem:
    """One reason the current interpreter cannot run this project."""

    code: str
    message: str
    #: Only blocking problems stop a run. The Conda-environment name is
    #: advisory, because a bare interpreter with the right dependencies works.
    blocking: bool = True


def _current_env_name() -> str | None:
    """The active Conda environment name, or ``None`` outside Conda."""
    return os.environ.get("CONDA_DEFAULT_ENV") or os.environ.get("CONDA_PREFIX")


def _installed_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _is_at_least(actual: str, minimum: str) -> bool:
    def parse(value: str) -> tuple[int, ...]:
        parts: list[int] = []
        for chunk in value.split("."):
            digits = "".join(char for char in chunk if char.isdigit())
            parts.append(int(digits) if digits else 0)
        return tuple(parts)

    return parse(actual) >= parse(minimum)


def check_environment() -> list[EnvironmentProblem]:
    """Return every environment problem, or an empty list when usable."""
    problems: list[EnvironmentProblem] = []
    env_name = _current_env_name()
    # The Conda name is a soft signal: it is reported so the workbench and the
    # CLI can say "you are probably in the wrong interpreter", but it never
    # blocks a run on its own. What must block is a missing or stale
    # dependency, because that is what actually breaks an import later.
    if env_name is None:
        problems.append(
            EnvironmentProblem(
                "environment_not_conda",
                "no active Conda environment; activate qa_env first "
                "(conda activate qa_env)",
                blocking=False,
            )
        )
    elif os.path.basename(str(env_name)) != SUPPORTED_ENV_NAME:
        problems.append(
            EnvironmentProblem(
                "environment_unsupported",
                f"active Conda environment {os.path.basename(str(env_name))!r} is not "
                f"{SUPPORTED_ENV_NAME!r}; run conda activate {SUPPORTED_ENV_NAME}",
                blocking=False,
            )
        )
    for distribution, minimum in REQUIRED_PACKAGES:
        version = _installed_version(distribution)
        if version is None:
            problems.append(
                EnvironmentProblem(
                    "dependency_missing",
                    f"{distribution} is not installed in this interpreter",
                    blocking=True,
                )
            )
        elif not _is_at_least(version, minimum):
            problems.append(
                EnvironmentProblem(
                    "dependency_too_old",
                    f"{distribution} {version} is older than the required {minimum}",
                    blocking=True,
                )
            )
    return problems


def assert_environment() -> None:
    """Raise ``RuntimeError`` for blocking environment problems only."""
    problems = [item for item in check_environment() if item.blocking]
    if not problems:
        return
    detail = "; ".join(item.message for item in problems)
    raise RuntimeError(
        f"unsupported runtime environment (expected {SUPPORTED_ENV_NAME}): {detail}"
    )


def describe_environment() -> dict[str, object]:
    """Structured summary for CLI and workbench diagnostics."""
    env_name = _current_env_name()
    problems = check_environment()
    return {
        "supported_env": SUPPORTED_ENV_NAME,
        "active_env": os.path.basename(str(env_name)) if env_name else None,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "ok": not problems,
        "problems": [
            {
                "code": item.code,
                "message": item.message,
                "blocking": item.blocking,
            }
            for item in problems
        ],
        "packages": {
            distribution: _installed_version(distribution)
            for distribution, _ in REQUIRED_PACKAGES
        },
    }
