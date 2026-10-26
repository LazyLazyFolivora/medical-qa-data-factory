"""Append-only persistence for requirements-v2 training artifacts."""
from __future__ import annotations

from .repository import GenerationRepository
from .training import (
    GenerationRunMetadata,
    PlannerStep,
    PlannerTrajectory,
    PlannerTrainingSample,
    QAItem,
)

PLANNER_STEPS = "planner_steps"
PLANNER_TRAJECTORIES = "planner_trajectories"
PLANNER_TRAINING_SAMPLES = "planner_training_samples"
QA_ITEMS = "qa_items"
GENERATION_RUN_METADATA = "generation_run_metadata"
LITERATURE_EVIDENCE = "literature_evidence"
LITERATURE_SUPPORT_ASSESSMENTS = "literature_support_assessments"


class TrainingRepository:
    def __init__(self, root: str) -> None:
        self._generation = GenerationRepository(root)

    def save_step(self, step: PlannerStep) -> str:
        return self._generation.save_idempotent(
            PLANNER_STEPS, step.step_id, "v1", step
        )

    def save_trajectory(self, trajectory: PlannerTrajectory) -> str:
        return self._generation.save_idempotent(
            PLANNER_TRAJECTORIES,
            trajectory.trajectory_id,
            "v1",
            trajectory,
        )

    def save_sample(self, sample: PlannerTrainingSample) -> str:
        return self._generation.save_idempotent(
            PLANNER_TRAINING_SAMPLES, sample.sample_id, "v1", sample
        )

    def save_qa_item(self, item: QAItem) -> str:
        return self._generation.save_idempotent(
            QA_ITEMS, item.question_id, item.question_version, item
        )

    def save_metadata(self, metadata: GenerationRunMetadata, episode_id: str) -> str:
        return self._generation.save_idempotent(
            GENERATION_RUN_METADATA, episode_id, "v1", metadata
        )

    def load_trajectory(self, trajectory_id: str) -> PlannerTrajectory:
        return self._generation.artifacts.load_latest(
            PLANNER_TRAJECTORIES, trajectory_id, PlannerTrajectory
        )

    def load_metadata(self, episode_id: str) -> GenerationRunMetadata:
        return self._generation.artifacts.load_latest(
            GENERATION_RUN_METADATA, episode_id, GenerationRunMetadata
        )

    def list_trajectories(self) -> list[PlannerTrajectory]:
        return [
            self._generation.artifacts.load_latest(
                PLANNER_TRAJECTORIES, record_id, PlannerTrajectory
            )
            for record_id in self._generation.artifacts.list_record_ids(
                PLANNER_TRAJECTORIES
            )
        ]

    def list_planner_steps(self) -> list[PlannerStep]:
        steps = [
            self._generation.artifacts.load_latest(
                PLANNER_STEPS, record_id, PlannerStep
            )
            for record_id in self._generation.artifacts.list_record_ids(
                PLANNER_STEPS
            )
        ]
        return sorted(steps, key=lambda item: (item.episode_id, item.turn))

    def list_qa_items(self) -> list[QAItem]:
        return [
            self._generation.artifacts.load_latest(QA_ITEMS, record_id, QAItem)
            for record_id in self._generation.artifacts.list_record_ids(QA_ITEMS)
        ]
