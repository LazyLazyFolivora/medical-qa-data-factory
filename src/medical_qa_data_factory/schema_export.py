"""JSON Schema export for externally consumed data contracts."""
from __future__ import annotations

import json
from pathlib import Path

from .domain import EvidenceBundle, GenerationTask, QuestionSpec
from .generation.run_identity import (
    AttemptRecord,
    GenerationPreparationRun,
    GenerationRunManifest,
)
from .generation.contracts import GenerationSpec, SafetyBudget
from .generation.task_contracts import (
    EvaluationTarget,
    PlannerQuestionInput,
    TaskSpec,
    ValidatedTask,
    ValidationEnvironment,
    ValidationReport,
)
from .generation.training import (
    GenerationRunMetadata,
    PlannerStep,
    PlannerTrajectory,
    PlannerTrainingSample,
    QAItem,
)


SCHEMA_MODELS = {
    "generation-task.schema.json": GenerationTask,
    "evidence-bundle.schema.json": EvidenceBundle,
    "question-spec.schema.json": QuestionSpec,
    "generation-preparation-run.schema.json": GenerationPreparationRun,
    "generation-run-manifest.schema.json": GenerationRunManifest,
    "attempt-record.schema.json": AttemptRecord,
    "generation-spec.schema.json": GenerationSpec,
    "safety-budget.schema.json": SafetyBudget,
    "qa-item.schema.json": QAItem,
    "planner-step.schema.json": PlannerStep,
    "planner-trajectory.schema.json": PlannerTrajectory,
    "planner-training-sample.schema.json": PlannerTrainingSample,
    "generation-run-metadata.schema.json": GenerationRunMetadata,
    "task-spec.schema.json": TaskSpec,
    "validated-task.schema.json": ValidatedTask,
    "validation-environment.schema.json": ValidationEnvironment,
    "validation-report.schema.json": ValidationReport,
    "planner-question-input.schema.json": PlannerQuestionInput,
    "evaluation-target.schema.json": EvaluationTarget,
}


def export_json_schemas(output_dir: str | Path) -> list[Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for filename, model in SCHEMA_MODELS.items():
        path = destination / filename
        payload = json.dumps(
            model.model_json_schema(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        path.write_text(payload + "\n", encoding="utf-8")
        paths.append(path)
    return paths
