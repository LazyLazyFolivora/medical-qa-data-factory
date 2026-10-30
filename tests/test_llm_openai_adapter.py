"""The real structured-output adapter, exercised against a local HTTP server.

Step 2 of the real-generation-agent plan. The local server is a real socket
server, so the adapter's own request building, status handling and JSON
parsing are all under test; only the remote provider is replaced.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from medical_qa_data_factory.domain.common import ContractModel
from medical_qa_data_factory.llm.config import ModelProviderConfig
from medical_qa_data_factory.llm.contracts import (
    StructuredGenerationRequest,
    StructuredGenerationResult,
)
from medical_qa_data_factory.llm.errors import (
    ModelError,
    ModelErrorCode,
    UnsupportedProviderError,
)
from medical_qa_data_factory.llm.providers.openai_compatible import (
    OpenAICompatibleModel,
)


class Answer(ContractModel):
    """A tiny response contract for the tests."""

    value: str = Field(min_length=1)
    score: int = Field(default=0, ge=0)


class _Server:
    """A local HTTP server that replays a scripted list of responses."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence the server
                pass

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                outer.requests.append(json.loads(raw))
                outer.headers.append(dict(self.headers))
                if not outer.responses:
                    payload: dict[str, Any] = {"status": 500, "body": "{}"}
                else:
                    payload = outer.responses.pop(0)
                status = int(payload.get("status", 200))
                body = payload.get("body", "")
                if isinstance(body, (dict, list)):
                    body = json.dumps(body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body.encode("utf-8"))))
                if payload.get("request_id"):
                    self.send_header("x-request-id", payload["request_id"])
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

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
        return f"http://{host}:{port}/v1/chat/completions"


def _envelope(content: str, *, usage: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "choices": [{"message": {"role": "assistant", "content": content}}]
    }
    if usage:
        payload["usage"] = {"prompt_tokens": 11, "completion_tokens": 7}
    return payload


def _config(url: str, **overrides: Any) -> ModelProviderConfig:
    payload: dict[str, Any] = {
        "provider_id": "local",
        "driver": "openai_compatible",
        "base_url": url,
        "model_id": "test-model",
        "api_key_env": "TEST_MODEL_KEY",
        "timeout_seconds": 10,
    }
    payload.update(overrides)
    return ModelProviderConfig(**payload)


def _request() -> StructuredGenerationRequest:
    return StructuredGenerationRequest(
        invocation_id="inv_1",
        role="question_writer",
        system_prompt_version="prompt-v1",
        payload={"question": "what treats headache?"},
        idempotency_key="key-1",
    )


@pytest.fixture(autouse=True)
def _key(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "test-secret")


# ---- success ------------------------------------------------------------


def test_a_valid_response_is_parsed_and_validated() -> None:
    with _Server([{"status": 200, "body": _envelope(json.dumps({"value": "aspirin"}))}]) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        result = model.generate_structured(_request(), Answer)
    assert isinstance(result, StructuredGenerationResult)
    assert result.parsed == {"value": "aspirin", "score": 0}
    assert result.model_config_id == "local:test-model"


def test_the_request_carries_a_strict_json_schema() -> None:
    with _Server([{"status": 200, "body": _envelope(json.dumps({"value": "x"}))}]) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        model.generate_structured(_request(), Answer)
    body = server.requests[0]
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert "value" in response_format["json_schema"]["schema"]["properties"]


def test_the_request_injects_role_and_skill_prompts() -> None:
    request = _request().model_copy(
        update={
            "skill_id": "layperson-search-v1",
            "skill_version": "1.0.0",
            "skill_instruction": "SKILL_MARKER: write like a real search user",
        }
    )
    with _Server([{"status": 200, "body": _envelope(json.dumps({"value": "x"}))}]) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        model.generate_structured(request, Answer)
    body = server.requests[0]
    assert "SKILL_MARKER" in body["messages"][0]["content"]
    assert "You write the question text" in body["messages"][0]["content"]
    user_payload = json.loads(body["messages"][1]["content"])
    assert user_payload["skill_id"] == "layperson-search-v1"
    assert user_payload["skill_version"] == "1.0.0"


def test_the_api_key_is_sent_as_a_bearer_header() -> None:
    with _Server([{"status": 200, "body": _envelope(json.dumps({"value": "x"}))}]) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        model.generate_structured(_request(), Answer)
    assert server.headers[0].get("Authorization") == "Bearer test-secret"


def test_the_provider_request_id_is_recorded() -> None:
    with _Server(
        [
            {
                "status": 200,
                "body": _envelope(json.dumps({"value": "x"})),
                "request_id": "req-42",
            }
        ]
    ) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        model.generate_structured(_request(), Answer)
    assert model.calls[0].provider_request_id == "req-42"


def test_usage_is_recorded_from_the_provider() -> None:
    with _Server([{"status": 200, "body": _envelope(json.dumps({"value": "x"}))}]) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        result = model.generate_structured(_request(), Answer)
    assert result.usage["input_tokens"] == 11
    assert result.usage["output_tokens"] == 7
    assert model.usage_records[0].input_tokens == 11
    assert model.usage_records[0].output_tokens == 7


def test_a_response_without_usage_still_succeeds() -> None:
    """Missing usage is recorded as unknown, not as zero."""
    with _Server(
        [{"status": 200, "body": _envelope(json.dumps({"value": "x"}), usage=False)}]
    ) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        result = model.generate_structured(_request(), Answer)
    assert result.parsed == {"value": "x", "score": 0}
    assert result.usage["input_tokens"] is None
    assert model.usage_records[0].input_tokens is None


# ---- failures ------------------------------------------------------------


def test_a_401_is_an_auth_error() -> None:
    with _Server([{"status": 401, "body": {}}]) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        with pytest.raises(ModelError) as excinfo:
            model.generate_structured(_request(), Answer)
    assert excinfo.value.code is ModelErrorCode.AUTH_ERROR
    assert excinfo.value.retryable is False


def test_a_429_is_a_retryable_rate_limit() -> None:
    with _Server([{"status": 429, "body": {}}]) as server:
        model = OpenAICompatibleModel(
            config=_config(server.url, max_retries=0)
        )
        with pytest.raises(ModelError) as excinfo:
            model.generate_structured(_request(), Answer)
    assert excinfo.value.code is ModelErrorCode.RATE_LIMITED
    assert excinfo.value.retryable is True


def test_a_500_is_a_retryable_provider_unavailable_error() -> None:
    with _Server([{"status": 500, "body": {}}]) as server:
        model = OpenAICompatibleModel(
            config=_config(server.url, max_retries=0)
        )
        with pytest.raises(ModelError) as excinfo:
            model.generate_structured(_request(), Answer)
    assert excinfo.value.code is ModelErrorCode.PROVIDER_UNAVAILABLE
    assert excinfo.value.status_code == 500
    assert excinfo.value.retryable is True


def test_a_transient_502_is_retried_without_becoming_a_schema_repair() -> None:
    with _Server(
        [
            {"status": 502, "body": {}},
            {
                "status": 200,
                "body": _envelope(json.dumps({"value": "recovered"})),
            },
        ]
    ) as server:
        model = OpenAICompatibleModel(
            config=_config(server.url, max_retries=1)
        )
        result = model.generate_structured(_request(), Answer)

    assert result.parsed["value"] == "recovered"
    assert len(model.calls) == 2
    assert model.calls[0].error_code == "model_provider_unavailable"
    assert model.calls[1].repaired is False


def test_a_400_remains_a_non_retryable_internal_provider_error() -> None:
    with _Server([{"status": 400, "body": {}}]) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        with pytest.raises(ModelError) as excinfo:
            model.generate_structured(_request(), Answer)
    assert excinfo.value.code is ModelErrorCode.INTERNAL_ERROR
    assert excinfo.value.status_code == 400
    assert excinfo.value.retryable is False


def test_a_timeout_is_classified_as_a_timeout() -> None:
    def _timeout(url: str, headers: dict, body: dict, timeout: int):
        raise TimeoutError("too slow")

    model = OpenAICompatibleModel(config=_config("http://127.0.0.1:1/x"), transport=_timeout)
    with pytest.raises(ModelError) as excinfo:
        model.generate_structured(_request(), Answer)
    assert excinfo.value.code is ModelErrorCode.TIMEOUT
    assert excinfo.value.retryable is True


def test_a_connection_failure_is_a_network_error() -> None:
    def _broken(url: str, headers: dict, body: dict, timeout: int):
        raise OSError("connection refused")

    model = OpenAICompatibleModel(config=_config("http://127.0.0.1:1/x"), transport=_broken)
    with pytest.raises(ModelError) as excinfo:
        model.generate_structured(_request(), Answer)
    assert excinfo.value.code is ModelErrorCode.NETWORK_ERROR


def test_invalid_json_is_a_parse_error() -> None:
    # Two invalid responses: the first triggers the single repair, the second
    # exhausts it, and the surfaced error is still a parse error.
    with _Server(
        [
            {"status": 200, "body": "not json at all"},
            {"status": 200, "body": "still not json"},
        ]
    ) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        with pytest.raises(ModelError) as excinfo:
            model.generate_structured(_request(), Answer)
    assert excinfo.value.code is ModelErrorCode.PARSE_ERROR


def test_a_response_that_violates_the_contract_is_a_schema_error() -> None:
    with _Server(
        [
            {"status": 200, "body": _envelope(json.dumps({"wrong_field": 1}))},
            {"status": 200, "body": _envelope(json.dumps({"wrong_field": 2}))},
        ]
    ) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        with pytest.raises(ModelError) as excinfo:
            model.generate_structured(_request(), Answer)
    assert excinfo.value.code is ModelErrorCode.SCHEMA_ERROR


# ---- the single schema repair -------------------------------------------


def test_one_schema_repair_is_attempted_and_succeeds() -> None:
    with _Server(
        [
            {"status": 200, "body": _envelope("not json")},
            {"status": 200, "body": _envelope(json.dumps({"value": "repaired"}))},
        ]
    ) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        result = model.generate_structured(_request(), Answer)
    assert result.parsed["value"] == "repaired"
    assert len(server.requests) == 2
    assert model.calls[1].repaired is True


def test_the_repair_prompt_is_added_to_the_second_request() -> None:
    with _Server(
        [
            {"status": 200, "body": _envelope("not json")},
            {"status": 200, "body": _envelope(json.dumps({"value": "ok"}))},
        ]
    ) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        model.generate_structured(_request(), Answer)
    second = server.requests[1]
    contents = [item["content"] for item in second["messages"]]
    assert any("did not satisfy the required JSON schema" in item for item in contents)


def test_the_repair_request_includes_the_specific_validation_error() -> None:
    with _Server(
        [
            {"status": 200, "body": _envelope(json.dumps({"value": ""}))},
            {"status": 200, "body": _envelope(json.dumps({"value": "fixed"}))},
        ]
    ) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        result = model.generate_structured(_request(), Answer)

    assert result.parsed["value"] == "fixed"
    repair_messages = [
        item["content"] for item in server.requests[1]["messages"]
    ]
    assert any(
        "String should have at least 1 character" in item
        for item in repair_messages
    )


def test_a_second_failure_is_not_repaired_again() -> None:
    with _Server(
        [
            {"status": 200, "body": _envelope("not json")},
            {"status": 200, "body": _envelope("still not json")},
        ]
    ) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        with pytest.raises(ModelError) as excinfo:
            model.generate_structured(_request(), Answer)
    assert excinfo.value.code is ModelErrorCode.PARSE_ERROR
    assert len(server.requests) == 2


# ---- provider capability -------------------------------------------------


def test_a_provider_without_strict_json_schema_fails_fast() -> None:
    with pytest.raises(UnsupportedProviderError):
        OpenAICompatibleModel(
            config=_config("http://127.0.0.1:1/x", supports_strict_json_schema=False)
        )


def test_a_missing_api_key_fails_before_the_call() -> None:
    """A missing key must not produce a request."""
    with _Server([{"status": 200, "body": _envelope(json.dumps({"value": "x"}))}]) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        import os

        previous = os.environ.pop("TEST_MODEL_KEY", None)
        try:
            with pytest.raises(ModelError) as excinfo:
                model.generate_structured(_request(), Answer)
        finally:
            if previous is not None:
                os.environ["TEST_MODEL_KEY"] = previous
    assert excinfo.value.code is ModelErrorCode.CONFIG_ERROR
    assert server.requests == []
