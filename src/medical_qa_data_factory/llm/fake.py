"""An offline model double that records every invocation.

It returns a preset payload per role so tests can exercise the graph without
network access, while still proving that writer and verifier call the model
with different roles and different payloads.
"""
from __future__ import annotations

from typing import Any

from ..domain.common import ContractModel, new_id
from .contracts import StructuredGenerationRequest, StructuredGenerationResult

BaseModelLike = ContractModel


class FakeStructuredModel:
    """Deterministic stand-in for a future real provider."""

    def __init__(
        self,
        *,
        responses: dict[str, Any] | None = None,
        model_config_id: str = "fake-model-v1",
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self.responses: dict[str, Any] = dict(responses or {})
        self.model_config_id = model_config_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.invocations: list[StructuredGenerationRequest] = []
        self.results: list[StructuredGenerationResult] = []
        self.usage_records: list[dict[str, Any]] = []

    def set_response(self, role: str, payload: Any) -> None:
        self.responses[role] = payload

    def calls_for(self, role: str) -> list[StructuredGenerationRequest]:
        return [item for item in self.invocations if item.role == role]

    def payloads_for(self, role: str) -> list[dict[str, Any]]:
        return [item.payload for item in self.invocations if item.role == role]

    def generate_structured(
        self,
        request: StructuredGenerationRequest,
        response_model: type[ContractModel],
    ) -> StructuredGenerationResult:
        self.invocations.append(request)
        if request.role not in self.responses:
            raise KeyError(
                f"no fake response registered for role {request.role!r}; "
                f"known roles: {sorted(self.responses)}"
            )
        raw = self.responses[request.role]
        payload = raw(request) if callable(raw) else raw
        if isinstance(payload, BaseModelLike):
            payload = payload.model_dump(mode="json")
        # Validate through the requested contract so a fake cannot smuggle in
        # fields that a real structured-output call would reject.
        parsed = response_model.model_validate(payload).model_dump(mode="json")
        usage = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model_config_id": self.model_config_id,
        }
        result = StructuredGenerationResult(
            invocation_id=request.invocation_id,
            role=request.role,
            parsed=parsed,
            usage=usage,
            model_config_id=self.model_config_id,
        )
        self.results.append(result)
        self.usage_records.append(
            {"invocation_id": request.invocation_id, "role": request.role, **usage}
        )
        return result

    def next_invocation_id(self, prefix: str = "invocation") -> str:
        return new_id(prefix)
