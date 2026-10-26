"""Run identity, evidence-mode authority, and the attempt ledger.

Step 1 of the real-generation-agent plan. Three things live here and nowhere
else:

1. ``RequiredEvidenceMode``: the server-authoritative evidence mode. It is set
   by ``RunPolicy`` at run creation and is copied onto the manifest, the
   bundle, the question and the coverage report. No model output, tool
   response or review command may change it.
2. ``GenerationPreparationRun`` / ``GenerationRunManifest``: the identity of a
   real run. A manifest is only frozen after a successful preflight, and an
   episode may only be created from a frozen manifest.
3. ``AttemptRecord``: the single source of truth for budget. Every physical
   model or tool attempt moves through ``reserved -> running -> terminal``,
   and the aggregate budget is derived from the latest version of every
   record, never from a separate counter that could drift.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field, model_validator

from ..domain.common import (
    ContractModel,
    ToolSnapshot,
    UsageRecord,
    UtcDateTime,
    new_id,
    utc_now,
)


#: Policy version for the run-identity contracts in this module.
RUN_IDENTITY_POLICY_VERSION = "run-identity-v1"


class RequiredEvidenceMode(str, Enum):
    """The evidence a run is required to produce.

    This is deliberately not the same field as the ablation-computed
    ``verified_route``. The generation requirement cannot be relaxed by what
    ablation later discovers: switching mode means a new run.
    """

    KG_ONLY = "kg_only"
    LITERATURE_ONLY = "literature_only"
    HYBRID = "hybrid"
    HYBRID_PER_HOP = "hybrid_per_hop"


class RunMode(str, Enum):
    RULE_BASED = "rule_based"
    FAKE = "fake"
    STRUCTURED = "structured"


class PreparationStatus(str, Enum):
    PREPARING = "preparing"
    READY = "ready"
    FAILED = "failed"
    UNKNOWN = "unknown"


class AttemptStatus(str, Enum):
    RESERVED = "reserved"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class AttemptOutcome(str, Enum):
    """Why an attempt ended, for reconciliation and audit."""

    SUCCESS = "success"
    EMPTY = "empty"
    TIMEOUT = "timeout"
    AUTH_ERROR = "auth_error"
    RATE_LIMITED = "rate_limited"
    PARSE_ERROR = "parse_error"
    PROVIDER_ERROR = "provider_error"
    POLICY_REJECTED = "policy_rejected"
    UNKNOWN = "unknown"


class GenerationPreparationRun(ContractModel):
    """Task-scoped preflight that must succeed before a run may be frozen.

    Provider preflight is an external call like any other: it reserves budget,
    runs, and reconciles. Only a ``ready`` preparation may produce a manifest.
    """

    schema_version: str = RUN_IDENTITY_POLICY_VERSION
    preparation_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    run_mode: RunMode
    request_digest: str = Field(min_length=1)
    #: The evidence mode this preparation is for. A different mode needs a new
    #: preparation, never an in-place change.
    required_evidence_mode: RequiredEvidenceMode
    budget: dict[str, Any] = Field(default_factory=dict)
    attempt_refs: list[dict[str, str]] = Field(default_factory=list)
    capability_snapshots: list[ToolSnapshot] = Field(default_factory=list)
    usage: list[UsageRecord] = Field(default_factory=list)
    status: PreparationStatus = PreparationStatus.PREPARING
    failure_reason: str | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime

    def is_ready(self) -> bool:
        return self.status is PreparationStatus.READY


class GenerationRunManifest(ContractModel):
    """Frozen identity of one real generation run."""

    schema_version: str = RUN_IDENTITY_POLICY_VERSION
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    preparation_id: str = Field(min_length=1)
    preflight_attempt_refs: list[dict[str, str]] = Field(default_factory=list)
    run_mode: RunMode
    run_policy_version: str = Field(min_length=1)
    #: Authoritative. Copied onto every downstream record and re-checked.
    required_evidence_mode: RequiredEvidenceMode
    request_digest: str = Field(min_length=1)
    kg_provider_id: str | None = None
    kg_snapshot_id: str | None = None
    rag_provider: str | None = None
    rag_capability_version: str | None = None
    #: Null means the provider could not pin an index snapshot. Such a run may
    #: not produce formal multi-source gold.
    rag_snapshot_id: str | None = None
    model_config_id: str | None = None
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    tool_snapshots: list[ToolSnapshot] = Field(default_factory=list)
    created_at: UtcDateTime


class AttemptRecord(ContractModel):
    """One physical external attempt, and the budget fact for it.

    ``attempt_id`` is unique per physical attempt; ``idempotency_key`` is
    stable per canonical request so a retry can be recognised. A retry is a
    *new* attempt with a *new* charge: replaying a completed attempt is a
    no-op, never a free second call.
    """

    schema_version: str = RUN_IDENTITY_POLICY_VERSION
    attempt_id: str = Field(min_length=1)
    attempt_version: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    #: ``task`` for preparation preflight, ``run`` for a frozen run's work.
    scope: str = Field(pattern="^(task|run)$")
    owner_id: str = Field(min_length=1)
    run_id: str | None = None
    episode_id: str | None = None
    tool_name: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    request_digest: str = Field(min_length=1)
    status: AttemptStatus = AttemptStatus.RESERVED
    outcome: AttemptOutcome | None = None
    #: Budget reserved before the call and actually charged after it. A
    #: reservation that is released must not leave a charge behind.
    reserved_units: float = Field(ge=0)
    charged_units: float | None = Field(default=None, ge=0)
    usage: list[UsageRecord] = Field(default_factory=list)
    provider_request_id: str | None = None
    response_digest: str | None = None
    error_code: str | None = None
    retryable: bool = False
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def terminal_attempt_has_an_outcome(self) -> "AttemptRecord":
        if self.status in (AttemptStatus.COMPLETED, AttemptStatus.FAILED):
            if self.outcome is None:
                raise ValueError("a terminal attempt requires an outcome")
            if self.charged_units is None:
                raise ValueError("a terminal attempt requires charged_units")
        if self.status in (AttemptStatus.RESERVED, AttemptStatus.RUNNING):
            if self.charged_units is not None:
                raise ValueError("an in-flight attempt must not be charged yet")
        return self

    def with_status(
        self,
        status: AttemptStatus,
        *,
        outcome: AttemptOutcome | None = None,
        charged_units: float | None = None,
        usage: list[UsageRecord] | None = None,
        provider_request_id: str | None = None,
        response_digest: str | None = None,
        error_code: str | None = None,
        retryable: bool = False,
    ) -> "AttemptRecord":
        """Return the next immutable version of this attempt."""
        return self.model_copy(
            update={
                "attempt_version": f"{self.attempt_id}.v{self._next_version()}",
                "status": status,
                "outcome": outcome,
                "charged_units": charged_units,
                "usage": usage if usage is not None else list(self.usage),
                "provider_request_id": (
                    provider_request_id
                    if provider_request_id is not None
                    else self.provider_request_id
                ),
                "response_digest": (
                    response_digest
                    if response_digest is not None
                    else self.response_digest
                ),
                "error_code": error_code if error_code is not None else self.error_code,
                "retryable": retryable,
                "updated_at": utc_now(),
            }
        )

    def _next_version(self) -> int:
        try:
            return int(self.attempt_version.rsplit(".v", 1)[1]) + 1
        except (IndexError, ValueError):  # pragma: no cover - defensive
            return 2


def new_attempt_id(prefix: str = "attempt") -> str:
    return new_id(prefix)