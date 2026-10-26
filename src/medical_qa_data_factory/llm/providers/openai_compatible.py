"""OpenAI-compatible structured-output adapter.

This is the real provider adapter for ``structured`` mode. It speaks the
OpenAI chat-completions shape with a JSON Schema response format, and it is
deliberately strict about the things the plan requires:

* The API key comes from the environment at call time, never from config.
* A provider that cannot honour a strict JSON Schema fails fast instead of
  quietly asking for free-form text.
* Each physical attempt is a separate call with its own usage record, so a
  schema repair is billed twice, not once.
* The raw response is kept out of the parse path: the model must return JSON
  that validates against the requested contract.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ...domain.common import ContractModel, UsageRecord, new_id, utc_now
from ..contracts import StructuredGenerationRequest, StructuredGenerationResult
from ..config import ModelProviderConfig
from ..errors import ModelError, ModelErrorCode, UnsupportedProviderError
from ..prompts import template_for


#: The system prompt used for the single allowed schema repair.
REPAIR_PROMPT = (
    "Your previous reply did not satisfy the required JSON schema. "
    "Reply with the corrected JSON object only. Do not explain it."
)


@dataclass
class ModelCallRecord:
    """What one physical attempt did, for the audit trail."""

    attempt_id: str
    role: str
    status: str
    http_status: int | None = None
    provider_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    repaired: bool = False
    error_code: str | None = None
    elapsed_ms: int = 0


@dataclass
class OpenAICompatibleModel:
    """Structured-output adapter for an OpenAI-compatible endpoint."""

    config: ModelProviderConfig
    #: Injected for tests. It must accept (url, headers, body, timeout) and
    #: return (status_code, headers, body_text).
    transport: Any = None
    calls: list[ModelCallRecord] = field(default_factory=list)
    usage_records: list[UsageRecord] = field(default_factory=list)
    #: Called once per physical attempt, before the request goes out. A
    #: budget wrapper uses it to reserve per attempt, so a schema repair is
    #: charged twice rather than once.
    on_physical_call: Any = None

    def __post_init__(self) -> None:
        if not self.config.supports_strict_json_schema:
            raise UnsupportedProviderError(
                f"provider {self.config.provider_id!r} does not support a strict "
                "JSON Schema response format; structured mode cannot run on it"
            )
        if self.transport is None:
            self.transport = _http_transport

    # ---- public API ----------------------------------------------------

    def generate_structured(
        self,
        request: StructuredGenerationRequest,
        response_model: type[ContractModel],
    ) -> StructuredGenerationResult:
        """Call the provider, repairing malformed output with bounded retries."""
        schema = response_model.model_json_schema()
        body = self._build_body(request, schema)
        max_attempts = max(1, self.config.max_retries + 1)
        last_error: ModelError | None = None
        for attempt_index in range(max_attempts):
            repaired = attempt_index > 0
            attempt_body = (
                self._build_body(request, schema, repair=True)
                if repaired
                else body
            )
            response = self._attempt(
                request, attempt_body, repaired=repaired
            )
            response.record.repaired = repaired
            try:
                parsed, usage = self._parse(response, response_model)
            except ModelError as exc:
                last_error = exc
                if (
                    exc.code
                    not in {
                        ModelErrorCode.PARSE_ERROR,
                        ModelErrorCode.SCHEMA_ERROR,
                    }
                    or attempt_index + 1 >= max_attempts
                ):
                    raise
                continue
            usage_record = self._usage_record(request, usage)
            self.usage_records.append(usage_record)
            return StructuredGenerationResult(
                invocation_id=request.invocation_id,
                role=request.role,
                parsed=parsed,
                usage={
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "model_config_id": self.config.model_config_id(),
                    "attempts": attempt_index + 1,
                },
                model_config_id=self.config.model_config_id(),
            )
        if last_error is not None:
            raise last_error
        raise ModelError(
            ModelErrorCode.INTERNAL_ERROR,
            "structured model call exhausted without a response",
        )

    def next_invocation_id(self, prefix: str = "invocation") -> str:
        return new_id(prefix)

    # ---- internals -----------------------------------------------------

    def _build_body(
        self,
        request: StructuredGenerationRequest,
        schema: dict[str, Any],
        *,
        repair: bool = False,
    ) -> dict[str, Any]:
        if not request.system_prompt_version.strip():
            raise ModelError(
                ModelErrorCode.CONFIG_ERROR,
                "a structured call must name the prompt version it used",
            )
        role_template = template_for(request.role)
        system_prompt = role_template.body
        if request.skill_instruction:
            system_prompt = (
                f"{system_prompt}\n\nSkill {request.skill_id} "
                f"version {request.skill_version}:\n{request.skill_instruction}"
            )
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    f"{system_prompt}\n\n"
                    "You return one JSON object that satisfies the given schema. "
                    "Return JSON only."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "role": request.role,
                        "system_prompt_version": request.system_prompt_version,
                        "skill_id": request.skill_id,
                        "skill_version": request.skill_version,
                        "payload": request.payload,
                        "json_schema": schema,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            },
        ]
        if repair:
            messages.append({"role": "system", "content": REPAIR_PROMPT})
        body: dict[str, Any] = {
            "model": self.config.model_id,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        body.update(self.config.request_parameters)
        return body

    def _attempt(
        self,
        request: StructuredGenerationRequest,
        body: dict[str, Any],
        *,
        repaired: bool,
    ) -> "_RawResponse":
        # Announce the physical call before it goes out, so a budget wrapper
        # can reserve per attempt. A schema repair is a second physical call
        # and must be charged twice.
        if self.on_physical_call is not None:
            self.on_physical_call(request, repaired)
        started = time.monotonic()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key()}",
        }
        try:
            status, response_headers, text = self.transport(
                self.config.base_url, headers, body, self.config.timeout_seconds
            )
        except TimeoutError as exc:
            record = ModelCallRecord(
                attempt_id=new_id("attempt"),
                role=request.role,
                status="failed",
                error_code=ModelErrorCode.TIMEOUT.value,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            self.calls.append(record)
            raise ModelError(
                ModelErrorCode.TIMEOUT,
                f"model call timed out after {self.config.timeout_seconds}s",
            ) from exc
        except ModelError:
            raise
        except Exception as exc:  # transport-level failure
            record = ModelCallRecord(
                attempt_id=new_id("attempt"),
                role=request.role,
                status="failed",
                error_code=ModelErrorCode.NETWORK_ERROR.value,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            self.calls.append(record)
            raise ModelError(
                ModelErrorCode.NETWORK_ERROR,
                f"model call failed: {type(exc).__name__}: {exc}",
            ) from exc
        elapsed = int((time.monotonic() - started) * 1000)
        provider_request_id = _header(response_headers, "x-request-id")
        if status == 401 or status == 403:
            record = ModelCallRecord(
                attempt_id=new_id("attempt"),
                role=request.role,
                status="failed",
                http_status=status,
                provider_request_id=provider_request_id,
                error_code=ModelErrorCode.AUTH_ERROR.value,
                elapsed_ms=elapsed,
            )
            self.calls.append(record)
            raise ModelError(
                ModelErrorCode.AUTH_ERROR,
                f"model provider rejected the credentials (HTTP {status})",
                status_code=status,
                provider_request_id=provider_request_id,
            )
        if status == 429:
            record = ModelCallRecord(
                attempt_id=new_id("attempt"),
                role=request.role,
                status="failed",
                http_status=status,
                provider_request_id=provider_request_id,
                error_code=ModelErrorCode.RATE_LIMITED.value,
                elapsed_ms=elapsed,
            )
            self.calls.append(record)
            raise ModelError(
                ModelErrorCode.RATE_LIMITED,
                f"model provider rate limited the call (HTTP 429)",
                status_code=status,
                provider_request_id=provider_request_id,
            )
        if status >= 400:
            record = ModelCallRecord(
                attempt_id=new_id("attempt"),
                role=request.role,
                status="failed",
                http_status=status,
                provider_request_id=provider_request_id,
                error_code=ModelErrorCode.INTERNAL_ERROR.value,
                elapsed_ms=elapsed,
            )
            self.calls.append(record)
            raise ModelError(
                ModelErrorCode.INTERNAL_ERROR,
                f"model provider returned HTTP {status}",
                status_code=status,
                provider_request_id=provider_request_id,
            )
        record = ModelCallRecord(
            attempt_id=new_id("attempt"),
            role=request.role,
            status="success",
            http_status=status,
            provider_request_id=provider_request_id,
            elapsed_ms=elapsed,
        )
        self.calls.append(record)
        return _RawResponse(body=text, record=record)

    def _parse(
        self, response: "_RawResponse", response_model: type[ContractModel]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            envelope = json.loads(response.body)
        except json.JSONDecodeError as exc:
            raise ModelError(
                ModelErrorCode.PARSE_ERROR,
                f"model response is not valid JSON: {exc}",
            ) from exc
        if not isinstance(envelope, dict):
            raise ModelError(
                ModelErrorCode.PARSE_ERROR, "model response is not a JSON object"
            )
        content = _message_content(envelope)
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ModelError(
                ModelErrorCode.PARSE_ERROR,
                f"model message content is not valid JSON: {exc}",
            ) from exc
        if not isinstance(raw, dict):
            raise ModelError(
                ModelErrorCode.PARSE_ERROR,
                "model message content is not a JSON object",
            )
        try:
            parsed = response_model.model_validate(raw)
        except Exception as exc:
            raise ModelError(
                ModelErrorCode.SCHEMA_ERROR,
                f"model response does not satisfy the contract: {exc}",
            ) from exc
        usage = envelope.get("usage") or {}
        return (
            parsed.model_dump(mode="json"),
            {
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
            },
        )

    def _usage_record(
        self, request: StructuredGenerationRequest, usage: dict[str, Any]
    ) -> UsageRecord:
        return UsageRecord(
            usage_id=new_id("usage"),
            usage_scope="generation",
            invocation_id=request.invocation_id,
            tool_name=f"model:{self.config.provider_id}",
            tool_calls=1,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            elapsed_ms=sum(
                call.elapsed_ms for call in self.calls if call.role == request.role
            ),
            measurement_source="provider",
            meter_version="model-usage-v1",
            recorded_at=utc_now(),
        )


@dataclass
class _RawResponse:
    body: str
    record: ModelCallRecord


def _message_content(envelope: dict[str, Any]) -> str:
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelError(
            ModelErrorCode.PARSE_ERROR, "model response has no choices"
        )
    first = choices[0]
    if not isinstance(first, dict):
        raise ModelError(
            ModelErrorCode.PARSE_ERROR, "model response choice is not an object"
        )
    message = first.get("message")
    if not isinstance(message, dict):
        raise ModelError(
            ModelErrorCode.PARSE_ERROR, "model response choice has no message"
        )
    content = message.get("content")
    if not isinstance(content, str):
        raise ModelError(
            ModelErrorCode.PARSE_ERROR, "model response message has no string content"
        )
    return content


def _header(headers: Any, name: str) -> str | None:
    if not headers:
        return None
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    if value is None:
        try:
            value = headers.get(name.title())
        except AttributeError:
            return None
    return str(value) if value is not None else None


def _http_transport(
    url: str, headers: dict[str, str], body: dict[str, Any], timeout: int
) -> tuple[int, dict[str, str], str]:
    """Default transport. Uses ``requests`` when it is installed."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ModelError(
            ModelErrorCode.INTERNAL_ERROR,
            "the requests package is required for real model calls",
        ) from exc
    try:
        response = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.Timeout as exc:
        raise TimeoutError(str(exc)) from exc
    return response.status_code, dict(response.headers), response.text
