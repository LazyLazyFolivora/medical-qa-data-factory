"""Stable boundary between retrieval orchestration and evidence providers.

Providers return materials and locations. They never return verified facts or
gold answers. Terminal outcomes are intentionally split so orchestration can
distinguish an empty result from a failed or unknown external call.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from pydantic import Field, model_validator

from ..domain.common import ContractModel, UtcDateTime


class ProviderTerminalStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    EMPTY = "EMPTY"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class SideEffectClass(str, Enum):
    READ_ONLY = "READ_ONLY"
    IDEMPOTENT_WRITE = "IDEMPOTENT_WRITE"
    NON_IDEMPOTENT_WRITE = "NON_IDEMPOTENT_WRITE"


class EvidenceProviderCapability(ContractModel):
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    side_effect_class: SideEffectClass
    supports_idempotency: bool
    supports_status_lookup: bool
    safe_to_repeat: bool

    @model_validator(mode="after")
    def replay_claim_matches_effect(self) -> "EvidenceProviderCapability":
        can_repeat = (
            self.side_effect_class is SideEffectClass.READ_ONLY
            or self.supports_idempotency
        )
        if self.safe_to_repeat and not can_repeat:
            raise ValueError(
                "safe_to_repeat requires a read-only provider or idempotency"
            )
        return self

    @classmethod
    def read_only(cls, *, provider_id: str) -> "EvidenceProviderCapability":
        return cls(
            provider_id=provider_id,
            provider_version="v1",
            side_effect_class=SideEffectClass.READ_ONLY,
            supports_idempotency=True,
            supports_status_lookup=False,
            safe_to_repeat=True,
        )


class EvidenceProviderRequest(ContractModel):
    request_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    operation_key: str = Field(min_length=1)
    query: dict[str, Any] = Field(min_length=1)
    requested_at: UtcDateTime
    material_limit: int = Field(ge=1)
    snapshot_policy: str | None = None


class EvidenceProviderAttempt(ContractModel):
    attempt_id: str = Field(min_length=1)
    operation_key: str = Field(min_length=1)
    retry_ordinal: int = Field(ge=0)
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    started_at: UtcDateTime
    finished_at: UtcDateTime | None = None
    terminal_status: ProviderTerminalStatus
    error_category: str | None = None

    @model_validator(mode="after")
    def terminal_error_shape_is_valid(self) -> "EvidenceProviderAttempt":
        failed = self.terminal_status in {
            ProviderTerminalStatus.FAILED,
            ProviderTerminalStatus.UNKNOWN,
        }
        if failed and not self.error_category:
            raise ValueError(
                "error_category is required for FAILED or UNKNOWN attempts"
            )
        if not failed and self.error_category:
            raise ValueError(
                "error_category is only valid for FAILED or UNKNOWN attempts"
            )
        return self


class EvidenceProviderResult(ContractModel):
    request: EvidenceProviderRequest
    terminal_status: ProviderTerminalStatus
    material_ids: list[str] = Field(default_factory=list)
    anchor_ids: list[str] = Field(default_factory=list)
    materials: list[dict[str, Any]] = Field(default_factory=list)
    anchors: list[dict[str, Any]] = Field(default_factory=list)
    raw_content_by_material_id: dict[str, str] = Field(default_factory=dict)
    error_category: str | None = None
    provider_request_id: str | None = None
    snapshot_id: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def terminal_shape_is_coherent(self) -> "EvidenceProviderResult":
        failed = self.terminal_status in {
            ProviderTerminalStatus.FAILED,
            ProviderTerminalStatus.UNKNOWN,
        }
        if failed and not self.error_category:
            raise ValueError(
                "error_category is required for FAILED or UNKNOWN results"
            )
        if not failed and self.error_category:
            raise ValueError(
                "error_category is only valid for FAILED or UNKNOWN results"
            )
        if self.terminal_status is ProviderTerminalStatus.EMPTY and (
            self.material_ids or self.anchor_ids
        ):
            raise ValueError("an EMPTY result cannot contain materials or anchors")
        material_records = {
            str(item.get("material_id")) for item in self.materials
        }
        if self.materials and material_records != set(self.material_ids):
            raise ValueError("material_ids must match material records")
        anchor_records = {
            str(item.get("anchor_id")) for item in self.anchors
        }
        if self.anchors and anchor_records != set(self.anchor_ids):
            raise ValueError("anchor_ids must match anchor records")
        if self.materials and set(self.raw_content_by_material_id) != set(
            self.material_ids
        ):
            raise ValueError(
                "raw_content_by_material_id must cover every material"
            )
        return self


class EvidenceProviderPort(Protocol):
    """One provider adapter implementing the stable material-only boundary."""

    capability: EvidenceProviderCapability

    def retrieve(
        self, request: EvidenceProviderRequest
    ) -> EvidenceProviderResult:  # pragma: no cover - protocol
        ...

    def lookup_status(
        self, operation_key: str
    ) -> EvidenceProviderResult | None:  # pragma: no cover - protocol
        ...
