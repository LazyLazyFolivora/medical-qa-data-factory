"""WeKnora literature provider adapter.

The contract in this module was read from the WeKnora source, not guessed:

* ``POST /api/v1/knowledge-search`` returns ranked chunks without any LLM
  synthesis. The request takes ``query`` plus ``knowledge_base_id`` or
  ``knowledge_base_ids``; the response is ``{success, data: [SearchResult]}``.
* ``GET /api/v1/chunks/by-id/:id`` reads one chunk back by id, independently
  of the search that found it. The response is ``{success, data: Chunk}`` with
  ``start_at``/``end_at`` character offsets into the source document.
* Authentication is the ``X-API-Key`` header.
* A search result carries no content hash, so the hash is computed here from
  the retrieved text. A provider never vouches for its own content.

Because a search result is not a chunk record, the read-back is what makes a
result verifiable: the search gives the id, the read-back gives the text and
the offsets, and the two must agree.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ...domain.common import ToolSnapshot, UsageRecord, new_id, utc_now
from ..contracts import (
    LITERATURE_SCHEMA_VERSION,
    LiteratureCallStatus,
    LiteratureCapability,
    LiteratureChunkReadRequest,
    LiteratureChunkReadResult,
    LiteratureRetrievalRequest,
    LiteratureRetrievalResult,
    LiteratureSnapshot,
    RetrievedChunk,
)


#: The WeKnora capability version this adapter was written against.
WEKNORA_CAPABILITY_VERSION = "weknora-v0.10"


def content_hash(text: str) -> str:
    """Canonical hash of a chunk's text.

    The provider does not return a hash for search results, so the hash is
    computed from the text that was actually retrieved. Whitespace is
    normalised so a re-read of the same chunk hashes the same way.
    """
    normalised = " ".join(text.split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


class HttpTransport(Protocol):
    """The one thing this adapter needs from the network."""

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
        timeout: int,
    ) -> tuple[int, dict[str, str], str]: ...


def requests_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
    timeout: int,
) -> tuple[int, dict[str, str], str]:
    """Default transport, backed by ``requests``."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError(
            "the requests package is required for literature retrieval"
        ) from exc
    response = requests.request(
        method, url, headers=headers, json=body, timeout=timeout
    )
    return response.status_code, dict(response.headers), response.text


@dataclass
class WeKnoraConfig:
    """The provider configuration. It stores no secret."""

    base_url: str
    #: The name of the environment variable holding the API key.
    api_key_env: str = "WEKNORA_API_KEY"
    timeout_seconds: int = 60
    provider_name: str = "weknora"
    #: The knowledge bases this adapter may search.
    knowledge_base_ids: list[str] = field(default_factory=list)
    #: Legacy deployment label only; the current endpoints cannot enforce a snapshot.
    snapshot_id: str | None = None

    def api_key(self) -> str:
        import os

        value = os.environ.get(self.api_key_env)
        if not value:
            raise RuntimeError(
                f"environment variable {self.api_key_env!r} is not set; the "
                "provider configuration stores only the variable name"
            )
        return value

    def has_api_key(self) -> bool:
        import os

        return bool(os.environ.get(self.api_key_env))


@dataclass
class WeKnoraProvider:
    """Adapter over the WeKnora knowledge-search and chunk APIs."""

    config: WeKnoraConfig
    transport: HttpTransport = requests_transport
    calls: list[dict[str, Any]] = field(default_factory=list)

    # ---- preflight -----------------------------------------------------

    def preflight(self) -> LiteratureCapability:
        """Report what the provider can do, without changing any state.

        This is the adapter contract, not a live health check. These endpoints
        do not select or attest an immutable corpus version. A configuration
        label must therefore never enable formal pinned-snapshot evidence.
        """
        snapshot = self._snapshot()
        capability = LiteratureCapability(
            provider=self.config.provider_name,
            capability_version=WEKNORA_CAPABILITY_VERSION,
            supports_search=True,
            supports_read_chunk=True,
            supports_pinned_snapshot=False,
            supports_metadata_filters=False,
            supports_source_offsets=True,
            max_limit=100,
            snapshot=snapshot,
            tool_snapshot=ToolSnapshot(
                tool_name=f"literature_{self.config.provider_name}",
                tool_version=WEKNORA_CAPABILITY_VERSION,
                data_snapshot_id=None,
                config_hash=content_hash(self.config.base_url),
                endpoint_kind="http",
                retrieved_at=utc_now(),
            ),
            notes=(
                "WeKnora search returns ranked chunks without LLM synthesis; "
                "chunk read-back is available by chunk id. Snapshot selection is "
                "not supported; configured labels do not pin the corpus. "
                "Results are development/context candidates, not formal gold."
            ),
        )
        return capability

    # ---- search ---------------------------------------------------------

    def search(
        self, request: LiteratureRetrievalRequest
    ) -> LiteratureRetrievalResult:
        """Search one knowledge base for chunks matching a need."""
        started = time.monotonic()
        if request.expected_snapshot_id is not None:
            return LiteratureRetrievalResult(
                request_id=request.request_id,
                need_id=request.need_id,
                status=LiteratureCallStatus.PROVIDER_ERROR,
                snapshot=self._snapshot(),
                error_code="snapshot_unavailable",
                message="WeKnora cannot enforce the requested corpus snapshot",
            )
        url = f"{self.config.base_url.rstrip('/')}/api/v1/knowledge-search"
        body = {
            "query": request.query,
            "knowledge_base_ids": list(request.knowledge_base_ids),
        }
        status, headers, text = self._call(
            "POST", url, body, request.expected_snapshot_id
        )
        elapsed = int((time.monotonic() - started) * 1000)
        if status == 401 or status == 403:
            return self._failed_search(
                request, LiteratureCallStatus.AUTH_ERROR, status, elapsed,
                "the provider rejected the credentials",
            )
        if status == 429:
            return self._failed_search(
                request, LiteratureCallStatus.RATE_LIMITED, status, elapsed,
                "the provider rate limited the call",
            )
        if status >= 400:
            return self._failed_search(
                request, LiteratureCallStatus.PROVIDER_ERROR, status, elapsed,
                f"the provider returned HTTP {status}",
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return self._failed_search(
                request, LiteratureCallStatus.PARSE_ERROR, status, elapsed,
                "the provider response is not valid JSON",
            )
        if not isinstance(payload, dict):
            return self._failed_search(
                request, LiteratureCallStatus.PARSE_ERROR, status, elapsed,
                "the provider response is not a JSON object",
            )
        if payload.get("success") is not True:
            return self._failed_search(
                request, LiteratureCallStatus.PROVIDER_ERROR, status, elapsed,
                "the provider did not confirm a successful search",
                error_code="provider_rejected",
            )
        rows = payload.get("data")
        if not isinstance(rows, list):
            return self._failed_search(
                request, LiteratureCallStatus.PARSE_ERROR, status, elapsed,
                "the provider response has no data array",
            )
        try:
            if any(not isinstance(row, dict) for row in rows):
                raise ValueError("invalid chunk row")
            chunks = [self._to_chunk(row) for row in rows]
            if any(item is None for item in chunks):
                raise ValueError("invalid chunk identity or text")
        except (TypeError, ValueError):
            return self._failed_search(
                request, LiteratureCallStatus.PARSE_ERROR, status, elapsed,
                "the provider returned malformed chunks, not an empty result",
                error_code="invalid_chunks",
            )
        chunks = chunks[: request.limit]
        snapshot = self._snapshot()
        return LiteratureRetrievalResult(
            request_id=request.request_id,
            need_id=request.need_id,
            status=(
                LiteratureCallStatus.SUCCESS
                if chunks
                else LiteratureCallStatus.EMPTY
            ),
            chunks=chunks,
            snapshot=snapshot,
            trace_id=self._trace_id(headers),
            provider_request_id=self._request_id(headers),
            usage=self._usage(request, elapsed, len(chunks)),
            elapsed_ms=elapsed,
            message="" if chunks else "the corpus returned no chunk for this need",
        )

    # ---- read back ------------------------------------------------------

    def read_chunk(
        self, request: LiteratureChunkReadRequest
    ) -> LiteratureChunkReadResult:
        """Read one chunk back by id, independently of the search."""
        started = time.monotonic()
        if request.expected_snapshot_id is not None:
            return LiteratureChunkReadResult(
                request_id=request.request_id,
                chunk_id=request.chunk_id,
                status=LiteratureCallStatus.PROVIDER_ERROR,
                error_code="snapshot_unavailable",
                message="WeKnora cannot enforce the requested corpus snapshot",
            )
        url = (
            f"{self.config.base_url.rstrip('/')}"
            f"/api/v1/chunks/by-id/{request.chunk_id}"
        )
        status, headers, text = self._call(
            "GET", url, None, request.expected_snapshot_id
        )
        elapsed = int((time.monotonic() - started) * 1000)
        if status == 401 or status == 403:
            return self._failed_read(
                request, LiteratureCallStatus.AUTH_ERROR, status, elapsed,
                "the provider rejected the credentials",
            )
        if status == 404:
            return self._failed_read(
                request, LiteratureCallStatus.PROVIDER_ERROR, status, elapsed,
                "the chunk does not exist",
            )
        if status == 429:
            return self._failed_read(
                request, LiteratureCallStatus.RATE_LIMITED, status, elapsed,
                "the provider rate limited the call",
            )
        if status >= 400:
            return self._failed_read(
                request, LiteratureCallStatus.PROVIDER_ERROR, status, elapsed,
                f"the provider returned HTTP {status}",
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return self._failed_read(
                request, LiteratureCallStatus.PARSE_ERROR, status, elapsed,
                "the provider response is not valid JSON",
            )
        if isinstance(payload, dict) and payload.get("success") is not True:
            return self._failed_read(
                request, LiteratureCallStatus.PROVIDER_ERROR, status, elapsed,
                "the provider did not confirm a successful read-back",
                error_code="provider_rejected",
            )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return self._failed_read(
                request, LiteratureCallStatus.PARSE_ERROR, status, elapsed,
                "the provider response has no chunk data",
            )
        return LiteratureChunkReadResult(
            request_id=request.request_id,
            chunk_id=request.chunk_id,
            status=LiteratureCallStatus.SUCCESS,
            document_id=str(data.get("knowledge_id") or "") or None,
            content=str(data.get("content") or ""),
            chunk_index=data.get("chunk_index"),
            start_at=data.get("start_at"),
            end_at=data.get("end_at"),
            snapshot=self._snapshot(),
            trace_id=self._trace_id(headers),
            provider_request_id=self._request_id(headers),
            usage=self._usage_read(request, elapsed),
            elapsed_ms=elapsed,
        )

    # ---- internals ------------------------------------------------------

    def _call(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None,
        expected_snapshot_id: str | None,
    ) -> tuple[int, dict[str, str], str]:
        headers = {
            "X-API-Key": self.config.api_key(),
            "Content-Type": "application/json",
        }
        record: dict[str, Any] = {
            "method": method,
            "url": url,
            "expected_snapshot_id": expected_snapshot_id,
        }
        try:
            status, response_headers, text = self.transport(
                method, url, headers, body, self.config.timeout_seconds
            )
        except TimeoutError as exc:
            record["status"] = "timeout"
            self.calls.append(record)
            raise LiteratureTimeoutError(
                f"the literature provider timed out after "
                f"{self.config.timeout_seconds}s"
            ) from exc
        record["status"] = status
        self.calls.append(record)
        return status, response_headers, text

    def _snapshot(self) -> LiteratureSnapshot:
        return LiteratureSnapshot(
            provider=self.config.provider_name,
            capability_version=WEKNORA_CAPABILITY_VERSION,
            snapshot_id=None,
            knowledge_base_ids=list(self.config.knowledge_base_ids),
            pinned=False,
            retrieved_at=utc_now(),
        )

    def _to_chunk(self, row: dict[str, Any]) -> RetrievedChunk | None:
        chunk_id = str(row.get("id") or "")
        content = row.get("content")
        if not chunk_id or not isinstance(content, str) or not content:
            return None
        metadata = row.get("metadata")
        return RetrievedChunk(
            chunk_id=chunk_id,
            document_id=str(row.get("knowledge_id") or ""),
            content=content,
            title=str(row.get("knowledge_title") or ""),
            chunk_index=int(row.get("chunk_index") or 0),
            start_at=int(row.get("start_at") or 0),
            end_at=int(row.get("end_at") or 0),
            score=row.get("score"),
            match_type=self._match_type(row.get("match_type")),
            chunk_type=row.get("chunk_type"),
            source_filename=row.get("knowledge_filename"),
            source_kind=row.get("knowledge_source"),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )

    def _match_type(self, value: Any) -> str | None:
        """Map the server's numeric match type onto a readable name.

        The numeric values are the wire contract and mirror the server's
        internal iota order, so they must not be reordered here.
        """
        names = {
            0: "vector",
            1: "keyword",
            2: "nearby_chunk",
            3: "history",
            4: "parent_chunk",
            5: "relation_chunk",
            6: "graph",
            7: "web_search",
            8: "direct_load",
            9: "data_analysis",
        }
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return names.get(value, f"match_type_{value}")
        if isinstance(value, str):
            return value
        return None

    def _trace_id(self, headers: dict[str, str]) -> str | None:
        for name in ("x-trace-id", "x-request-id", "traceparent"):
            value = headers.get(name) or headers.get(name.title())
            if value:
                return str(value)
        return None

    def _request_id(self, headers: dict[str, str]) -> str | None:
        value = headers.get("x-request-id") or headers.get("X-Request-Id")
        return str(value) if value else None

    def _usage(
        self, request: LiteratureRetrievalRequest, elapsed: int, chunks: int
    ) -> UsageRecord:
        return UsageRecord(
            usage_id=new_id("usage"),
            usage_scope="generation",
            invocation_id=request.request_id,
            tool_name=f"literature_{self.config.provider_name}",
            tool_calls=1,
            documents_retrieved=chunks,
            elapsed_ms=elapsed,
            measurement_source="tool",
            meter_version="literature-usage-v1",
            recorded_at=utc_now(),
        )

    def _usage_read(
        self, request: LiteratureChunkReadRequest, elapsed: int
    ) -> UsageRecord:
        return UsageRecord(
            usage_id=new_id("usage"),
            usage_scope="generation",
            invocation_id=request.request_id,
            tool_name=f"literature_{self.config.provider_name}",
            tool_calls=1,
            documents_read=1,
            elapsed_ms=elapsed,
            measurement_source="tool",
            meter_version="literature-usage-v1",
            recorded_at=utc_now(),
        )

    def _failed_search(
        self,
        request: LiteratureRetrievalRequest,
        status: LiteratureCallStatus,
        http_status: int,
        elapsed: int,
        message: str,
        *,
        error_code: str | None = None,
    ) -> LiteratureRetrievalResult:
        return LiteratureRetrievalResult(
            request_id=request.request_id,
            need_id=request.need_id,
            status=status,
            snapshot=self._snapshot(),
            error_code=error_code or f"http_{http_status}",
            message=message,
            elapsed_ms=elapsed,
        )

    def _failed_read(
        self,
        request: LiteratureChunkReadRequest,
        status: LiteratureCallStatus,
        http_status: int,
        elapsed: int,
        message: str,
        *,
        error_code: str | None = None,
    ) -> LiteratureChunkReadResult:
        return LiteratureChunkReadResult(
            request_id=request.request_id,
            chunk_id=request.chunk_id,
            status=status,
            error_code=error_code or f"http_{http_status}",
            message=message,
            elapsed_ms=elapsed,
        )


class LiteratureTimeoutError(RuntimeError):
    """The provider did not answer in time."""


__all__ = [
    "LiteratureTimeoutError",
    "WEKNORA_CAPABILITY_VERSION",
    "WeKnoraConfig",
    "WeKnoraProvider",
    "content_hash",
    "requests_transport",
]