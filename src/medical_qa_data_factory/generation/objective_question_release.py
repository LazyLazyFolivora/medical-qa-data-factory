"""Public/audit package export for a completed objective agent loop."""
from __future__ import annotations

import json
from pathlib import Path

from .agent_loop import ObjectiveAgentLoopResult
from .task_release import (
    ObjectiveQuestionReleaseGate,
)


def export_objective_release(
    result: ObjectiveAgentLoopResult,
    output_dir: str | Path,
) -> Path:
    destination = Path(output_dir)
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(
            f"release directory is not empty: {destination}"
        )
    public = destination / "public"
    audit = destination / "audit"
    public.mkdir(parents=True, exist_ok=True)
    audit.mkdir(parents=True, exist_ok=True)

    public_rows: list[dict] = []
    report_rows: list[dict] = []
    for item in result.published_questions:
        ObjectiveQuestionReleaseGate.assert_releasable(
            item.validation_report
        )
        public_rows.append(
            {
                "question_id": item.draft.draft_id,
                "question": item.draft.question_text,
                "answer": list(item.draft.answer_values),
                "explanation": item.draft.concise_explanation,
                "schema_version": "objective-public-question-v1",
            }
        )
        report_rows.append(
            item.validation_report.model_dump(mode="json")
        )

    _write_jsonl(
        public / "questions.jsonl",
        public_rows,
    )
    _write_jsonl(
        audit / "validation_reports.jsonl",
        report_rows,
    )
    _write_jsonl(
        audit / "runtime_artifacts.jsonl",
        [
            artifact.model_dump(mode="json")
            for artifact in sorted(
                result.runtime.artifacts.values(),
                key=lambda item: item.artifact_id,
            )
        ],
    )
    _write_jsonl(
        audit / "main_agent_decisions.jsonl",
        [
            decision.model_dump(mode="json")
            for decision in sorted(
                result.runtime.decisions.values(),
                key=lambda item: item.decision_id,
            )
        ],
    )
    _write_json(
        public / "manifest.json",
        {
            "schema_version": "objective-public-package-v1",
            "run_id": result.run_id,
            "question_count": len(public_rows),
            "files": ["questions.jsonl"],
        },
    )
    _write_json(
        audit / "manifest.json",
        {
            "schema_version": "objective-audit-package-v1",
            "run_id": result.run_id,
            "run_state": result.run_state.value,
            "stop_reason": result.stop_reason,
            "files": [
                "validation_reports.jsonl",
                "runtime_artifacts.jsonl",
                "main_agent_decisions.jsonl",
            ],
        },
    )
    return destination


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        for row in rows
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
