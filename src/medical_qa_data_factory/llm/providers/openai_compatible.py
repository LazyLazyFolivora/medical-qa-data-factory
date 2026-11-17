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
import hashlib
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ...domain.common import ContractModel, UsageRecord, new_id, utc_now
from ..contracts import StructuredGenerationRequest, StructuredGenerationResult
from ..config import ModelProviderConfig
from ..context import ContextManager, estimate_tokens
from ..errors import ModelError, ModelErrorCode, UnsupportedProviderError
from ..prompts import template_for
from ..run_trace import trace_event


#: The system prompt used for the single allowed schema repair.
REPAIR_PROMPT = (
    "Your previous reply did not satisfy the required JSON schema. "
    "Repair that reply using the validation error and the original task. "
    "Preserve valid content and judgments; change them only when required to resolve the reported error "
    "or a contradiction identified by that error. Complete missing fields from the original evidence. "
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
    structured_output_fallback: bool = False
    error_code: str | None = None
    error_detail: str | None = None
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
    retry_backoff_seconds: float = 0.5
    retry_sleep: Any = time.sleep
    _token_counts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _tokenizer_unavailable: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.config.structured_output_mode not in {
            "json_schema",
            "json_object",
            "prompt_only",
        }:
            raise UnsupportedProviderError(
                f"provider {self.config.provider_id!r} has unsupported "
                f"structured output mode {self.config.structured_output_mode!r}"
            )
        if (
            self.config.structured_output_mode == "json_schema"
            and not self.config.supports_strict_json_schema
        ):
            raise UnsupportedProviderError(
                f"provider {self.config.provider_id!r} does not support a strict "
                "JSON Schema response format; structured mode cannot run on it"
            )
        if self.transport is None:
            self.transport = _http_transport

    # ---- public API ----------------------------------------------------

    def generate_structured(self, request, response_model):
        try:
            result = self._generate_structured(request, response_model)
            trace_event("model_result", invocation_id=request.invocation_id, role=request.role,
                        stage=request.payload.get("stage"), result=result.model_dump(mode="json"))
            return result
        except Exception as exc:
            trace_event("model_error", invocation_id=request.invocation_id, role=request.role,
                        stage=request.payload.get("stage"), error_type=type(exc).__name__, error=str(exc))
            raise

    def _generate_structured(
        self,
        request: StructuredGenerationRequest,
        response_model: type[ContractModel],
    ) -> StructuredGenerationResult:
        """Call the provider, repairing malformed output with bounded retries."""
        schema = response_model.model_json_schema()
        max_attempts = max(1, self.config.max_retries + 1)
        last_error: ModelError | None = None
        repair = False
        repair_error = None
        repair_content = None
        fallback_mode: str | None = None
        active_request = request
        context_compaction_round = 0
        output_cap = None

        def build(current):
            body = self._build_body(current, schema, repair=repair,
                repair_error=repair_error,
                repair_content=repair_content,
                structured_output_mode=fallback_mode)
            if output_cap is not None:
                key = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
                body[key] = min(body[key], output_cap)
            return body

        for attempt_index in range(max_attempts):
            context = ContextManager(self.config, self._count_input_tokens)
            try:
                active_request, attempt_body, context_report = context.prepare(
                    active_request, build, _compact_context_request,
                )
            except ModelError as exc:
                if not repair_content or exc.code is not ModelErrorCode.BAD_REQUEST:
                    raise
                # Rebuild the same evidence-bearing request without an optional
                # malformed reply if that reply consumed the remaining budget.
                repair_content = None
                active_request, attempt_body, context_report = context.prepare(
                    active_request, build, _compact_context_request,
                )
            try:
                response = self._attempt(
                    active_request, attempt_body, repaired=repair
                )
            except ModelError as exc:
                exc.attempts = attempt_index + 1
                if self.calls:
                    self.calls[-1].repaired = repair
                    self.calls[-1].structured_output_fallback = (
                        fallback_mode is not None
                    )
                last_error = exc
                if exc.retryable and attempt_index + 1 < max_attempts:
                    if exc.code is ModelErrorCode.BAD_REQUEST:
                        if _is_context_length_error(exc):
                            context_compaction_round += 1
                            compacted = _compact_context_request(
                                active_request,
                                round_number=context_compaction_round + 1,
                            )
                            shrank = self._count_input_tokens(build(compacted))[0] < self._count_input_tokens(attempt_body)[0]
                            if shrank:
                                active_request = compacted
                            # Some servers stop counting at the first token over the limit.
                            # Allow headroom rather than subtracting the reported excess of one.
                            previous = attempt_body.get("max_completion_tokens", attempt_body.get("max_tokens"))
                            cap = max(1024, previous // 2)
                            if cap < previous:
                                output_cap = cap
                            elif not shrank:
                                exc.retryable = False
                                raise
                        else:
                            fallback_mode = self._next_structured_output_mode(
                                attempt_body, current_mode=fallback_mode or self.config.structured_output_modes_by_role.get(
                                    request.role, self.config.structured_output_mode))
                    delay = self.retry_backoff_seconds * (2 ** attempt_index)
                    if delay > 0:
                        self.retry_sleep(delay)
                    continue
                if exc.code is ModelErrorCode.BAD_REQUEST and _is_context_length_error(exc):
                    # Bounded compaction has already failed. Retrying the same
                    # stage from the orchestration loop only resets this budget.
                    exc.retryable = False
                raise
            response.record.repaired = repair
            response.record.structured_output_fallback = (
                fallback_mode is not None
            )
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
                repair = True
                repair_error = exc.message[:1500]
                # Keep only the public answer, never provider reasoning fields.
                # The next attempt repairs this output instead of solving afresh.
                try:
                    envelope = json.loads(response.body)
                    repair_content = _message_content(envelope) if isinstance(envelope, dict) else None
                    if repair_content is not None and (
                        len(repair_content) > 6000 or not repair_content.strip()
                    ):
                        repair_content = None
                except (json.JSONDecodeError, ModelError):
                    repair_content = None
                continue
            usage_record = self._usage_record(active_request, usage)
            self.usage_records.append(usage_record)
            return StructuredGenerationResult(
                invocation_id=active_request.invocation_id,
                role=active_request.role,
                parsed=parsed,
                usage={
                    "context_management": context_report,
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "model_config_id": self.config.model_config_id(),
                    "attempts": attempt_index + 1,
                    "structured_output_fallback": (
                        fallback_mode is not None
                    ),
                },
                model_config_id=self.config.model_config_id(),
            )
        if last_error is not None:
            raise last_error
        raise ModelError(
            ModelErrorCode.INTERNAL_ERROR,
            "structured model call exhausted without a response",
        )

    def needs_history_compaction(self, request, response_model):
        body = self._build_body(request, response_model.model_json_schema())
        budget = ContextManager(self.config).input_budget(body)
        return self._count_input_tokens(body)[0] > budget * self.config.compact_trigger_ratio

    def _count_input_tokens(self, body):
        """Count rendered chat input, excluding the decoder grammar and HTTP envelope."""
        if self.config.tokenization_url and not self._tokenizer_unavailable:
            payload = {k: v for k, v in body.items() if k in {
                "model", "messages", "tools", "tool_choice", "chat_template_kwargs"}}
            payload["add_generation_prompt"] = True
            key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if key in self._token_counts:
                return self._token_counts[key], "server_tokenizer"
            headers = {"Content-Type": "application/json"}
            api_key = self.config.api_key()
            if api_key is not None:
                headers["Authorization"] = f"Bearer {api_key}"
            try:
                status, _, text = self.transport(self.config.tokenization_url, headers, payload,
                                                min(self.config.timeout_seconds, 15))
                if status != 200:
                    raise ValueError(f"tokenizer HTTP {status}")
                result = json.loads(text)
                count = result.get("count") if isinstance(result, dict) else None
                if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                    raise ValueError("tokenizer returned an invalid count")
                if len(self._token_counts) >= 256:
                    self._token_counts.clear()
                self._token_counts[key] = count
                trace_event("context_token_count", estimator="server_tokenizer", input_tokens=count)
                return count, "server_tokenizer"
            except (OSError, TimeoutError, ValueError, ModelError) as exc:
                self._tokenizer_unavailable = True
                trace_event("context_token_count_fallback", error_type=type(exc).__name__,
                            reason="Tokenizer unavailable; using conservative local estimate")
        return estimate_tokens(body), "heuristic_ascii3_unicode2"

    def next_invocation_id(self, prefix: str = "invocation") -> str:
        return new_id(prefix)

    # ---- internals -----------------------------------------------------

    def _build_body(
        self,
        request: StructuredGenerationRequest,
        schema: dict[str, Any],
        *,
        repair: bool = False,
        repair_error: str | None = None,
        repair_content: str | None = None,
        structured_output_mode: str | None = None,
    ) -> dict[str, Any]:
        if not request.system_prompt_version.strip():
            raise ModelError(
                ModelErrorCode.CONFIG_ERROR,
                "a structured call must name the prompt version it used",
            )
        role_template = template_for(request.role)
        output_mode = structured_output_mode or self.config.structured_output_modes_by_role.get(
            request.role, self.config.structured_output_mode)
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
                        **({} if request.role == "objective_react_agent" and output_mode == "json_schema"
                           else {"json_schema": schema}),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            },
        ]
        if repair:
            repair_message = REPAIR_PROMPT
            if repair_error:
                repair_message = (
                    f"{repair_message}\nValidation error to correct:\n"
                    f"{repair_error}"
                )
            messages[0]["content"] = (
                f"{messages[0]['content']}\n\n{repair_message}"
            )
            if repair_content is not None:
                messages.extend([
                    {"role": "assistant", "content": repair_content},
                    {"role": "user", "content": repair_message},
                ])
        body: dict[str, Any] = {
            "model": self.config.model_id,
            "messages": messages,
        }
        if output_mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "strict": True,
                    "schema": schema,
                },
            }
        elif output_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        body.update(self.config.request_parameters)
        if "max_completion_tokens" not in body and "max_tokens" not in body:
            body["max_tokens"] = self.config.output_reserve_tokens
        return body

    @staticmethod
    def _next_structured_output_mode(
        body: dict[str, Any],
        *,
        current_mode: str,
    ) -> str:
        """Choose the next compatible structured-output mode."""
        response_format = body.get("response_format")
        if (
            isinstance(response_format, dict)
            and response_format.get("type") == "json_schema"
        ):
            return "json_object"
        elif (
            isinstance(response_format, dict)
            and response_format.get("type") == "json_object"
        ):
            return "prompt_only"
        if current_mode == "json_schema":
            return "json_object"
        if current_mode == "json_object":
            return "prompt_only"
        return "prompt_only"

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
        trace_attempt = new_id("trace_attempt")
        trace_event("model_request", attempt_id=trace_attempt, invocation_id=request.invocation_id,
                    role=request.role, stage=request.payload.get("stage"),
                    skill_id=request.skill_id, skill_version=request.skill_version,
                    repaired=repaired, body=body)
        started = time.monotonic()
        headers = {"Content-Type": "application/json"}
        api_key = self.config.api_key()
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            status, response_headers, text = self.transport(
                self.config.base_url, headers, body, self.config.timeout_seconds
            )
        except TimeoutError as exc:
            trace_event("transport_error", attempt_id=trace_attempt, invocation_id=request.invocation_id, role=request.role, error_type="TimeoutError")
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
        except ModelError as exc:
            trace_event("transport_error", attempt_id=trace_attempt, invocation_id=request.invocation_id, role=request.role, error_type=type(exc).__name__, error=str(exc))
            raise
        except Exception as exc:  # transport-level failure
            trace_event("transport_error", attempt_id=trace_attempt, invocation_id=request.invocation_id, role=request.role, error_type=type(exc).__name__)
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
        trace_event("model_response", attempt_id=trace_attempt, invocation_id=request.invocation_id,
                    role=request.role, stage=request.payload.get("stage"), http_status=status,
                    elapsed_ms=int((time.monotonic() - started) * 1000), raw_response=text)
        elapsed = int((time.monotonic() - started) * 1000)
        provider_request_id = _header(response_headers, "x-request-id")
        provider_response = _sanitize_provider_response(text)
        if status == 401 or status == 403:
            record = ModelCallRecord(
                attempt_id=new_id("attempt"),
                role=request.role,
                status="failed",
                http_status=status,
                provider_request_id=provider_request_id,
                error_code=ModelErrorCode.AUTH_ERROR.value,
                error_detail=provider_response,
                elapsed_ms=elapsed,
            )
            self.calls.append(record)
            raise ModelError(
                ModelErrorCode.AUTH_ERROR,
                f"model provider rejected the credentials (HTTP {status})",
                status_code=status,
                provider_request_id=provider_request_id,
                provider_response=provider_response,
            )
        if status == 429:
            record = ModelCallRecord(
                attempt_id=new_id("attempt"),
                role=request.role,
                status="failed",
                http_status=status,
                provider_request_id=provider_request_id,
                error_code=ModelErrorCode.RATE_LIMITED.value,
                error_detail=provider_response,
                elapsed_ms=elapsed,
            )
            self.calls.append(record)
            raise ModelError(
                ModelErrorCode.RATE_LIMITED,
                f"model provider rate limited the call (HTTP 429)",
                status_code=status,
                provider_request_id=provider_request_id,
                provider_response=provider_response,
            )
        if status >= 500:
            record = ModelCallRecord(
                attempt_id=new_id("attempt"),
                role=request.role,
                status="failed",
                http_status=status,
                provider_request_id=provider_request_id,
                error_code=ModelErrorCode.PROVIDER_UNAVAILABLE.value,
                error_detail=provider_response,
                elapsed_ms=elapsed,
            )
            self.calls.append(record)
            raise ModelError(
                ModelErrorCode.PROVIDER_UNAVAILABLE,
                f"model provider is temporarily unavailable (HTTP {status})",
                status_code=status,
                provider_request_id=provider_request_id,
                provider_response=provider_response,
            )
        if status >= 400:
            error_code = ModelErrorCode.BAD_REQUEST
            record = ModelCallRecord(
                attempt_id=new_id("attempt"),
                role=request.role,
                status="failed",
                http_status=status,
                provider_request_id=provider_request_id,
                error_code=error_code.value,
                error_detail=provider_response,
                elapsed_ms=elapsed,
            )
            self.calls.append(record)
            raise ModelError(
                error_code,
                f"model provider rejected the request (HTTP {status})",
                status_code=status,
                provider_request_id=provider_request_id,
                provider_response=provider_response,
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


_SENSITIVE_FIELD = re.compile(
    r"(api[_-]?key|authorization|bearer|password|secret|token)",
    re.IGNORECASE,
)


def _sanitize_provider_response(text: str) -> str:
    """Keep useful provider errors while removing common credentials."""
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if payload is not None:
        payload = _redact_sensitive(payload)
        return json.dumps(payload, ensure_ascii=False)[:4000]
    safe = str(text or "")[:4000]
    safe = re.sub(
        r"(?i)(bearer\s+)[a-z0-9._~+/=-]+",
        r"\1[REDACTED]",
        safe,
    )
    safe = re.sub(
        r"(?i)((?:api[_-]?key|token|secret|password)=)[^&\s]+",
        r"\1[REDACTED]",
        safe,
    )
    return safe


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if _SENSITIVE_FIELD.search(str(key))
                else _redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


_CONTEXT_LENGTH_MARKERS = (
    "maximum context length",
    "context_length_exceeded",
    "prompt contains at least",
    "reduce the length of the input prompt",
)


def _is_context_length_error(error: ModelError) -> bool:
    detail = f"{error.message}\n{error.provider_response or ''}".lower()
    return any(marker in detail for marker in _CONTEXT_LENGTH_MARKERS)


def _compact_context_request(
    request: StructuredGenerationRequest,
    *,
    round_number: int,
) -> StructuredGenerationRequest:
    payload = request.payload
    if not isinstance(payload, dict):
        return request
    return request.model_copy(
        update={
            "payload": _compact_context_payload(
                payload,
                round_number=round_number,
            )
        }
    )


def _compact_context_payload(
    payload: dict[str, Any],
    *,
    round_number: int,
) -> dict[str, Any]:
    compacted = deepcopy(payload)
    string_limit = 240 if round_number == 1 else 120
    preview_limit = 1 if round_number == 1 else 0
    history_limit = 12 if round_number == 1 else 6
    trace_limit = 4 if round_number == 1 else 2

    # Preserve the newest search verbatim. Older searches still identify the
    # provider, query and purpose, which the planner needs to avoid repetition.
    searches = compacted.get("prior_searches")
    if isinstance(searches, list) and len(searches) > 1 and compacted.get("control_mode") != "objective-react-v1":
        compacted["prior_searches"] = [
            {key: item[key] for key in ("provider_id", "query", "purpose", "evidence_need") if key in item}
            if isinstance(item, dict) else item for item in searches[:-1]
        ] + searches[-1:]
    if compacted.get("stage") in {"blind_solve", "counterexample_review"}:
        for field in ("independent_evidence", "evidence_anchors"):
            # NCBI returns the full record plus individual citable fields.
            # Keep both anchor IDs but send each long value only once.
            anchors = compacted.get(field, [])
            by_id = {a.get("anchor_id"): a for a in anchors}
            for record_anchor in anchors:
                rid = str(record_anchor.get("anchor_id", ""))
                if not rid.startswith("anchor.ncbi_gene.") or not rid.endswith(".record"):
                    continue
                raw = record_anchor.get("quoted_text")
                if not isinstance(raw, str):
                    continue
                try:
                    record = json.loads(raw)["summary"]["record"]
                except (KeyError, TypeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                for name, value in record.items():
                    sibling = by_id.get(rid[:-6] + name)
                    if sibling is None or sibling is record_anchor:
                        continue
                    quoted = sibling.get("quoted_text", "")
                    if len(quoted) < 200:
                        continue
                    if quoted == json.dumps(value, ensure_ascii=False, separators=(",", ":")):
                        del sibling["quoted_text"]
                        sibling["value_reference"] = {"anchor_id": rid, "field_path": ["summary", "record", name]}
            for anchor in compacted.get(field, []):
                if not str(anchor.get("anchor_id", "")).endswith(".record"):
                    continue
                quote = anchor.get("quoted_text")
                if not isinstance(quote, str) or len(quote) < 2048:
                    continue
                try:
                    record = json.loads(quote)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                # Factor repeated field names (transcripts, exons, xrefs)
                # into a schema table. Every field value and row is retained.
                schemas = []
                indices = {}
                def encode(value):
                    if isinstance(value, list):
                        return [encode(item) for item in value]
                    if isinstance(value, dict):
                        keys = tuple(sorted(value))
                        if keys not in indices:
                            indices[keys] = len(schemas)
                            schemas.append(keys)
                        return {"s": indices[keys], "v": [encode(value[key]) for key in keys]}
                    return value
                encoded = encode(record)
                projection = {"format": "Objects use s as index into keys and v as corresponding values; arrays are unchanged.",
                              "keys": schemas, "record": encoded}
                projection = _share_repeated_rows(projection)
                if len(json.dumps(projection, ensure_ascii=False)) < len(quote):
                    del anchor["quoted_text"]
                    anchor["structured_evidence"] = projection

    summaries = compacted.get("artifact_summaries")
    if isinstance(summaries, list):
        for summary in summaries:
            if not isinstance(summary, dict):
                continue
            content = summary.get("content_summary")
            if not isinstance(content, dict):
                continue
            previews = content.get("material_previews")
            if isinstance(previews, list):
                content["material_previews"] = [
                    {
                        **item,
                        "preview": str(item.get("preview", ""))[
                            :string_limit
                        ],
                    }
                    for item in previews[:preview_limit]
                    if isinstance(item, dict)
                ]
            rationale = content.get("search_rationale")
            if rationale is not None:
                content["search_rationale"] = str(rationale)[
                    :string_limit
                ]
            content.update(
                _truncate_long_strings(
                    {
                        key: value
                        for key, value in content.items()
                        if key not in {"material_previews", "search_rationale"}
                    },
                    max_chars=string_limit,
                )
            )

    history = compacted.get("retrieval_history")
    if isinstance(history, list):
        compacted["retrieval_history"] = [
            {
                **item,
                "search_rationale": str(
                    item.get("search_rationale", "")
                )[:string_limit],
                "search_trace": list(item.get("search_trace", []))[
                    -trace_limit:
                ],
            }
            for item in history[-history_limit:]
            if isinstance(item, dict)
        ]

    return compacted


def _share_repeated_rows(projection):
    """Represent identical flat rows (e.g. exons shared by transcripts) once."""
    occurrences = {}

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if all(not isinstance(item, (dict, list)) for item in value["v"]):
                key = json.dumps(value, ensure_ascii=False)
                if len(key) >= 80:
                    occurrences.setdefault(key, []).append(value)
            else:
                visit(value["v"])

    visit(projection["record"])
    rows = []
    for matches in occurrences.values():
        if len(matches) < 2:
            continue
        index = len(rows)
        rows.append(matches[0].copy())
        for row in matches:
            row.clear()
            row["r"] = index
    if rows:
        projection["rows"] = rows
        projection["format"] += " An object with r is the complete row at that zero-based index in rows."
    return projection


def _truncate_long_strings(value: Any, *, max_chars: int) -> Any:
    if isinstance(value, dict):
        return {
            key: _truncate_long_strings(item, max_chars=max_chars)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _truncate_long_strings(item, max_chars=max_chars)
            for item in value
        ]
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + "...[truncated]"
    return value


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
