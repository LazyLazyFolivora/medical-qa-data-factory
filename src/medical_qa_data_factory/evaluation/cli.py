"""The acceptance command.

Step 11 of the real-generation-agent plan. The command runs the pinned
manifest against the current environment and writes the report. It never
adjusts the manifest to fit what it found.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..generation.contracts import EntityGenerationRequest, UpstreamEntitySeed
from ..generation.service import EntityGenerationService
from ..repository import LocalArtifactRepository
from .manifest import EvalManifest, load_eval_manifest, verify_against_environment
from .runner import (
    AcceptanceReport,
    attach_seed_details,
    SeedOutcome,
    build_report,
    finalize,
    summarize_questions,
)


class EvaluationError(RuntimeError):
    """The acceptance run could not be carried out."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def run_evaluation(
    manifest: EvalManifest,
    *,
    workspace: str | Path,
    provider_id: str = "primekg",
    model_mode: str = "rule-based",
    report_path: str | Path | None = None,
) -> AcceptanceReport:
    """Run the manifest's seeds and produce the report."""
    root = Path(workspace)
    repository = LocalArtifactRepository(root)
    report = build_report(manifest, mode=model_mode)

    # The environment is checked before anything runs, so a mismatch is
    # recorded rather than quietly ignored.
    graph_snapshot_id = _graph_snapshot_id(manifest, provider_id)
    problems = verify_against_environment(manifest, graph_snapshot_id=graph_snapshot_id)
    report.environment_problems = list(problems)

    for seed in manifest.seeds:
        outcome = SeedOutcome(
            seed_id=seed.seed_id,
            canonical_name=seed.canonical_name,
        )
        started = time.monotonic()
        try:
            request = EntityGenerationRequest(
                task_id=f"{manifest.manifest_id}-{seed.seed_id}",
                workspace=str(root),
                seed=UpstreamEntitySeed(
                    provider_id=seed.provider_id,
                    native_id=seed.native_id,
                    entity_type=seed.entity_type,
                    canonical_name=seed.canonical_name,
                ),
                question_count=seed.target_question_count,
                max_hops=seed.max_hops,
            )
            service = EntityGenerationService(
                workspace=root,
                provider_id=provider_id,
                model_mode=model_mode,
            )
            result = service.run(request)
            outcome.episode_id = result.episode_id
            outcome.status = result.status
            outcome.stop_reason = result.stop_reason
            outcome.question_ids = list(result.question_ids)
        except Exception as exc:  # a failed seed is a result, not a crash
            outcome.status = "failed"
            outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
        report.seed_outcomes.append(outcome)

    questions = repository.list_latest_questions()
    summarize_questions(report, questions)
    attach_seed_details(report, questions)
    report.usage_totals = _usage_totals(questions)
    finalize(report)
    if report_path is not None:
        report.write(report_path)
    return report


def _graph_snapshot_id(manifest: EvalManifest, provider_id: str) -> str | None:
    """The snapshot the graph provider actually reports."""
    try:
        from ..knowledge_graph import provider_from_config

        provider = provider_from_config(provider_id)
    except Exception:
        return None
    try:
        snapshot = provider.snapshot()
        return str(snapshot.data_snapshot_id)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()


def _usage_totals(questions: list[Any]) -> dict[str, Any]:
    """The objective usage the run recorded."""
    return {
        "question_count": len(questions),
        "tool_calls": sum(
            1
            for question in questions
            for check in question.validation_checks
            if check.check_type == "schema_references"
        ),
    }


def react_evaluation_command(
    manifest_path: str | Path,
    *,
    provider_id: str = "primekg",
    report_path: str | Path | None = None,
) -> int:
    """The CLI entry point for the multi-hop reachability run."""
    from .react_run import run_react_acceptance

    try:
        manifest = load_eval_manifest(manifest_path)
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    payload = run_react_acceptance(
        manifest,
        provider_id=provider_id,
        report_path=report_path,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    failed = [
        item
        for item in payload["outcomes"]
        if item["status"] != "completed"
    ]
    return 1 if failed else 0


def evaluation_command(
    manifest_path: str | Path,
    *,
    workspace: str | Path,
    provider_id: str = "primekg",
    model_mode: str = "rule-based",
    report_path: str | Path | None = None,
) -> int:
    """The CLI entry point for the acceptance run."""
    try:
        manifest = load_eval_manifest(manifest_path)
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    report = run_evaluation(
        manifest,
        workspace=workspace,
        provider_id=provider_id,
        model_mode=model_mode,
        report_path=report_path,
    )
    print(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2)
    )
    return 0 if report.verdict in {"passed", "incomplete"} else 1


import time  # noqa: E402 - the import sits with the module's other helpers
