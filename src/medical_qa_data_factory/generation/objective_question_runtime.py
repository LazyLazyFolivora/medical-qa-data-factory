"""Single-writer runtime state with atomic commits and child result slots."""
from __future__ import annotations

import threading
from datetime import timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from ..domain.common import ContractModel, UtcDateTime, utc_now
from .objective_question_contracts import (
    Checkpoint,
    GenerationGoal,
    HypothesisState,
    MainAgentDecision,
    OperationRecord,
    RunManifest,
    RunState,
    SubagentResult,
    canonical_digest,
    transition_hypothesis,
)
from .runtime_storage import PostgresRuntimeStore, StorageConflict, StorageIntegrityError


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
    producer_role: str | None = None
    parent_decision_id: str | None = None
    input_artifact_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
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
    goal: GenerationGoal | None = None
    run_state: RunState = RunState.CREATED
    state_revision: int = Field(ge=0)
    commit_sequence: int = Field(ge=0)
    hypotheses: dict[str, HypothesisState] = Field(default_factory=dict)
    child_slots: dict[str, ChildResultSlot] = Field(default_factory=dict)
    operations: dict[str, list[OperationRecord]] = Field(default_factory=dict)
    aggregation_commits: dict[str, int] = Field(default_factory=dict)
    decisions: dict[str, MainAgentDecision] = Field(default_factory=dict)
    artifacts: dict[str, ArtifactEnvelope] = Field(default_factory=dict)
    budget: RuntimeBudget
    stop_reason: str | None = None
    recovery_state: RunState | None = None
    resume_after_step: int | None = Field(default=None, ge=1)
    resume_count: int = Field(default=0, ge=0)
    recovery_events: list[dict[str, Any]] = Field(default_factory=list)
    resume_events: list[dict[str, Any]] = Field(default_factory=list)


_LOCK_REGISTRY: dict[Path, threading.RLock] = {}
_LOCK_REGISTRY_GUARD = threading.Lock()


def _lock_for(root: Path) -> threading.RLock:
    normalized = root.resolve()
    with _LOCK_REGISTRY_GUARD:
        return _LOCK_REGISTRY.setdefault(normalized, threading.RLock())


class ObjectiveRuntimeRepository:
    """PostgreSQL state and incremental events with immutable content files."""

    def __init__(self, root: str | Path, *, run_id: str) -> None:
        self.root = Path(root)
        self.run_id = run_id
        self._lock = _lock_for(self.root)
        self._store = PostgresRuntimeStore.open(self.root, run_id)

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        manifest: RunManifest,
        budget: RuntimeBudget,
        goal: GenerationGoal | None = None,
        storage_database=None,
        content_root=None,
    ) -> "ObjectiveRuntimeRepository":
        repo = cls(root, run_id=manifest.run_id)
        if repo._store is not None:
            raise FileExistsError("objective runtime already initialized")
        if storage_database is None:
            from .runtime_storage import database_for_workspace
            workspace = Path(root).parent.parent if Path(root).parent.name == "objective_runs" else Path(root)
            storage_database = database_for_workspace(workspace)
        repo._store = PostgresRuntimeStore.prepare(
            root, manifest.run_id, storage_database,
            content_root or Path(root).parent / "runtime_content",
        )
        snapshot = ObjectiveRuntimeSnapshot(
            run_id=manifest.run_id,
            manifest=manifest,
            goal=goal,
            run_state=RunState.CREATED,
            state_revision=1,
            commit_sequence=1,
            budget=budget,
        )
        repo._store.append(snapshot.model_dump(mode="json"), 0)
        return repo

    def load(self) -> ObjectiveRuntimeSnapshot:
        with self._lock:
            return self._load_latest()

    @property
    def pause_requested(self) -> bool:
        return (self.root / "objective_runtime" / "pause.requested").exists()

    def request_pause(self) -> None:
        with self._lock:
            snapshot = self._load_latest()
            if snapshot.run_state in {
                RunState.COMPLETED, RunState.COMPLETED_WITH_ZERO_QUESTIONS,
                RunState.PROVIDER_FAILED, RunState.VERSION_INCOMPATIBLE,
                RunState.CANCELLED, RunState.RECOVERY_REQUIRED,
            }:
                raise ValueError("run is not running")
            (self.root / "objective_runtime" / "pause.requested").touch()

    def pause_at_boundary(self) -> bool:
        with self._lock:
            if not self.pause_requested:
                return False
            snapshot = self._load_latest()
            self.set_run_state(
                expected_state_revision=snapshot.state_revision,
                target=RunState.RECOVERY_REQUIRED,
                stop_reason="user_paused",
            )
            (self.root / "objective_runtime" / "pause.requested").unlink(missing_ok=True)
            return True

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

    def ordered_decisions(self) -> list[MainAgentDecision]:
        snapshot = self.load()
        return [snapshot.decisions[key] for key in self._store.decision_ids()
                if key in snapshot.decisions]

    def pending_decision(self) -> MainAgentDecision | None:
        snapshot = self.load()
        if snapshot.run_state in {
            RunState.COMPLETED,
            RunState.COMPLETED_WITH_ZERO_QUESTIONS,
            RunState.PROVIDER_FAILED,
            RunState.VERSION_INCOMPATIBLE,
            RunState.CANCELLED,
        }:
            return None
        decisions = self.ordered_decisions()
        if not decisions:
            return None
        latest = decisions[-1]
        slots = [
            slot
            for slot in snapshot.child_slots.values()
            if slot.parent_decision_id == latest.decision_id
        ]
        if not slots or any(slot.state == "OPEN" for slot in slots):
            return latest
        return None

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

    def set_run_state(
        self,
        *,
        expected_state_revision: int,
        target: RunState,
        stop_reason: str | None = None,
        recovery_error: dict[str, Any] | None = None,
    ) -> ObjectiveRuntimeSnapshot:
        with self._lock:
            snapshot = self._load_latest()
            self._require_revision(snapshot, expected_state_revision)
            updates: dict[str, Any] = {
                "run_state": target,
                "stop_reason": stop_reason,
            }
            if (
                target is RunState.RECOVERY_REQUIRED
                and snapshot.run_state is not RunState.RECOVERY_REQUIRED
            ):
                updates["recovery_state"] = snapshot.run_state
                updates["resume_after_step"] = len(snapshot.decisions) + 1
                updates["recovery_events"] = [
                    *snapshot.recovery_events,
                    {
                        "at": utc_now().isoformat(),
                        "run_state": snapshot.run_state.value,
                        "resume_after_step": len(snapshot.decisions) + 1,
                        "stop_reason": stop_reason,
                        **(recovery_error or {}),
                    },
                ]
            elif target is not RunState.RECOVERY_REQUIRED:
                updates["recovery_state"] = None
                updates["resume_after_step"] = None
            updated = snapshot.model_copy(update=updates)
            return self._commit_next(snapshot, updated)

    def resume_from_recovery(self) -> ObjectiveRuntimeSnapshot:
        """Return a paused run to the state where its next decision can run."""
        with self._lock:
            snapshot = self._load_latest()
            if snapshot.run_state is not RunState.RECOVERY_REQUIRED:
                raise ValueError("run is not waiting for recovery")
            resumed_state = snapshot.recovery_state
            if resumed_state is None:
                raise CheckpointIntegrityError("paused runtime has no recovery state")
            updated = snapshot.model_copy(
                update={
                    "run_state": resumed_state,
                    "stop_reason": None,
                    "recovery_state": None,
                    "resume_after_step": None,
                    "resume_count": snapshot.resume_count + 1,
                }
            )
            return self._commit_next(snapshot, updated)

    def resume_stalled(
        self,
        *,
        reason: str,
    ) -> ObjectiveRuntimeSnapshot:
        with self._lock:
            snapshot = self._load_latest()
            if snapshot.run_state in {
                RunState.COMPLETED,
                RunState.COMPLETED_WITH_ZERO_QUESTIONS,
                RunState.PROVIDER_FAILED,
                RunState.RECOVERY_REQUIRED,
                RunState.VERSION_INCOMPATIBLE,
                RunState.CANCELLED,
            }:
                raise ValueError("run cannot resume from a stalled state")
            updated = snapshot.model_copy(
                update={
                    "resume_count": snapshot.resume_count + 1,
                    "resume_events": [
                        *snapshot.resume_events,
                        {
                            "at": utc_now().isoformat(),
                            "reason": reason,
                            "run_state": snapshot.run_state.value,
                        },
                    ],
                }
            )
            return self._commit_next(snapshot, updated)

    def record_decision(
        self,
        *,
        expected_state_revision: int,
        decision: MainAgentDecision,
    ) -> ObjectiveRuntimeSnapshot:
        with self._lock:
            snapshot = self._load_latest()
            if decision.decision_id in snapshot.decisions:
                existing = snapshot.decisions[decision.decision_id]
                if existing != decision:
                    raise OperationConflict(
                        "decision identity is already committed with other content"
                    )
                return snapshot
            self._require_revision(snapshot, expected_state_revision)
            decisions = dict(snapshot.decisions)
            decisions[decision.decision_id] = decision
            return self._commit_next(
                snapshot,
                snapshot.model_copy(update={"decisions": decisions}),
            )

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
        hypothesis_transition: tuple[str, list[HypothesisState]] | None = None,
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
            hypotheses = dict(snapshot.hypotheses)
            if hypothesis_transition:
                hid, states = hypothesis_transition
                for target in states:
                    hypotheses[hid] = transition_hypothesis(hypotheses[hid], target)
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
                        "hypotheses": hypotheses,
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
        if self._store is None:
            raise FileNotFoundError("objective runtime has no storage marker")
        try:
            return self._store.verify()
        except StorageIntegrityError as exc:
            raise CheckpointIntegrityError(str(exc)) from exc

    def _load_latest(self) -> ObjectiveRuntimeSnapshot:
        if self._store is None:
            raise FileNotFoundError("objective runtime has no storage marker")
        try:
            return ObjectiveRuntimeSnapshot.model_validate(self._store.load())
        except StorageIntegrityError as exc:
            raise CheckpointIntegrityError(str(exc)) from exc

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
        try:
            self._store.append(updated.model_dump(mode="json"), current.commit_sequence)
        except StorageConflict as exc:
            raise StaleStateRevision(str(exc)) from exc
        return updated

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
            if existing is not None:
                if _same_immutable_artifact(existing, artifact):
                    purpose = artifact.metadata.get("purpose")
                    if purpose:
                        purposes = set(
                            existing.metadata.get("purposes", [])
                        )
                        if existing.metadata.get("purpose"):
                            purposes.add(
                                str(existing.metadata["purpose"])
                            )
                        purposes.add(str(purpose))
                        merged[artifact.artifact_id] = existing.model_copy(
                            update={
                                "metadata": {
                                    **existing.metadata,
                                    "purposes": sorted(purposes),
                                }
                            }
                        )
                    continue
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


def _same_immutable_artifact(
    left: ArtifactEnvelope,
    right: ArtifactEnvelope,
) -> bool:
    if left.digest == right.digest:
        return True
    if (
        left.artifact_type == right.artifact_type == "evidence_material"
        and left.payload.get("content_hash")
        == right.payload.get("content_hash")
        and left.payload.get("provider_id")
        == right.payload.get("provider_id")
        and left.payload.get("source_record_id")
        == right.payload.get("source_record_id")
    ):
        return True
    if (
        left.artifact_type == right.artifact_type == "evidence_anchor"
        and left.payload.get("quote_hash") == right.payload.get("quote_hash")
        and left.payload.get("material_id") == right.payload.get("material_id")
    ):
        return True
    return False
