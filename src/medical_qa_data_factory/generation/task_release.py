"""Export validated tasks with a physical public/hidden data boundary."""
from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from pydantic import Field

from ..domain.common import ContractModel
from .task_service import (
    CtdEvidenceCatalogEntry,
    EvidenceCatalogEntry,
    TaskGenerationResult,
)
from .objective_question_contracts import (
    ObjectiveValidationReport,
    ObjectiveVerdict,
)


class ObjectiveQuestionReleaseDenied(RuntimeError):
    """The objective-question report cannot authorize a release."""


class ObjectiveQuestionReleaseGate:
    """Deterministic gate shared by every objective-question exporter."""

    @staticmethod
    def assert_releasable(
        report: ObjectiveValidationReport,
        *,
        current_input_digest: str | None = None,
    ) -> None:
        if (
            report.verdict is not ObjectiveVerdict.PASS
            or not report.release_allowed
        ):
            raise ObjectiveQuestionReleaseDenied(
                "only ObjectiveValidationReport.PASS can be released"
            )
        if (
            current_input_digest is not None
            and report.input_digest != current_input_digest
        ):
            raise ObjectiveQuestionReleaseDenied(
                "validation report is stale for the current input digest"
            )


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
                "gold_evidence_ids",
                "acceptable_evidence_sets",
                "completion_rule",
                "evidence_catalog_ref",
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
                    key=self._catalog_id,
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
        if result.episode is not None:
            hidden_data["generation_episodes.jsonl"] = [
                result.episode.model_dump(mode="json")
            ]
        counterfactual_rows = [
            {
                "task_id": report.task_id,
                **item.model_dump(mode="json"),
            }
            for report in result.validation_reports
            for item in report.qualifier_counterfactuals
        ]
        if counterfactual_rows:
            hidden_data["qualifier_counterfactuals.jsonl"] = (
                counterfactual_rows
            )
        pubmed_json_files = self._add_pubmed_artifacts(
            result, hidden_data, hidden_dir
        )
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
            "question_diversity": self._question_diversity(published),
        }
        if result.episode is not None:
            quality["stop_reason"] = result.episode.stop_reason
            quality["search_complete"] = result.episode.search_complete
        self._write_json(hidden_dir / "quality_report.json", quality)

        public_files = {
            name: {
                "sha256": self._sha256(public_dir / name),
                "bytes": (public_dir / name).stat().st_size,
            }
            for name in sorted(public_data)
        }
        hidden_artifact_names = [
            *hidden_data,
            *pubmed_json_files,
            "quality_report.json",
        ]
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
        if result.episode is not None:
            release_payload["episode_id"] = result.episode.episode_id
            release_payload["provenance"] = result.episode.provenance
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
        if result.episode is not None:
            shared_manifest["episode_id"] = result.episode.episode_id
            shared_manifest["provenance"] = result.episode.provenance
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
    def _add_pubmed_artifacts(
        result: TaskGenerationResult,
        hidden_data: dict[str, list[dict[str, Any]]],
        hidden_dir: Path,
    ) -> list[str]:
        entries = [
            item
            for item in result.evidence_catalog
            if isinstance(item, CtdEvidenceCatalogEntry)
        ]
        manifests = {
            item.pubmed_corpus.manifest_id: item.pubmed_corpus
            for item in entries
            if item.pubmed_corpus is not None
        }
        if not manifests:
            return []
        assessments = {
            (assessment.pmid, assessment.evidence_id): assessment
            for item in entries
            if item.fusion is not None
            for assessment in item.fusion.pubmed_assessments
        }
        documents = {
            evidence.evidence_id: evidence
            for item in entries
            if item.fusion is not None
            for evidence in item.fusion.evidence_records
            if evidence.provider_id == "pubmed"
        }
        spans = {
            assessment.span.span_id: assessment.span
            for assessment in assessments.values()
        }
        competitors = {
            (
                outcome.answer_id,
                outcome.status,
                tuple(outcome.reason_codes),
            ): outcome
            for item in entries
            for outcome in item.competitor_outcomes
        }
        fusions = {
            item.fusion.fusion_id: item.fusion
            for item in entries
            if item.fusion is not None
        }
        identity_rows = {
            (
                item.primekg_subject_id,
                item.primekg_object_id,
                item.mapping_status.value if item.mapping_status else None,
                item.record.disease_id,
            ): {
                "primekg_subject_id": item.primekg_subject_id,
                "primekg_object_id": item.primekg_object_id,
                "ctd_chemical_id": item.record.chemical_id,
                "ctd_disease_id": item.record.disease_id,
                "mapping_status": (
                    item.mapping_status.value
                    if item.mapping_status is not None
                    else None
                ),
                "source_snapshot_id": item.snapshot_id,
            }
            for item in entries
            if item.primekg_subject_id is not None
            and item.primekg_object_id is not None
        }
        TaskReleaseExporter._write_json(
            hidden_dir / "pubmed_corpus_manifest.json",
            {
                "schema_version": "pubmed-corpus-manifest-set-v1",
                "manifests": [
                    manifests[key].model_dump(mode="json")
                    for key in sorted(manifests)
                ],
            },
        )
        TaskReleaseExporter._write_json(
            hidden_dir / "primekg_ctd_identity_map.snapshot.json",
            {
                "schema_version": "primekg-ctd-identity-snapshot-v1",
                "entries": [
                    identity_rows[key] for key in sorted(identity_rows)
                ],
            },
        )
        hidden_data["pubmed_documents.jsonl"] = [
            documents[key].model_dump(mode="json")
            for key in sorted(documents)
        ]
        hidden_data["pubmed_spans.jsonl"] = [
            spans[key].model_dump(mode="json") for key in sorted(spans)
        ]
        hidden_data["pubmed_assessments.jsonl"] = [
            assessments[key].model_dump(mode="json")
            for key in sorted(assessments)
        ]
        hidden_data["ctd_pubmed_fusions.jsonl"] = [
            fusions[key].model_dump(mode="json")
            for key in sorted(fusions)
        ]
        hidden_data["competitor_reports.jsonl"] = [
            competitors[key].model_dump(mode="json")
            for key in sorted(competitors)
        ]
        return [
            "pubmed_corpus_manifest.json",
            "primekg_ctd_identity_map.snapshot.json",
        ]

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

    @staticmethod
    def _question_diversity(published) -> dict[str, Any]:
        questions = [item.planner_input.question for item in published]
        signatures: list[str] = []
        for item, question in zip(published, questions, strict=True):
            public_names = sorted(
                item.validated_task.task.public_entity_names.values(),
                key=len,
                reverse=True,
            )
            normalized = question.casefold()
            for name in public_names:
                normalized = normalized.replace(name.casefold(), "<entity>")
            normalized = re.sub(r"\d+(?:\.\d+)?", "<n>", normalized)
            normalized = re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)
            signatures.append(normalized)
        pair_count = len(questions) * (len(questions) - 1) // 2
        near_duplicates = sum(
            1
            for left in range(len(signatures))
            for right in range(left + 1, len(signatures))
            if SequenceMatcher(
                None,
                signatures[left],
                signatures[right],
                autojunk=False,
            ).ratio()
            >= 0.9
        )
        counts: dict[str, int] = {}
        for signature in signatures:
            counts[signature] = counts.get(signature, 0) + 1
        return {
            "status": (
                "not_applicable_single_question"
                if len(questions) < 2
                else "reported"
            ),
            "question_count": len(questions),
            "unique_signature_count": len(counts),
            "near_duplicate_pair_count": near_duplicates,
            "near_duplicate_pair_ratio": (
                near_duplicates / pair_count if pair_count else None
            ),
            "max_signature_share": (
                max(counts.values()) / len(questions) if questions else None
            ),
        }

    @staticmethod
    def _catalog_id(
        item: EvidenceCatalogEntry | CtdEvidenceCatalogEntry,
    ) -> str:
        if isinstance(item, CtdEvidenceCatalogEntry):
            return item.catalog_id
        return item.edge.edge_id
