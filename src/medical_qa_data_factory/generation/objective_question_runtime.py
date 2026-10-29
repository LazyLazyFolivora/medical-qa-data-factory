"""Single-writer runtime state with atomic commits and child result slots."""
from __future__ import annotations

import json
import threading
from datetime import timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from ..domain.common import ContractModel, UtcDateTime, utc_now
from .objective_question_contracts import (
    Checkpoint,
    HypothesisState,
    MainAgentDecision,
    OperationRecord,
    RunManifest,
    RunState,
    SubagentResult,
    canonical_digest,
    transition_hypothesis,
)


class StaleStateRevision(RuntimeError):
    code = "STALE_INPUT"


class StaleChildRevision(RuntimeError):
    code = "STALE_INPUT"


class OperationConflict(RuntimeError):
    code = "OPERATION_CONFLICT"


class RuntimeBudgetExceeded(RuntimeError):
    code = "BUDGET_EXHAUSTED"


class CheckpointIntegrityError(RuntimeError):
    code = "CHECKPOINT_INTEGRITY_ERROR"


class RuntimeVersionIncompatible(RuntimeError):
    code = "VERSION_INCOMPATIBLE"


class LoopDetected(RuntimeError):
    code = "LOOP_DETECTED"


class RuntimeBudget(ContractModel):
    max_cost: float = Field(gt=0)
    max_model_calls: int = Field(ge=1)
    charged_cost: float = Field(default=0.0, ge=0)
    used_model_calls: int = Field(default=0, ge=0)


class ArtifactEnvelope(ContractModel):
    artifact_id: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    payload: dict[str, Any]
    digest: str | None = None

    @model_validator(mode="after")
    def stamp_digest(self) -> "ArtifactEnvelope":
        expected = canonical_digest(self.payload)
        if self.digest is None:
            self.digest = expected
        elif self.digest != expected:
            raise ValueError("artifact digest does not match payload")
        return self


class ChildResultSlot(ContractModel):
    slot_id: str = Field(min_length=1)
    parent_decision_id: str = Field(min_length=1)
    subtask_revision: int = Field(ge=0)
    state: Literal["OPEN", "COMMITTED"] = "OPEN"
    result: SubagentResult | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    aggregated_commit_sequence: int | None = None


class ObjectiveRuntimeSnapshot(ContractModel):
    schema_version: str = "objective-runtime-v1"
    run_id: str = Field(min_length=1)
    manifest: RunManifest
    run_state: RunState = RunState.CREATED
    state_revision: int = Field(ge=0)
    commit_sequence: int = Field(ge=0)
    hypotheses: dict[str, HypothesisState] = Field(default_factory=dict)
    child_slots: dict[str, ChildResultSlot] = Field(default_factory=dict)
    operations: dict[str, list[OperationRecord]] = Field(default_factory=dict)
    aggregation_commits: dict[str, int] = Field(default_factory=dict)
    artifacts: dict[str, ArtifactEnvelope] = Field(default_factory=dict)
    budget: RuntimeBudget
    stop_reason: str | None = None


class RuntimeCommitEnvelope(ContractModel):
    schema_version: str = "objective-runtime-commit-v1"
    commit_sequence: int = Field(ge=1)
    previous_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot: ObjectiveRuntimeSnapshot


_LOCK_REGISTRY: dict[Path, threading.RLock] = {}
_LOCK_REGISTRY_GUARD = threading.Lock()


def _lock_for(root: Path) -> threading.RLock:
    normalized = root.resolve()
    with _LOCK_REGISTRY_GUARD:
        return _LOCK_REGISTRY.setdefault(normalized, threading.RLock())


class ObjectiveRuntimeRepository:
    """A single atomic runtime commit file per state revision."""

    def __init__(self, root: str | Path, *, run_id: str) -> None:
        self.root = Path(root)
        self.run_id = run_id
        self.commit_dir = self.root / "objective_runtime" / "commits"
        self._lock = _lock_for(self.root)

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        manifest: RunManifest,
        budget: RuntimeBudget,
    ) -> "ObjectiveRuntimeRepository":
        repo = cls(root, run_id=manifest.run_id)
        if repo.commit_dir.exists() and any(repo.commit_dir.glob("*.json")):
            raise FileExistsError("objective runtime already initialized")
        snapshot = ObjectiveRuntimeSnapshot(
            run_id=manifest.run_id,
            manifest=manifest,
            run_state=RunState.CREATED,
            state_revision=1,
            commit_sequence=1,
            budget=budget,
        )
        repo._write_commit(
            snapshot=snapshot,
            previous_digest="0" * 64,
        )
        return repo

    @property
    def _commit_files(self) -> list[Path]:
        if not self.commit_dir.exists():
            return []
        return sorted(
            self.commit_dir.glob("*.json"),
            key=lambda item: int(item.stem),
        )

    def load(self) -> ObjectiveRuntimeSnapshot:
        with self._lock:
            return self._load_latest()

    def checkpoint(self) -> Checkpoint:
        snapshot = self.load()
        return Checkpoint(
            checkpoint_id=f"checkpoint.{self.run_id}.{snapshot.commit_sequence}",
            run_id=self.run_id,
            runtime_bundle_id=snapshot.manifest.runtime_bundle_id,
            confirmed_state=snapshot.run_state.value,
            state_revision=snapshot.state_revision,
            last_committed_sequence=snapshot.commit_sequence,
            open_operation_ids=[],
            created_at=utc_now(),
        )

    def set_hypothesis_state(
        self,
        *,
        expected_state_revision: int,
        hypothesis_id: str,
        target: HypothesisState,
    ) -> ObjectiveRuntimeSnapshot:
        with self._lock:
            snapshot = self._load_latest()
            self._require_revision(snapshot, expected_state_revision)
            hypotheses = dict(snapshot.hypotheses)
            if hypothesis_id in hypotheses:
                transition_hypothesis(hypotheses[hypothesis_id], target)
            elif target is not HypothesisState.PROPOSED:
                raise ValueError(
                    "a new hypothesis must enter through PROPOSED"
                )
            hypotheses[hypothesis_id] = target
            engaged = (
                snapshot.run_state is RunState.CREATED
                or snapshot.run_state is RunState.HYPOTHESES_PROPOSED
            )
            updated = snapshot.model_copy(
                update={
                    "hypotheses": hypotheses,
                    "run_state": (
                        RunState.CANDIDATES_PROCESSING
                        if engaged
                        else snapshot.run_state
                    ),
                }
            )
            return self._commit_next(snapshot, updated)

    def complete_without_hypotheses(
        self,
        *,
        expected_state_revision: int,
        stop_reason: str,
    ) -> ObjectiveRuntimeSnapshot:
        with self._lock:
            snapshot = self._load_latest()
            self._require_revision(snapshot, expected_state_revision)
            if snapshot.hypotheses and any(
                state is HypothesisState.PUBLISHED
                for state in snapshot.hypotheses.values()
            ):
                raise ValueError("a published question is not a zero-question run")
            updated = snapshot.model_copy(
                update={
                    "run_state": RunState.COMPLETED_WITH_ZERO_QUESTIONS,
                    "stop_reason": stop_reason,
                }
            )
            return self._commit_next(snapshot, updated)

    def register_child_slot(
        self,
        *,
        expected_state_revision: int,
        slot_id: str,
        parent_decision_id: str,
        subtask_revision: int,
    ) -> ObjectiveRuntimeSnapshot:
        with self._lock:
            snapshot = self._load_latest()
            if slot_id in snapshot.child_slots:
                existing = snapshot.child_slots[slot_id]
                if existing.parent_decision_id != parent_decision_id:
                    raise OperationConflict(
                        "child slot belongs to another parent decision"
                    )
                return snapshot
            self._require_revision(snapshot, expected_state_revision)
            slots = dict(snapshot.child_slots)
            slots[slot_id] = ChildResultSlot(
                slot_id=slot_id,
                parent_decision_id=parent_decision_id,
                subtask_revision=subtask_revision,
            )
            return self._commit_next(
                snapshot,
                snapshot.model_copy(update={"child_slots": slots}),
            )

    def commit_child_result(
        self,
        *,
        slot_id: str,
        expected_subtask_revision: int,
        result: SubagentResult,
        artifacts: list[ArtifactEnvelope] | None = None,
    ) -> ObjectiveRuntimeSnapshot:
        with self._lock:
            snapshot = self._load_latest()
            slot = snapshot.child_slots.get(slot_id)
            if slot is None:
                raise KeyError(slot_id)
            if slot.state == "COMMITTED":
                if slot.result != result:
                    raise OperationConflict(
                        "child slot is already committed with another result"
                    )
                return snapshot
            if slot.subtask_revision != expected_subtask_revision:
                raise StaleChildRevision(
                    f"slot {slot_id} is at revision {slot.subtask_revision}"
                )
            artifact_map = self._merge_artifacts(snapshot, artifacts or [])
            committed = slot.model_copy(
                update={
                    "subtask_revision": slot.subtask_revision + 1,
                    "state": "COMMITTED",
                    "result": result,
                    "artifact_ids": [item.artifact_id for item in artifacts or []],
                }
            )
            slots = dict(snapshot.child_slots)
            slots[slot_id] = committed
            return self._commit_next(
                snapshot,
                snapshot.model_copy(
                    update={
                        "child_slots": slots,
                        "artifacts": artifact_map,
                    }
                ),
            )

    def aggregate_child_results(
        self,
        *,
        expected_state_revision: int,
        parent_decision_id: str,
    ) -> int:
        with self._lock:
            snapshot = self._load_latest()
            if parent_decision_id in snapshot.aggregation_commits:
                return snapshot.aggregation_commits[parent_decision_id]
            self._require_revision(snapshot, expected_state_revision)
            slots = [
                slot
                for slot in snapshot.child_slots.values()
                if slot.parent_decision_id == parent_decision_id
            ]
            if not slots:
                raise KeyError(parent_decision_id)
            if any(slot.state != "COMMITTED" for slot in slots):
                raise ValueError("not every child result is committed")
            sequence = snapshot.commit_sequence + 1
            updated_slots = {
                slot_id: (
                    slot.model_copy(
                        update={"aggregated_commit_sequence": sequence}
                    )
                    if slot.parent_decision_id == parent_decision_id
                    else slot
                )
                for slot_id, slot in snapshot.child_slots.items()
            }
            aggregate_commits = dict(snapshot.aggregation_commits)
            aggregate_commits[parent_decision_id] = sequence
            self._commit_next(
                snapshot,
                snapshot.model_copy(
                    update={
                        "child_slots": updated_slots,
                        "aggregation_commits": aggregate_commits,
                    }
                ),
            )
            return sequence

    def commit_operation(
        self,
        *,
        expected_state_revision: int,
        record: OperationRecord,
        artifacts: list[ArtifactEnvelope] | None = None,
    ) -> OperationRecord:
        with self._lock:
            snapshot = self._load_latest()
            existing = list(snapshot.operations.get(record.operation_key, []))
            for item in existing:
                if item.attempt_id == record.attempt_id:
                    if _operation_content(item) != _operation_content(record):
                        raise OperationConflict(
                            "attempt identity is already committed with other content"
                        )
                    return item
            committed = [
                item
                for item in existing
                if item.deduplication_status.value == "COMMITTED"
            ]
            if any(
                item.input_digest != record.input_digest for item in committed
            ):
                raise OperationConflict(
                    "operation_key was reused with a different input digest"
                )
            if committed:
                return committed[0]
            self._require_revision(snapshot, expected_state_revision)
            next_cost = snapshot.budget.charged_cost + record.cost
            calls = snapshot.budget.used_model_calls + int(
                record.operation_type.startswith("llm") or record.operation_type.startswith("model")
            )
            if next_cost > snapshot.budget.max_cost:
                raise RuntimeBudgetExceeded("operation would exceed run cost budget")
            if calls > snapshot.budget.max_model_calls:
                raise RuntimeBudgetExceeded("operation would exceed model-call budget")
            sequence = snapshot.commit_sequence + 1
            saved = record.model_copy(update={"commit_sequence": sequence})
            operations = dict(snapshot.operations)
            operations[record.operation_key] = [*existing, saved]
            artifact_map = self._merge_artifacts(snapshot, artifacts or [])
            updated = snapshot.model_copy(
                update={
                    "operations": operations,
                    "artifacts": artifact_map,
                    "budget": snapshot.budget.model_copy(
                        update={
                            "charged_cost": next_cost,
                            "used_model_calls": calls,
                        }
                    ),
                }
            )
            self._commit_next(snapshot, updated)
            return saved

    def verify_integrity(self) -> bool:
        with self._lock:
            files = self._commit_files
            if not files:
                raise CheckpointIntegrityError("runtime has no commits")
            previous_digest = "0" * 64
            previous_sequence = 0
            for path in files:
                try:
                    envelope = RuntimeCommitEnvelope.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                except Exception as exc:
                    raise CheckpointIntegrityError(
                        f"invalid runtime commit {path.name}: {exc}"
                    ) from exc
                if envelope.commit_sequence != previous_sequence + 1:
                    raise CheckpointIntegrityError("commit sequence is not contiguous")
                if envelope.commit_sequence != envelope.snapshot.commit_sequence:
                    raise CheckpointIntegrityError("snapshot commit sequence differs")
                if envelope.previous_snapshot_digest != previous_digest:
                    raise CheckpointIntegrityError("previous commit digest differs")
                actual = canonical_digest(envelope.snapshot.model_dump(mode="json"))
                if actual != envelope.snapshot_digest:
                    raise CheckpointIntegrityError("runtime snapshot digest differs")
                previous_digest = envelope.snapshot_digest
                previous_sequence = envelope.commit_sequence
            return True

    def _load_latest(self) -> ObjectiveRuntimeSnapshot:
        files = self._commit_files
        if not files:
            raise FileNotFoundError("objective runtime has no commits")
        path = files[-1]
        envelope = RuntimeCommitEnvelope.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        actual = canonical_digest(envelope.snapshot.model_dump(mode="json"))
        if actual != envelope.snapshot_digest:
            raise CheckpointIntegrityError("latest runtime snapshot digest differs")
        if envelope.snapshot.run_id != self.run_id:
            raise CheckpointIntegrityError("runtime belongs to another run")
        return envelope.snapshot

    def _commit_next(
        self,
        current: ObjectiveRuntimeSnapshot,
        proposed: ObjectiveRuntimeSnapshot,
    ) -> ObjectiveRuntimeSnapshot:
        next_sequence = current.commit_sequence + 1
        updated = proposed.model_copy(
            update={
                "state_revision": current.state_revision + 1,
                "commit_sequence": next_sequence,
            }
        )
        previous = self._load_latest()
        previous_digest = canonical_digest(previous.model_dump(mode="json"))
        self._write_commit(snapshot=updated, previous_digest=previous_digest)
        return updated

    def _write_commit(
        self,
        *,
        snapshot: ObjectiveRuntimeSnapshot,
        previous_digest: str,
    ) -> None:
        self.commit_dir.mkdir(parents=True, exist_ok=True)
        digest = canonical_digest(snapshot.model_dump(mode="json"))
        envelope = RuntimeCommitEnvelope(
            commit_sequence=snapshot.commit_sequence,
            previous_snapshot_digest=previous_digest,
            snapshot_digest=digest,
            snapshot=snapshot,
        )
        path = self.commit_dir / f"{snapshot.commit_sequence:08d}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                envelope.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _require_revision(
        snapshot: ObjectiveRuntimeSnapshot,
        expected: int,
    ) -> None:
        if snapshot.state_revision != expected:
            raise StaleStateRevision(
                f"expected revision {expected}, current revision "
                f"{snapshot.state_revision}"
            )

    @staticmethod
    def _merge_artifacts(
        snapshot: ObjectiveRuntimeSnapshot,
        artifacts: list[ArtifactEnvelope],
    ) -> dict[str, ArtifactEnvelope]:
        merged = dict(snapshot.artifacts)
        for artifact in artifacts:
            existing = merged.get(artifact.artifact_id)
            if existing is not None and existing.digest != artifact.digest:
                raise OperationConflict(
                    f"artifact {artifact.artifact_id} has different content"
                )
            merged[artifact.artifact_id] = artifact
        return merged


class SubagentLoopGuard:
    def __init__(self, *, max_repeated_decisions: int = 3) -> None:
        if max_repeated_decisions < 1:
            raise ValueError("max_repeated_decisions must be positive")
        self.max_repeated_decisions = max_repeated_decisions
        self._last: MainAgentDecision | None = None
        self._count = 0

    def observe(self, decision: MainAgentDecision) -> None:
        fingerprint = canonical_digest(
            decision.model_dump(mode="json", exclude={"decision_id"})
        )
        previous = (
            canonical_digest(
                self._last.model_dump(mode="json", exclude={"decision_id"})
            )
            if self._last is not None
            else None
        )
        if fingerprint == previous:
            self._count += 1
        else:
            self._count = 1
        self._last = decision
        if self._count > self.max_repeated_decisions:
            raise LoopDetected(
                "main agent repeated the same decision without state gain"
            )


def assess_runtime_bundle(
    manifest: RunManifest,
    *,
    available_bundles: dict[str, str],
) -> bool:
    digest = available_bundles.get(manifest.runtime_bundle_id)
    if digest is None:
        raise RuntimeVersionIncompatible(
            f"runtime bundle {manifest.runtime_bundle_id} is unavailable"
        )
    if digest != manifest.runtime_bundle_digest:
        raise CheckpointIntegrityError("runtime bundle digest does not match manifest")
    return True


def _operation_content(record: OperationRecord) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    payload.pop("commit_sequence", None)
    return payload
