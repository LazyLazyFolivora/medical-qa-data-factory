"""Literature retrieval contracts.

Step 4 of the real-generation-agent plan. The coarse ``LiteratureTool.collect``
interface could not express a per-hop information need, and it had no notion of
a pinned index snapshot or an independent read-back. These contracts replace it
for real generation; the old interface stays as a legacy/demo adapter.

The three capabilities are deliberately separate:

* ``preflight`` - what the provider can do, and which index snapshot it can pin.
* ``search`` - ranked chunks for one information need, inside one snapshot.
* ``read_chunk`` - fetch one chunk back by id, independently of the search.

A gold text result is only valid if it survives ``read_chunk`` against the
same snapshot with the same content hash. A provider that cannot pin a
snapshot, or cannot read a chunk back, can only produce development candidates.
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
)


#: Contract version for the literature layer.
LITERATURE_SCHEMA_VERSION = "literature-v1"


class LiteratureCallStatus(str, Enum):
    """The outcome of one retrieval call.

    ``EMPTY`` is a real answer: the corpus does not contain this. It must
    never be turned into "the relation does not hold".
    """

    SUCCESS = "success"
    EMPTY = "empty"
    TIMEOUT = "timeout"
    AUTH_ERROR = "auth_error"
    RATE_LIMITED = "rate_limited"
    PARSE_ERROR = "parse_error"
    PROVIDER_ERROR = "provider_error"


class LiteratureEvidenceNeed(ContractModel):
    """One information need, derived from a path hop or a claim gap.

    The need is built by code, never by the model: the model may only suggest
    the wording of a query, never the need id, the hop, the entity identity
    or the budget.
    """

    schema_version: str = LITERATURE_SCHEMA_VERSION
    need_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    path_id: str | None = None
    hop_position: int | None = Field(default=None, ge=0)
    kg_evidence_id: str | None = None
    subject_entity_id: str = Field(min_length=1)
    #: The canonical name. A passage writes a name, never an id, so the name
    #: is the term that a support judgement has to match.
    subject_name: str = Field(min_length=1)
    subject_aliases: list[str] = Field(default_factory=list)
    canonical_relation: str = Field(min_length=1)
    relation_phrases: list[str] = Field(default_factory=list)
    object_entity_id: str = Field(min_length=1)
    object_name: str = Field(min_length=1)
    object_aliases: list[str] = Field(default_factory=list)
    information_need: str = Field(min_length=1)
    required_support_count: int = Field(default=1, ge=1, le=10)
    filters: dict[str, Any] = Field(default_factory=dict)
    #: Digest of the inputs this need was built from. A change invalidates
    #: every result that was collected for it.
    dependency_digest: str = Field(min_length=1)


class LiteratureRetrievalRequest(ContractModel):
    """One sealed retrieval request."""

    schema_version: str = LITERATURE_SCHEMA_VERSION
    request_id: str = Field(min_length=1)
    sealed_action_id: str | None = None
    attempt_id: str | None = None
    need_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=8, ge=1, le=100)
    #: The snapshot the caller expects. A provider that returns a different
    #: snapshot has drifted, and the caller must stop the run.
    expected_snapshot_id: str | None = None
    provider_capability_version: str | None = None

    @model_validator(mode="after")
    def a_request_needs_a_corpus(self) -> "LiteratureRetrievalRequest":
        if not self.knowledge_base_ids:
            raise ValueError("a retrieval request must name at least one knowledge base")
        return self


class LiteratureSnapshot(ContractModel):
    """The index or corpus version a call was served from."""

    schema_version: str = LITERATURE_SCHEMA_VERSION
    provider: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    #: The pinned corpus or index version. Null means the provider cannot pin
    #: one, and no formal gold may be produced from this provider.
    snapshot_id: str | None = None
    knowledge_base_ids: list[str] = Field(default_factory=list)
    pinned: bool = False
    retrieved_at: UtcDateTime

    @model_validator(mode="after")
    def a_pinned_snapshot_has_an_id(self) -> "LiteratureSnapshot":
        if self.pinned and not self.snapshot_id:
            raise ValueError("a pinned snapshot must carry a snapshot id")
        return self


class RetrievedChunk(ContractModel):
    """One chunk returned by a search."""

    chunk_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    title: str = Field(min_length=1)
    chunk_index: int = Field(default=0, ge=0)
    #: Character offsets inside the source document.
    start_at: int = Field(default=0, ge=0)
    end_at: int = Field(default=0, ge=0)
    score: float | None = None
    match_type: str | None = None
    chunk_type: str | None = None
    source_filename: str | None = None
    source_kind: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LiteratureRetrievalResult(ContractModel):
    """The outcome of one search."""

    schema_version: str = LITERATURE_SCHEMA_VERSION
    request_id: str = Field(min_length=1)
    need_id: str = Field(min_length=1)
    status: LiteratureCallStatus
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    snapshot: LiteratureSnapshot
    trace_id: str | None = None
    provider_request_id: str | None = None
    usage: UsageRecord | None = None
    error_code: str | None = None
    message: str = ""
    elapsed_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def only_a_success_carries_chunks(self) -> "LiteratureRetrievalResult":
        if self.status is not LiteratureCallStatus.SUCCESS and self.chunks:
            raise ValueError("a non-success result must not carry chunks")
        return self


class LiteratureChunkReadRequest(ContractModel):
    """An independent read-back of one chunk."""

    schema_version: str = LITERATURE_SCHEMA_VERSION
    request_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    document_id: str | None = None
    expected_snapshot_id: str | None = None
    provider_capability_version: str | None = None


class LiteratureChunkReadResult(ContractModel):
    """The outcome of an independent read-back.

    The content hash is computed by the caller, not taken from the provider,
    so a provider cannot vouch for its own text.
    """

    schema_version: str = LITERATURE_SCHEMA_VERSION
    request_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    status: LiteratureCallStatus
    document_id: str | None = None
    content: str | None = None
    chunk_index: int | None = None
    start_at: int | None = None
    end_at: int | None = None
    snapshot: LiteratureSnapshot | None = None
    trace_id: str | None = None
    provider_request_id: str | None = None
    usage: UsageRecord | None = None
    error_code: str | None = None
    message: str = ""
    elapsed_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def only_a_success_carries_content(self) -> "LiteratureChunkReadResult":
        if self.status is LiteratureCallStatus.SUCCESS and self.content is None:
            raise ValueError("a successful read must carry the content")
        return self


class LiteratureCapability(ContractModel):
    """What the provider says it can do.

    A provider that cannot pin a snapshot, or cannot read a chunk back, may
    only produce development candidates.
    """

    schema_version: str = LITERATURE_SCHEMA_VERSION
    provider: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    supports_search: bool = False
    supports_read_chunk: bool = False
    supports_pinned_snapshot: bool = False
    supports_metadata_filters: bool = False
    supports_source_offsets: bool = False
    max_limit: int = Field(default=8, ge=1, le=1000)
    snapshot: LiteratureSnapshot | None = None
    tool_snapshot: ToolSnapshot | None = None
    notes: str = ""


class LiteratureProviderError(RuntimeError):
    """A retrieval call failed with a stable code."""

    def __init__(
        self,
        status: LiteratureCallStatus,
        message: str,
        *,
        error_code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.error_code = error_code
        self.retryable = retryable