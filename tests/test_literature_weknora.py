"""The WeKnora adapter, exercised against a local HTTP server.

The request and response shapes in this file are the ones the WeKnora source
actually uses: ``POST /api/v1/knowledge-search`` returns
``{success, data: [SearchResult]}`` and ``GET /api/v1/chunks/by-id/:id``
returns ``{success, data: Chunk}``.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from medical_qa_data_factory.literature.contracts import (
    LiteratureCallStatus,
    LiteratureChunkReadRequest,
    LiteratureRetrievalRequest,
)
from medical_qa_data_factory.literature.providers.weknora import (
    WeKnoraConfig,
    WeKnoraProvider,
    content_hash,
)


SEARCH_ROW = {
    "id": "chunk-00000001",
    "content": "Aspirin inhibits cyclooxygenase and reduces prostaglandin synthesis.",
    "knowledge_id": "knowledge-00000001",
    "chunk_index": 4,
    "knowledge_title": "Aspirin pharmacology",
    "start_at": 120,
    "end_at": 208,
    "seq": 1,
    "score": 0.82,
    "match_type": 0,
    "chunk_type": "text",
    "image_info": "",
    "metadata": {},
    "knowledge_filename": "aspirin.pdf",
    "knowledge_source": "file",
}

CHUNK_ROW = {
    "id": "chunk-00000001",
    "knowledge_id": "knowledge-00000001",
    "knowledge_base_id": "kb-00000001",
    "content": "Aspirin inhibits cyclooxygenase and reduces prostaglandin synthesis.",
    "chunk_index": 4,
    "start_at": 120,
    "end_at": 208,
    "chunk_type": "text",
    "content_hash": "",
    "created_at": "2025-08-12T11:52:36.168632+08:00",
    "updated_at": "2025-08-12T11:52:53.376871+08:00",
}


class _Server:
    """A local server that replays scripted responses per path."""

    def __init__(self, responses: dict[str, list[dict[str, Any]]]) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _respond(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                path = self.path.split("?")[0]
                outer.requests.append(
                    {
                        "method": method,
                        "path": path,
                        "body": json.loads(raw) if raw else None,
                    }
                )
                outer.headers.append(dict(self.headers))
                key = "search" if "knowledge-search" in path else "chunk"
                queue = outer.responses.get(key, [])
                payload = queue.pop(0) if queue else {"status": 500, "body": {}}
                status = int(payload.get("status", 200))
                body = payload.get("body", {})
                if not isinstance(body, str):
                    body = json.dumps(body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body.encode("utf-8"))))
                self.send_header("x-request-id", "req-77")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            def do_POST(self) -> None:  # noqa: N802
                self._respond("POST")

            def do_GET(self) -> None:  # noqa: N802
                self._respond("GET")

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"


def _config(url: str, **overrides: Any) -> WeKnoraConfig:
    payload: dict[str, Any] = {
        "base_url": url,
        "api_key_env": "WEKNORA_TEST_KEY",
        "timeout_seconds": 10,
        "knowledge_base_ids": ["kb-00000001"],
    }
    payload.update(overrides)
    return WeKnoraConfig(**payload)


def _request(**overrides: Any) -> LiteratureRetrievalRequest:
    payload: dict[str, Any] = {
        "request_id": "req-1",
        "need_id": "need-1",
        "query": "aspirin mechanism of action",
        "knowledge_base_ids": ["kb-00000001"],
        "limit": 8,
    }
    payload.update(overrides)
    return LiteratureRetrievalRequest(**payload)


@pytest.fixture(autouse=True)
def _key(monkeypatch) -> None:
    monkeypatch.setenv("WEKNORA_TEST_KEY", "test-key")


# ---- preflight -----------------------------------------------------------


def test_preflight_reports_the_capabilities() -> None:
    provider = WeKnoraProvider(config=_config("http://127.0.0.1:1"))
    capability = provider.preflight()
    assert capability.provider == "weknora"
    assert capability.supports_search
    assert capability.supports_read_chunk
    assert capability.supports_source_offsets
    assert capability.tool_snapshot is not None
    assert capability.tool_snapshot.endpoint_kind == "http"


def test_preflight_reports_that_no_snapshot_is_pinned() -> None:
    provider = WeKnoraProvider(config=_config("http://127.0.0.1:1"))
    capability = provider.preflight()
    assert capability.supports_pinned_snapshot is False
    assert capability.snapshot is not None
    assert capability.snapshot.pinned is False
    assert capability.snapshot.snapshot_id is None


def test_a_deployment_label_does_not_claim_a_pinned_snapshot() -> None:
    provider = WeKnoraProvider(
        config=_config("http://127.0.0.1:1", snapshot_id="corpus-2026-10")
    )
    capability = provider.preflight()
    assert capability.supports_pinned_snapshot is False
    assert capability.snapshot is not None
    assert capability.snapshot.snapshot_id is None
    assert capability.snapshot.pinned is False
    assert capability.tool_snapshot.data_snapshot_id is None


# ---- search --------------------------------------------------------------


def test_a_search_returns_the_ranked_chunks() -> None:
    with _Server(
        {"search": [{"status": 200, "body": {"success": True, "data": [SEARCH_ROW]}}]}
    ) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        result = provider.search(_request())
    assert result.status is LiteratureCallStatus.SUCCESS
    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.chunk_id == "chunk-00000001"
    assert chunk.document_id == "knowledge-00000001"
    assert chunk.start_at == 120
    assert chunk.end_at == 208
    assert chunk.score == 0.82
    assert chunk.source_filename == "aspirin.pdf"


def test_the_search_request_names_the_knowledge_base_and_the_query() -> None:
    with _Server(
        {"search": [{"status": 200, "body": {"success": True, "data": [SEARCH_ROW]}}]}
    ) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        provider.search(_request())
    body = server.requests[0]["body"]
    assert body["query"] == "aspirin mechanism of action"
    assert body["knowledge_base_ids"] == ["kb-00000001"]


def test_the_api_key_is_sent_as_the_x_api_key_header() -> None:
    with _Server(
        {"search": [{"status": 200, "body": {"success": True, "data": [SEARCH_ROW]}}]}
    ) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        provider.search(_request())
    assert server.headers[0].get("X-API-Key") == "test-key"


def test_an_empty_result_is_not_a_failure() -> None:
    with _Server(
        {"search": [{"status": 200, "body": {"success": True, "data": []}}]}
    ) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        result = provider.search(_request())
    assert result.status is LiteratureCallStatus.EMPTY
    assert result.chunks == []


def test_a_401_is_an_auth_error() -> None:
    with _Server({"search": [{"status": 401, "body": {}}]}) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        result = provider.search(_request())
    assert result.status is LiteratureCallStatus.AUTH_ERROR
    assert result.error_code == "http_401"


def test_a_429_is_a_rate_limit() -> None:
    with _Server({"search": [{"status": 429, "body": {}}]}) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        result = provider.search(_request())
    assert result.status is LiteratureCallStatus.RATE_LIMITED


def test_a_500_is_a_provider_error() -> None:
    with _Server({"search": [{"status": 500, "body": {}}]}) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        result = provider.search(_request())
    assert result.status is LiteratureCallStatus.PROVIDER_ERROR


def test_an_unparseable_response_is_a_parse_error() -> None:
    with _Server({"search": [{"status": 200, "body": "not json"}]}) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        result = provider.search(_request())
    assert result.status is LiteratureCallStatus.PARSE_ERROR


def test_a_response_without_a_data_array_is_a_parse_error() -> None:
    with _Server({"search": [{"status": 200, "body": {"success": True}}]}) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        result = provider.search(_request())
    assert result.status is LiteratureCallStatus.PARSE_ERROR


def test_the_limit_is_applied_to_the_results() -> None:
    rows = [dict(SEARCH_ROW, id=f"chunk-{index}") for index in range(5)]
    with _Server(
        {"search": [{"status": 200, "body": {"success": True, "data": rows}}]}
    ) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        result = provider.search(_request(limit=2))
    assert len(result.chunks) == 2


def test_the_numeric_match_type_is_mapped_to_a_name() -> None:
    with _Server(
        {"search": [{"status": 200, "body": {"success": True, "data": [SEARCH_ROW]}}]}
    ) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        result = provider.search(_request())
    assert result.chunks[0].match_type == "vector"


# ---- read back -----------------------------------------------------------


def test_a_chunk_is_read_back_by_id() -> None:
    with _Server(
        {"chunk": [{"status": 200, "body": {"success": True, "data": CHUNK_ROW}}]}
    ) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        result = provider.read_chunk(
            LiteratureChunkReadRequest(
                request_id="read-1", chunk_id="chunk-00000001"
            )
        )
    assert result.status is LiteratureCallStatus.SUCCESS
    assert result.chunk_id == "chunk-00000001"
    assert result.document_id == "knowledge-00000001"
    assert result.start_at == 120
    assert result.end_at == 208
    assert "Aspirin inhibits" in (result.content or "")


def test_the_read_back_uses_the_by_id_path() -> None:
    with _Server(
        {"chunk": [{"status": 200, "body": {"success": True, "data": CHUNK_ROW}}]}
    ) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        provider.read_chunk(
            LiteratureChunkReadRequest(
                request_id="read-1", chunk_id="chunk-00000001"
            )
        )
    assert server.requests[0]["path"] == "/api/v1/chunks/by-id/chunk-00000001"


def test_a_missing_chunk_is_a_provider_error() -> None:
    with _Server({"chunk": [{"status": 404, "body": {}}]}) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        result = provider.read_chunk(
            LiteratureChunkReadRequest(
                request_id="read-1", chunk_id="chunk-missing"
            )
        )
    assert result.status is LiteratureCallStatus.PROVIDER_ERROR
    assert result.error_code == "http_404"


# ---- the round trip that makes a result verifiable ----------------------


def test_a_searched_chunk_can_be_read_back_and_hashes_the_same() -> None:
    """The property that makes a search result usable as gold evidence."""
    with _Server(
        {
            "search": [
                {
                    "status": 200,
                    "body": {"success": True, "data": [SEARCH_ROW]},
                }
            ],
            "chunk": [
                {"status": 200, "body": {"success": True, "data": CHUNK_ROW}}
            ],
        }
    ) as server:
        provider = WeKnoraProvider(config=_config(server.url))
        searched = provider.search(_request())
        chunk = searched.chunks[0]
        read_back = provider.read_chunk(
            LiteratureChunkReadRequest(
                request_id="read-1",
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                expected_snapshot_id=searched.snapshot.snapshot_id,
            )
        )
    assert read_back.status is LiteratureCallStatus.SUCCESS
    # The same text must hash the same way on both paths.
    assert content_hash(chunk.content) == content_hash(read_back.content or "")
    # And the offsets must agree.
    assert read_back.start_at == chunk.start_at
    assert read_back.end_at == chunk.end_at


def test_the_content_hash_normalises_whitespace() -> None:
    assert content_hash("a  b\nc") == content_hash("a b c")


def test_the_content_hash_is_stable() -> None:
    assert content_hash("aspirin") == content_hash("aspirin")
    assert content_hash("aspirin") != content_hash("ibuprofen")


# ---- configuration -------------------------------------------------------


def test_the_configuration_stores_no_secret() -> None:
    config = _config("http://127.0.0.1:1")
    assert "test-key" not in repr(config)
    assert config.api_key_env == "WEKNORA_TEST_KEY"


def test_a_missing_api_key_fails_before_the_call(monkeypatch) -> None:
    monkeypatch.delenv("WEKNORA_TEST_KEY", raising=False)
    provider = WeKnoraProvider(config=_config("http://127.0.0.1:1"))
    with pytest.raises(RuntimeError, match="WEKNORA_TEST_KEY"):
        provider.search(_request())
    assert provider.calls == []


def test_the_request_requires_a_knowledge_base() -> None:
    with pytest.raises(ValueError):
        LiteratureRetrievalRequest(
            request_id="r", need_id="n", query="q"
        )

def test_configured_label_keeps_ordinary_search_and_readback_available() -> None:
    with _Server({
        "search": [{"status": 200, "body": {"success": True, "data": [SEARCH_ROW]}}],
        "chunk": [{"status": 200, "body": {"success": True, "data": CHUNK_ROW}}],
    }) as server:
        provider = WeKnoraProvider(config=_config(server.url, snapshot_id="unverified-label"))
        searched = provider.search(_request())
        read_back = provider.read_chunk(LiteratureChunkReadRequest(
            request_id="ordinary-read", chunk_id=searched.chunks[0].chunk_id,
        ))
    assert searched.status is LiteratureCallStatus.SUCCESS
    assert read_back.status is LiteratureCallStatus.SUCCESS
    assert searched.snapshot.pinned is False and searched.snapshot.snapshot_id is None
    assert read_back.snapshot.pinned is False and read_back.snapshot.snapshot_id is None


def test_fixed_snapshot_requests_fail_before_http() -> None:
    provider = WeKnoraProvider(
        config=_config("http://127.0.0.1:1", snapshot_id="unverified-label"),
        transport=lambda *_args: pytest.fail("unsupported snapshot must not issue HTTP"),
    )
    searched = provider.search(_request(expected_snapshot_id="unverified-label"))
    read_back = provider.read_chunk(LiteratureChunkReadRequest(
        request_id="fixed-read", chunk_id="chunk-00000001",
        expected_snapshot_id="unverified-label",
    ))
    assert searched.status is LiteratureCallStatus.PROVIDER_ERROR
    assert read_back.status is LiteratureCallStatus.PROVIDER_ERROR
    assert searched.error_code == read_back.error_code == "snapshot_unavailable"
    assert provider.calls == []


def test_configuration_label_cannot_pass_formal_evidence_gate(tmp_path) -> None:
    from medical_qa_data_factory.generation.executor import ToolCallError
    from medical_qa_data_factory.generation.literature_execution import LiteratureEvidenceExecutor

    provider = WeKnoraProvider(config=_config("http://127.0.0.1:1", snapshot_id="label"))
    executor = LiteratureEvidenceExecutor(
        provider=provider, repository_root=str(tmp_path), episode_id="episode",
        run_id="run", knowledge_base_ids=["kb"], needs={}, evidence_by_id={},
    )
    with pytest.raises(ToolCallError, match="pinned literature snapshot"):
        executor.preflight()
    assert provider.calls == []


def test_http_200_failure_is_not_empty_evidence() -> None:
    with _Server({"search": [{"status": 200, "body": {"success": False, "data": []}}]}) as server:
        result = WeKnoraProvider(config=_config(server.url)).search(_request())
    assert result.status is LiteratureCallStatus.PROVIDER_ERROR
    assert result.error_code == "provider_rejected"


@pytest.mark.parametrize("rows", [[{}], ["invalid"], [dict(SEARCH_ROW, start_at="not-a-number")]])
def test_malformed_search_rows_are_not_empty_evidence(rows) -> None:
    with _Server({"search": [{"status": 200, "body": {"success": True, "data": rows}}]}) as server:
        result = WeKnoraProvider(config=_config(server.url)).search(_request())
    assert result.status is LiteratureCallStatus.PARSE_ERROR
    assert result.error_code == "invalid_chunks"


def test_http_200_rejected_readback_is_not_success() -> None:
    with _Server({"chunk": [{"status": 200, "body": {"success": False, "data": CHUNK_ROW}}]}) as server:
        result = WeKnoraProvider(config=_config(server.url)).read_chunk(
            LiteratureChunkReadRequest(request_id="read", chunk_id="chunk-00000001")
        )
    assert result.status is LiteratureCallStatus.PROVIDER_ERROR
    assert result.error_code == "provider_rejected"
