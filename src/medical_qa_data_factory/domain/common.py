"""Shared, versioned data-contract models."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any
from uuid import uuid4

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION = "0.1.0"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


UtcDateTime = Annotated[datetime, AfterValidator(normalize_utc)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GenerationMode(str, Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    HYBRID = "hybrid"


class ActorType(str, Enum):
    AGENT = "agent"
    SYSTEM = "system"
    HUMAN = "human"


class LinkStatus(str, Enum):
    EXACT = "exact"
    ALIAS = "alias"
    FUZZY = "fuzzy"
    MANUAL = "manual"
    UNLINKED = "unlinked"


class ValidationLevel(str, Enum):
    DRAFT = "draft"
    MACHINE_VALIDATED = "machine_validated"
    HUMAN_CONFIRMED = "human_confirmed"
    REJECTED = "rejected"


class ValidationStatus(str, Enum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    NOT_RUN = "not_run"


class ValidationSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class EntityRef(ContractModel):
    entity_ref_id: str = Field(min_length=1)
    raw_mention: str = Field(min_length=1)
    canonical_name: str | None
    canonical_id: str | None
    namespace: str | None
    entity_type: str | None
    aliases: list[str] = Field(default_factory=list)
    link_status: LinkStatus
    link_score: float | None = Field(default=None, ge=0, le=1)
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_linked_identity(self) -> "EntityRef":
        if self.link_status is not LinkStatus.UNLINKED:
            if not self.canonical_name or not self.canonical_id:
                raise ValueError(
                    "canonical_name and canonical_id are required for linked entities"
                )
        return self


class InvocationConfig(ContractModel):
    invocation_id: str = Field(min_length=1)
    provider: str | None
    model_id: str | None
    model_revision: str | None = None
    temperature: float | None = Field(default=None, ge=0)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning_effort: str | None = None
    prompt_template_version: str | None = None
    request_parameters: dict[str, Any] = Field(default_factory=dict)
    config_hash: str = Field(min_length=1)


class UsageScope(str, Enum):
    GENERATION = "generation"
    EPISODE = "episode"
    COUNTERFACTUAL = "counterfactual"


class MeasurementSource(str, Enum):
    PROVIDER = "provider"
    TOOL = "tool"
    DERIVED = "derived"
    ESTIMATED = "estimated"


class UsageRecord(ContractModel):
    usage_id: str = Field(min_length=1)
    usage_scope: UsageScope
    invocation_id: str | None
    tool_name: str = Field(min_length=1)
    tool_calls: int = Field(ge=0)
    kg_relation_accesses: int | None = Field(default=None, ge=0)
    documents_retrieved: int | None = Field(default=None, ge=0)
    documents_read: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    elapsed_ms: int | None = Field(default=None, ge=0)
    provider_reported_cost: float | None = Field(default=None, ge=0)
    currency: str | None = None
    measurement_source: MeasurementSource
    meter_version: str = Field(min_length=1)
    recorded_at: UtcDateTime

    @model_validator(mode="after")
    def validate_currency(self) -> "UsageRecord":
        if self.provider_reported_cost is not None and not self.currency:
            raise ValueError("currency is required with provider_reported_cost")
        return self


class EndpointKind(str, Enum):
    PYTHON = "python"
    HTTP = "http"
    MCP = "mcp"
    MOCK = "mock"


class ToolSnapshot(ContractModel):
    tool_name: str = Field(min_length=1)
    tool_version: str = Field(min_length=1)
    data_snapshot_id: str | None
    config_hash: str = Field(min_length=1)
    endpoint_kind: EndpointKind
    retrieved_at: UtcDateTime


class ValidationCheck(ContractModel):
    #: Every persisted record carries a schema version. It was optional in
    #: early artifacts, so a missing value reads as the current version
    #: rather than failing the load.
    schema_version: str = SCHEMA_VERSION
    check_id: str = Field(min_length=1)
    check_type: str = Field(min_length=1)
    status: ValidationStatus
    severity: ValidationSeverity
    message: str
    validator_version: str = Field(min_length=1)
    artifact_ids: list[str] = Field(default_factory=list)
    # Applicability. A check result is only valid for the question version it
    # actually evaluated, and only while the inputs it depended on are
    # unchanged. Without these two fields a stale ``passed`` record would
    # silently satisfy a gate for a question that has since changed.
    question_id: str | None = None
    evaluated_question_version: str | None = None
    #: Canonical digest of the input fields this check depended on. When the
    #: corresponding fields change, the digest changes and the old result no
    #: longer applies.
    input_digest: str | None = None
    evaluated_at: UtcDateTime | None = None


class ChangeRecord(ContractModel):
    change_id: str = Field(min_length=1)
    actor_type: ActorType
    operation: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    created_at: UtcDateTime
    parent_version: str | None = None
    new_version: str = Field(min_length=1)

