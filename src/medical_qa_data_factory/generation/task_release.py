"""Export validated tasks with a physical public/hidden data boundary."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from pydantic import Field

from ..domain.common import ContractModel
from .task_service import TaskGenerationResult


class TaskReleaseSummary(ContractModel):
    output_dir: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    published_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    validation_report_count: int = Field(ge=0)


class TaskReleaseExporter:
    """Write an immutable first-slice task release."""

    schema_version = "task-release-v1"

    def export(
        self, result: TaskGenerationResult, output_dir: str | Path
    ) -> TaskReleaseSummary:
        destination = Path(output_dir)
        if destination.exists() and any(destination.iterdir()):
            raise FileExistsError(
                f"release directory is not empty: {destination}"
            )
        destination.mkdir(parents=True, exist_ok=True)
        public_dir = destination / "public"
        hidden_dir = destination / "hidden"
        public_dir.mkdir()
        hidden_dir.mkdir()

        published = sorted(
            result.published, key=lambda item: item.planner_input.qid
        )
        planner_rows = [
            item.planner_input.model_dump(mode="json") for item in published
        ]
        target_rows = [
            item.evaluation_target.model_dump(mode="json")
            for item in published
        ]
        planner_qids = {item["qid"] for item in planner_rows}
        target_qids = {item["qid"] for item in target_rows}
        if planner_qids != target_qids:
            raise ValueError("planner questions and evaluation targets differ")
        for row in planner_rows:
            forbidden = {
                "answer_contract",
                "information_requirements",
                "gold_claim_ids",
                "acceptable_evidence_sets",
                "validation_report_ref",
            }
            leaked = forbidden & set(row)
            if leaked:
                raise ValueError(f"planner view leaks hidden fields: {leaked}")

        public_data = {
            "planner_questions.jsonl": planner_rows,
        }
        hidden_data = {
            "evaluation_targets.jsonl": target_rows,
            "validated_tasks.jsonl": [
                item.validated_task.model_dump(mode="json")
                for item in published
            ],
            "validation_reports.jsonl": [
                item.model_dump(mode="json")
                for item in sorted(
                    result.validation_reports,
                    key=lambda report: report.report_id,
                )
            ],
            "rejected_candidates.jsonl": [
                item.model_dump(mode="json")
                for item in sorted(
                    result.rejections, key=lambda rejection: rejection.candidate_id
                )
            ],
            "evidence_catalog.jsonl": [
                item.model_dump(mode="json")
                for item in sorted(
                    result.evidence_catalog,
                    key=lambda evidence: evidence.edge.edge_id,
                )
            ],
            "generation_traces.jsonl": [
                item.model_dump(mode="json")
                for item in sorted(
                    result.generation_traces,
                    key=lambda trace: trace.query_id,
                )
            ],
        }
        for filename, rows in public_data.items():
            self._write_jsonl(public_dir / filename, rows)
        for filename, rows in hidden_data.items():
            self._write_jsonl(hidden_dir / filename, rows)

        quality = {
            "schema_version": self.schema_version,
            "seed_entity_id": result.seed_entity_id,
            "environment_id": result.environment_id,
            "candidate_count": result.candidates_seen,
            "published_count": len(published),
            "rejected_count": len(result.rejections),
            "rejection_codes": self._count_rejections(result),
            "question_type_distribution": self._count_question_types(result),
        }
        self._write_json(hidden_dir / "quality_report.json", quality)

        public_files = {
            name: {
                "sha256": self._sha256(public_dir / name),
                "bytes": (public_dir / name).stat().st_size,
            }
            for name in sorted(public_data)
        }
        hidden_artifact_names = [*hidden_data, "quality_report.json"]
        hidden_files = {
            name: {
                "sha256": self._sha256(hidden_dir / name),
                "bytes": (hidden_dir / name).stat().st_size,
            }
            for name in sorted(hidden_artifact_names)
        }
        release_payload = {
            "schema_version": self.schema_version,
            "environment_id": result.environment_id,
            "seed_entity_id": result.seed_entity_id,
            "public_files": public_files,
            "hidden_files": hidden_files,
        }
        release_id = "release-" + hashlib.sha256(
            json.dumps(
                release_payload, sort_keys=True, ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()[:24]
        shared_manifest = {
            "schema_version": self.schema_version,
            "release_id": release_id,
            "environment_id": result.environment_id,
            "seed_entity_id": result.seed_entity_id,
        }
        self._write_json(
            public_dir / "dataset_manifest.json",
            {
                **shared_manifest,
                "visibility": "public",
                "files": public_files,
            },
        )
        self._write_json(
            hidden_dir / "dataset_manifest.json",
            {
                **shared_manifest,
                "visibility": "hidden",
                "files": hidden_files,
            },
        )
        return TaskReleaseSummary(
            output_dir=str(destination.resolve()),
            release_id=release_id,
            published_count=len(published),
            rejected_count=len(result.rejections),
            validation_report_count=len(result.validation_reports),
        )

    @staticmethod
    def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
        payload = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        )
        TaskReleaseExporter._atomic_write(path, payload)

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        text = json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True
        )
        TaskReleaseExporter._atomic_write(path, text + "\n")

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        temporary.replace(path)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _count_rejections(result: TaskGenerationResult) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rejection in result.rejections:
            for code in rejection.reason_codes:
                counts[code] = counts.get(code, 0) + 1
        return dict(sorted(counts.items()))

    @staticmethod
    def _count_question_types(result: TaskGenerationResult) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in result.published:
            qtype = item.planner_input.qtype
            counts[qtype] = counts.get(qtype, 0) + 1
        return dict(sorted(counts.items()))
