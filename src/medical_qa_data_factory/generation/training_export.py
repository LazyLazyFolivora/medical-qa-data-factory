"""Versioned export of QA items and generation-policy exploration samples."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field

from ..domain.common import ContractModel, utc_now
from .training import (
    PlannerTrainingSample,
    QAItem,
    derive_training_samples,
)
from .training_repository import TrainingRepository

SPLIT_POLICY_VERSION = "split-group-hash-v1"


class SplitLeakageError(RuntimeError):
    code = "split_leakage"


class TrainingExportManifest(ContractModel):
    schema_version: str = "training-export-manifest-v1"
    artifact_role: Literal["generation_exploration"] = "generation_exploration"
    blind_solver_training_compatible: Literal[False] = False
    qa_item_count: int = Field(ge=0)
    planner_sample_count: int = Field(ge=0)
    trajectory_count: int = Field(ge=0)
    split_policy_version: str = SPLIT_POLICY_VERSION
    split_counts: dict[str, int] = Field(default_factory=dict)
    difficulty_counts: dict[str, int] = Field(default_factory=dict)
    sample_quality_counts: dict[str, int] = Field(default_factory=dict)
    label_policy_versions: list[str] = Field(default_factory=list)
    one_split_per_group: bool = True
    leakage_check: Literal["passed", "failed"] = "passed"
    created_at: str


def _split_for_group(group_id: str) -> Literal["train", "validation", "test"]:
    digest = hashlib.sha256(
        f"{SPLIT_POLICY_VERSION}|{group_id}".encode("utf-8")
    ).hexdigest()
    bucket = int(digest[:8], 16) % 100
    if bucket < 80:
        return "train"
    if bucket < 90:
        return "validation"
    return "test"


def _leakage_fingerprint(item: QAItem) -> str:
    payload = {
        "seed": item.source.seed_entity_id,
        "answer": item.answer.canonical_entity_id or item.answer.text,
        "relations": item.reasoning.ordered_relation_chain,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def export_training_data(workspace: str | Path, output: str | Path) -> Path:
    """Export generation-policy exploration, not independent solver trajectories.

    Blind solvers consume TaskReleaseExporter.public/planner_questions.jsonl
    and must collect their own transitions before training.
    """
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)
    qa_path = destination / "qa_items.jsonl"
    sample_path = destination / "planner_training_samples.jsonl"
    manifest_path = destination / "manifest.json"
    for path in (qa_path, sample_path, manifest_path):
        if path.exists():
            raise FileExistsError(
                f"{path} already exists; choose a new versioned output directory"
            )
    repository = TrainingRepository(str(workspace))
    items = sorted(
        repository.list_qa_items(),
        key=lambda item: (item.dataset_split_group_id, item.question_id),
    )
    trajectories = sorted(
        repository.list_trajectories(), key=lambda item: item.trajectory_id
    )
    split_by_group: dict[str, str] = {}
    split_by_fingerprint: dict[str, str] = {}
    qa_payloads: list[dict] = []
    for item in items:
        split = _split_for_group(item.dataset_split_group_id)
        prior = split_by_group.setdefault(item.dataset_split_group_id, split)
        if prior != split:  # pragma: no cover - deterministic function
            raise SplitLeakageError("one split group was assigned two splits")
        fingerprint = _leakage_fingerprint(item)
        prior_split = split_by_fingerprint.setdefault(fingerprint, split)
        if prior_split != split:
            raise SplitLeakageError(
                f"structural fingerprint {fingerprint} crosses "
                f"{prior_split!r} and {split!r}"
            )
        payload = item.model_dump(mode="json")
        payload["dataset_split"] = split
        payload["split_policy_version"] = SPLIT_POLICY_VERSION
        qa_payloads.append(payload)
    samples: list[PlannerTrainingSample] = []
    for trajectory in trajectories:
        samples.extend(derive_training_samples(trajectory))
    samples.sort(key=lambda item: (item.trajectory_id, item.step_id))
    qa_path.write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
            for item in qa_payloads
        ),
        encoding="utf-8",
    )
    sample_path.write_text(
        "".join(
            item.model_dump_json() + "\n"
            for item in samples
        ),
        encoding="utf-8",
    )
    split_counts = Counter(
        payload["dataset_split"] for payload in qa_payloads
    )
    manifest = TrainingExportManifest(
        qa_item_count=len(items),
        planner_sample_count=len(samples),
        trajectory_count=len(trajectories),
        split_counts=dict(sorted(split_counts.items())),
        difficulty_counts=dict(
            sorted(Counter(item.difficulty for item in items).items())
        ),
        sample_quality_counts=dict(
            sorted(Counter(item.quality for item in samples).items())
        ),
        label_policy_versions=sorted(
            {item.label_policy_version for item in samples}
        ),
        created_at=utc_now().isoformat(),
    )
    manifest_path.write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return destination
