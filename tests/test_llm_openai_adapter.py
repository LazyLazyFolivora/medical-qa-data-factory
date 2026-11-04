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


def test_exploration_note_preflights_full_request_and_resumes(tmp_path, monkeypatch):
    from medical_qa_data_factory.generation.exploration_memory import prepare_exploration_history
    from medical_qa_data_factory.generation.workflow_scheduler import SearchChoice

    monkeypatch.setenv("TEST_MODEL_KEY", "test")
    responses = [{"body": _envelope(json.dumps({"note": "Earlier search found a single source."}))},
                 {"body": _envelope(json.dumps({"action": "stop", "rationale": "Search complete."}))}]
    with _Server(responses) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        base = StructuredGenerationRequest(role="objective_workflow_planner", system_prompt_version="test",
            invocation_id="plan", idempotency_key="key", payload={"stage": "exploration", "facts": ["a" * 50000]})
        prepare_exploration_history(model, base, SearchChoice, tmp_path, "run")
        full = base.model_copy(update={"payload": {**base.payload, "facts": ["b" * 50000, *base.payload["facts"]]}})
        assert model.needs_history_compaction(full, SearchChoice)
        ready = prepare_exploration_history(model, full, SearchChoice, tmp_path, "run")
        assert ready.payload["facts"] == ["b" * 50000]
        assert not model.needs_history_compaction(ready, SearchChoice)
        model.generate_structured(ready, SearchChoice)
        resumed = OpenAICompatibleModel(config=_config(server.url))
        assert prepare_exploration_history(resumed, full, SearchChoice, tmp_path, "run").payload == ready.payload
        assert len(server.requests) == 2


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


def test_json_object_mode_supports_a_self_hosted_server_without_auth() -> None:
    with _Server(
        [{"status": 200, "body": _envelope(json.dumps({"value": "local"}))}]
    ) as server:
        model = OpenAICompatibleModel(
            config=_config(
                server.url,
                auth_mode="none",
                api_key_env=None,
                structured_output_mode="json_object",
                supports_strict_json_schema=False,
            )
        )
        result = model.generate_structured(_request(), Answer)

    assert result.parsed["value"] == "local"
    assert server.requests[0]["response_format"] == {"type": "json_object"}
    assert "Authorization" not in server.headers[0]


def test_prompt_only_mode_omits_response_format() -> None:
    with _Server(
        [{"status": 200, "body": _envelope(json.dumps({"value": "local"}))}]
    ) as server:
        model = OpenAICompatibleModel(
            config=_config(
                server.url,
                structured_output_mode="prompt_only",
                supports_strict_json_schema=False,
            )
        )
        model.generate_structured(_request(), Answer)

    assert "response_format" not in server.requests[0]


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
    delays: list[float] = []
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
            config=_config(server.url, max_retries=1),
            retry_sleep=delays.append,
        )
        result = model.generate_structured(_request(), Answer)

    assert result.parsed["value"] == "recovered"
    assert len(model.calls) == 2
    assert model.calls[0].error_code == "model_provider_unavailable"
    assert model.calls[1].repaired is False
    assert delays == [0.5]


def test_a_400_is_retried_with_a_compatible_structured_output_mode() -> None:
    delays: list[float] = []
    with _Server(
        [
            {
                "status": 400,
                "body": {
                    "error": {
                        "message": "response_format json_schema is unsupported",
                        "type": "invalid_request_error",
                    }
                },
                "request_id": "req-400",
            },
            {
                "status": 200,
                "body": _envelope(json.dumps({"value": "recovered"})),
            },
        ]
    ) as server:
        model = OpenAICompatibleModel(
            config=_config(server.url, max_retries=1),
            retry_sleep=delays.append,
        )
        result = model.generate_structured(_request(), Answer)

    assert result.parsed["value"] == "recovered"
    assert len(server.requests) == 2
    assert server.requests[0]["response_format"]["type"] == "json_schema"
    assert server.requests[1]["response_format"] == {"type": "json_object"}
    assert delays == [0.5]
    assert model.calls[0].http_status == 400
    assert model.calls[0].provider_request_id == "req-400"
    assert "json_schema" in (model.calls[0].error_detail or "")
    assert model.calls[1].structured_output_fallback is True


def test_an_exhausted_400_is_retryable_and_preserves_a_sanitized_detail() -> None:
    with _Server(
        [
            {
                "status": 400,
                "body": {
                    "error": {
                        "message": "bad request body",
                        "api_key": "super-secret",
                    }
                },
            },
            {
                "status": 400,
                "body": {
                    "error": {
                        "message": "bad request body",
                        "api_key": "super-secret",
                    }
                },
            },
        ]
    ) as server:
        model = OpenAICompatibleModel(
            config=_config(server.url, max_retries=1),
            retry_sleep=lambda _: None,
        )
        with pytest.raises(ModelError) as excinfo:
            model.generate_structured(_request(), Answer)

    assert excinfo.value.code is ModelErrorCode.BAD_REQUEST
    assert excinfo.value.status_code == 400
    assert excinfo.value.retryable is True
    assert "bad request body" in (excinfo.value.provider_response or "")
    assert "super-secret" not in (excinfo.value.provider_response or "")
    assert excinfo.value.attempts == 2


def test_schema_repair_stays_on_the_fallback_structured_output_mode() -> None:
    with _Server(
        [
            {
                "status": 400,
                "body": {"error": {"message": "json_schema unsupported"}},
            },
            {"status": 200, "body": _envelope("not json")},
            {
                "status": 200,
                "body": _envelope(json.dumps({"value": "repaired"})),
            },
        ]
    ) as server:
        model = OpenAICompatibleModel(
            config=_config(server.url, max_retries=2),
            retry_sleep=lambda _: None,
        )
        result = model.generate_structured(_request(), Answer)

    assert result.parsed["value"] == "repaired"
    assert server.requests[1]["response_format"] == {"type": "json_object"}
    assert server.requests[2]["response_format"] == {"type": "json_object"}
    repair_messages = [
        item["content"] for item in server.requests[2]["messages"]
    ]
    assert any(
        "did not satisfy the required JSON schema" in item
        for item in repair_messages
    )


def test_repeated_400s_never_restore_the_failed_json_schema_mode() -> None:
    with _Server(
        [
            {"status": 400, "body": {"error": {"message": "bad schema"}}},
            {"status": 400, "body": {"error": {"message": "bad object"}}},
            {"status": 400, "body": {"error": {"message": "bad prompt"}}},
            {"status": 400, "body": {"error": {"message": "still bad"}}},
        ]
    ) as server:
        model = OpenAICompatibleModel(
            config=_config(server.url, max_retries=3),
            retry_sleep=lambda _: None,
        )
        with pytest.raises(ModelError) as excinfo:
            model.generate_structured(_request(), Answer)

    assert len(server.requests) == 4
    assert server.requests[1]["response_format"] == {"type": "json_object"}
    assert "response_format" not in server.requests[2]
    assert "response_format" not in server.requests[3]
    assert excinfo.value.attempts == 4


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
                {"status": 200, "body": "not json after retry"},
                {"status": 200, "body": "invalid JSON after all retries"},
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
                {"status": 200, "body": _envelope(json.dumps({"wrong_field": 3}))},
                {"status": 200, "body": _envelope(json.dumps({"wrong_field": 4}))},
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


def test_repair_instructions_remain_in_the_leading_system_message() -> None:
    with _Server(
        [
            {"status": 200, "body": _envelope(json.dumps({"value": ""}))},
            {"status": 200, "body": _envelope(json.dumps({"value": "fixed"}))},
        ]
    ) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        model.generate_structured(_request(), Answer)

    messages = server.requests[1]["messages"]
    assert [item["role"] for item in messages] == ["system", "user"]
    assert "did not satisfy the required JSON schema" in messages[0]["content"]
    assert "String should have at least 1 character" in messages[0]["content"]


def test_schema_repair_is_bounded_to_three_retries() -> None:
    with _Server(
        [
            {"status": 200, "body": _envelope("not json")},
            {"status": 200, "body": _envelope("still not json")},
                {"status": 200, "body": _envelope("still invalid")},
                {"status": 200, "body": _envelope("final invalid response")},
        ]
    ) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        with pytest.raises(ModelError) as excinfo:
            model.generate_structured(_request(), Answer)
    assert excinfo.value.code is ModelErrorCode.PARSE_ERROR
    assert len(server.requests) == 4


def test_context_length_400_retries_with_a_compacted_payload() -> None:
    payload = {
        "artifact_summaries": [
            {
                "artifact_id": f"provider.{index}",
                "content_summary": {
                    "material_previews": [
                        {
                            "material_id": f"material.{preview}",
                            "preview": "large evidence " * 500,
                        }
                        for preview in range(8)
                    ],
                    "search_rationale": "rationale " * 1000,
                },
            }
            for index in range(30)
        ],
        "retrieval_history": [
            {
                "provider_id": "pubmed",
                "search_rationale": "history " * 1000,
                "search_trace": [
                    {
                        "query": {"term": f"query-{item}-{round_no}"},
                        "material_count": 1,
                    }
                    for round_no in range(8)
                ],
            }
            for item in range(30)
        ],
    }
    request = _request().model_copy(update={"payload": payload})
    context_error = {
        "error": {
            "message": (
                "This model's maximum context length is 32768 tokens. "
                "Your prompt contains at least 32769 input tokens."
            ),
            "type": "BadRequestError",
        }
    }
    with _Server(
        [
            {"status": 400, "body": context_error},
            {
                "status": 200,
                "body": _envelope(json.dumps({"value": "recovered"})),
            },
        ]
    ) as server:
        model = OpenAICompatibleModel(
            config=_config(server.url, max_retries=1),
            retry_sleep=lambda _: None,
        )
        result = model.generate_structured(request, Answer)

    assert result.parsed["value"] == "recovered"
    first_payload = json.loads(server.requests[0]["messages"][1]["content"])[
        "payload"
    ]
    second_payload = json.loads(server.requests[1]["messages"][1]["content"])[
        "payload"
    ]
    assert len(json.dumps(second_payload, default=str)) < len(
        json.dumps(first_payload, default=str)
    )
    assert len(second_payload["retrieval_history"]) <= 12
    assert len(
        second_payload["artifact_summaries"][0]["content_summary"][
            "material_previews"
        ]
    ) == 0


def test_exhausted_context_error_cannot_restart_same_stage_forever():
    failure = {"status": 400, "body": {"error": {
        "message": "This model's maximum context length is 32768 tokens."}}}
    with _Server([failure, failure]) as server:
        model = OpenAICompatibleModel(config=_config(server.url, max_retries=1),
                                      retry_sleep=lambda _: None)
        with pytest.raises(ModelError) as raised:
            model.generate_structured(_request(), Answer)
    assert raised.value.code == ModelErrorCode.BAD_REQUEST
    assert raised.value.retryable is False
    assert raised.value.attempts == 2


@pytest.mark.parametrize("limit_key", ["max_tokens", "max_completion_tokens"])
def test_context_overflow_keeps_schema_and_reduces_output_when_input_cannot_shrink(limit_key):
    failure = {"status": 400, "body": {"error": {"message":
        "This model's maximum context length is 32768 tokens. Your prompt contains at least 28673 input tokens."}}}
    with _Server([failure] * 5) as server:
        config = _config(server.url, max_retries=4, request_parameters={limit_key: 4096})
        model = OpenAICompatibleModel(config=config, retry_sleep=lambda _: None)
        with pytest.raises(ModelError) as error:
            model.generate_structured(_request(), Answer)
    assert not error.value.retryable
    assert [body[limit_key] for body in server.requests] == [4096, 2048, 1024]
    assert all(body["messages"] == server.requests[0]["messages"] for body in server.requests)
    assert all(body["response_format"] == server.requests[0]["response_format"] for body in server.requests)


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


def test_preflight_compacts_before_first_http_request():
    payload = {"retrieval_history": [{"search_rationale": "old " * 30000}],
               "evidence_excerpts": [{"quoted_text": "Important condition " * 200}]}
    with _Server([{"status": 200, "body": _envelope(json.dumps({"value": "ok"}))}]) as server:
        model = OpenAICompatibleModel(config=_config(server.url))
        result = model.generate_structured(_request().model_copy(update={"payload": payload}), Answer)
    sent = json.loads(server.requests[0]["messages"][1]["content"])["payload"]
    assert sent["evidence_excerpts"] == payload["evidence_excerpts"]
    assert len(sent["retrieval_history"][0]["search_rationale"]) <= 240
    assert result.usage["context_management"]["compaction_rounds"] > 0
    assert server.requests[0]["max_tokens"] == 4096


def test_preflight_rejects_oversized_evidence_without_transport():
    model = OpenAICompatibleModel(config=_config("http://unused"),
        transport=lambda *args: pytest.fail("oversized request reached transport"))
    request = _request().model_copy(update={"payload": {"evidence_excerpts": [
        {"quoted_text": "evidence " * 30000}]}})
    with pytest.raises(ModelError) as error:
        model.generate_structured(request, Answer)
    assert error.value.retryable is False
    assert "context budget exceeded" in error.value.message
    assert not model.calls


def test_preflight_counts_skill_and_schema():
    model = OpenAICompatibleModel(config=_config("http://unused"),
        transport=lambda *args: pytest.fail("oversized skill reached transport"))
    request = _request().model_copy(update={"skill_instruction": "instruction " * 30000})
    with pytest.raises(ModelError, match="context budget exceeded"):
        model.generate_structured(request, Answer)


def test_output_override_is_reserved_in_preflight_budget():
    from dataclasses import replace
    config = replace(_config("http://unused"), request_parameters={"max_tokens": 32000})
    model = OpenAICompatibleModel(config=config,
        transport=lambda *args: pytest.fail("invalid budget reached transport"))
    with pytest.raises(ModelError, match="context budget exceeded"):
        model.generate_structured(_request(), Answer)


def test_invalid_context_configuration_is_rejected():
    from dataclasses import replace
    from medical_qa_data_factory.llm.errors import ModelConfigurationError
    with pytest.raises(ModelConfigurationError):
        replace(_config("http://unused"), context_window_tokens=4096)
    with pytest.raises(ModelConfigurationError):
        replace(_config("http://unused"), compact_target_ratio=0.9)
def test_verification_compaction_preserves_structured_record_and_recent_history():
    import json
    from medical_qa_data_factory.llm.providers.openai_compatible import _compact_context_payload

    exon = {"exon_id": "ENSE" + "1" * 80, "start": 15322512, "end": 15322987}
    record = {"transcripts": [{"transcript_identifier": i, "exon_coordinates": [i, None, i + 3], "exon": exon}
                              for i in range(250)]}
    anchor = {"anchor_id": "a.record", "quoted_text": json.dumps(record)}
    recent = {"query": {"symbol": "X"}, "rationale": "new direction"}
    original = {"stage": "blind_solve", "independent_evidence": [anchor],
                "prior_searches": [{"query": {}, "rationale": "old explanation"}, recent]}
    compacted = _compact_context_payload(original, round_number=1)
    packed = compacted["independent_evidence"][0]["structured_evidence"]
    assert len(packed["rows"]) == 1

    def decode(value):
        if isinstance(value, list):
            return [decode(item) for item in value]
        if isinstance(value, dict):
            if "r" in value:
                return decode(packed["rows"][value["r"]])
            return dict(zip(packed["keys"][value["s"]], map(decode, value["v"]), strict=True))
        return value

    assert decode(packed["record"]) == record
    assert compacted["prior_searches"] == [{"query": {}}, recent]
    assert original["independent_evidence"][0]["quoted_text"] == json.dumps(record)


def test_ncbi_anchor_reference_requires_exact_value_in_visible_parent():
    import json
    from medical_qa_data_factory.llm.providers.openai_compatible import _compact_context_payload

    prefix = "anchor.ncbi_gene.gene_123."
    value = "long annotation " * 30
    quote = json.dumps(value)
    parent = {"anchor_id": prefix + "record", "quoted_text": json.dumps(
        {"summary": {"record": {"summary": value, "description": "different"}}})}
    fields = [{"anchor_id": prefix + key, "quoted_text": quote} for key in ("summary", "description")]
    for anchors in ([parent, *fields], fields):
        packed = _compact_context_payload({"stage": "blind_solve", "independent_evidence": anchors},
                                          round_number=1)["independent_evidence"]
        assert packed[-1]["quoted_text"] == quote
        if parent in anchors:
            assert packed[-2]["value_reference"] == {"anchor_id": parent["anchor_id"],
                                                      "field_path": ["summary", "record", "summary"]}
        else:
            assert packed[-2]["quoted_text"] == quote

