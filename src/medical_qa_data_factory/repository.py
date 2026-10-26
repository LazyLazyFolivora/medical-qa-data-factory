"""Immutable local artifact persistence for the single-user MVP."""
from __future__ import annotations

import json

import re
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from .domain import EvidenceBundle, GenerationTask, QuestionSpec


ModelT = TypeVar("ModelT", bound=BaseModel)
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RecordAlreadyExistsError(FileExistsError):
    """Raised when an immutable record version already exists."""


class RecordNotFoundError(FileNotFoundError):
    """Raised when a requested artifact version does not exist."""


def _version_key(stem: str) -> tuple[Any, ...]:
    """A sortable key for a version id.

    Version ids come in two shapes: a dotted sequence ("v1", "v2.3") and an
    opaque id ("version_9f2c..."). The sequence is compared numerically, so
    "v2" sorts after "v1" and "v10" after "v9". An opaque id has no order, so
    it sorts by its text and is only used as a last resort.
    """
    match = re.fullmatch(r"v(\d+(?:\.\d+)*)", stem)
    if match:
        return (0, tuple(int(part) for part in match.group(1).split(".")), "")
    return (1, (), stem)


class LocalArtifactRepository:
    """Store each immutable record version as a standalone JSON file."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @staticmethod
    def _safe(value: str) -> str:
        if not SAFE_IDENTIFIER.fullmatch(value):
            raise ValueError(f"not a safe identifier: {value!r}")
        return value

    def _path(self, collection: str, record_id: str, version: str) -> Path:
        safe_collection = self._safe(collection)
        safe_id = self._safe(record_id)
        safe_version = self._safe(version)
        return self.root / safe_collection / safe_id / f"{safe_version}.json"

    def save(
        self,
        collection: str,
        record_id: str,
        version: str,
        record: BaseModel,
    ) -> Path:
        path = self._path(collection, record_id, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(record.model_dump_json(indent=2))
                stream.write("\n")
        except FileExistsError as exc:
            raise RecordAlreadyExistsError(str(path)) from exc
        self._record_write_order(collection, record_id, version)
        return path

    # ---- write order ---------------------------------------------------

    @property
    def _order_path(self) -> Path:
        return self.root / ".write_order.json"

    def _record_write_order(
        self, collection: str, record_id: str, version: str
    ) -> None:
        """Remember the order in which versions were written.

        The file's modification time cannot decide which version is latest:
        two writes inside one filesystem tick carry the same timestamp, and
        comparing the version ids as text makes "v1" look newer than "v99".
        The write order is recorded explicitly, so "latest" is a fact about
        the repository rather than an accident of the clock.
        """
        order = self._read_write_order()
        key = f"{collection}/{record_id}"
        versions = order.setdefault(key, [])
        if version not in versions:
            versions.append(version)
        self._write_write_order(order)

    def _read_write_order(self) -> dict[str, list[str]]:
        path = self._order_path
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): [str(item) for item in value]
            for key, value in payload.items()
            if isinstance(value, list)
        }

    def _write_write_order(self, order: dict[str, list[str]]) -> None:
        path = self._order_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(order, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def _latest_version(self, collection: str, record_id: str) -> str | None:
        """The version that was written last, if the order is known."""
        order = self._read_write_order()
        versions = order.get(f"{collection}/{record_id}")
        if versions:
            return versions[-1]
        return None

    def load(
        self,
        collection: str,
        record_id: str,
        version: str,
        model: type[ModelT],
    ) -> ModelT:
        path = self._path(collection, record_id, version)
        try:
            payload = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RecordNotFoundError(str(path)) from exc
        return model.model_validate_json(payload)

    def list_versions(self, collection: str, record_id: str) -> list[str]:
        directory = self._path(collection, record_id, "placeholder").parent
        if not directory.exists():
            return []
        return sorted(path.stem for path in directory.glob("*.json"))

    def list_record_ids(self, collection: str) -> list[str]:
        directory = self.root / self._safe(collection)
        if not directory.exists():
            return []
        return sorted(path.name for path in directory.iterdir() if path.is_dir())

    def load_latest(
        self,
        collection: str,
        record_id: str,
        model: type[ModelT],
    ) -> ModelT:
        directory = self._path(collection, record_id, "placeholder").parent
        candidates = list(directory.glob("*.json")) if directory.exists() else []
        if not candidates:
            raise RecordNotFoundError(str(directory))
        # The write order decides which version is latest. The file's
        # modification time cannot: two writes inside one filesystem tick
        # carry the same timestamp, and comparing the version ids as text
        # makes "v1" look newer than "v99".
        latest_name = self._latest_version(collection, record_id)
        if latest_name is not None:
            chosen = directory / f"{latest_name}.json"
            if chosen.exists():
                return model.model_validate_json(chosen.read_text(encoding="utf-8"))
        latest = max(
            candidates,
            key=lambda path: (path.stat().st_mtime_ns, _version_key(path.stem)),
        )
        return model.model_validate_json(latest.read_text(encoding="utf-8"))

    def list_latest_tasks(self) -> list[GenerationTask]:
        tasks = [
            self.load_latest("tasks", record_id, GenerationTask)
            for record_id in self.list_record_ids("tasks")
        ]
        return sorted(tasks, key=lambda task: task.updated_at, reverse=True)

    def list_latest_bundles(self) -> list[EvidenceBundle]:
        return [
            self.load_latest("bundles", record_id, EvidenceBundle)
            for record_id in self.list_record_ids("bundles")
        ]

    def list_latest_questions(self) -> list[QuestionSpec]:
        return [
            self.load_latest("questions", record_id, QuestionSpec)
            for record_id in self.list_record_ids("questions")
        ]

    def load_latest_task(self, task_id: str) -> GenerationTask:
        return self.load_latest("tasks", task_id, GenerationTask)

    def load_latest_bundle(self, bundle_id: str) -> EvidenceBundle:
        return self.load_latest("bundles", bundle_id, EvidenceBundle)

    def load_latest_question(self, question_id: str) -> QuestionSpec:
        return self.load_latest("questions", question_id, QuestionSpec)

    def save_task(self, task: GenerationTask) -> Path:
        return self.save("tasks", task.task_id, task.record_version, task)

    def load_task(self, task_id: str, version: str) -> GenerationTask:
        return self.load("tasks", task_id, version, GenerationTask)

    def save_bundle(self, bundle: EvidenceBundle) -> Path:
        return self.save("bundles", bundle.bundle_id, bundle.bundle_version, bundle)

    def load_bundle(self, bundle_id: str, version: str) -> EvidenceBundle:
        return self.load("bundles", bundle_id, version, EvidenceBundle)

    def save_question(self, question: QuestionSpec) -> Path:
        return self.save(
            "questions", question.question_id, question.question_version, question
        )

    def load_question(self, question_id: str, version: str) -> QuestionSpec:
        return self.load("questions", question_id, version, QuestionSpec)

