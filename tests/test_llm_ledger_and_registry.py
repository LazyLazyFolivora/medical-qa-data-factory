"""Every physical model attempt is reserved, charged and recorded.

Step 2 of the real-generation-agent plan: a schema repair is two physical
calls and must be billed twice.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from medical_qa_data_factory.domain.common import ContractModel
from medical_qa_data_factory.generation.attempt_ledger import AttemptLedger
from medical_qa_data_factory.generation.run_identity import AttemptStatus
from medical_qa_data_factory.llm.attempted_model import LedgeredModel
from medical_qa_data_factory.llm.config import ModelProviderConfig
from medical_qa_data_factory.llm.contracts import StructuredGenerationRequest
from medical_qa_data_factory.llm.errors import ModelError, ModelErrorCode
from medical_qa_data_factory.llm.registry import ProviderRegistry
from medical_qa_data_factory.llm.providers.openai_compatible import OpenAICompatibleModel


class Answer(ContractModel):
    value: str = Field(min_length=1)


def _request(key: str = "key-1") -> StructuredGenerationRequest:
    return StructuredGenerationRequest(
        invocation_id="inv_1",
        role="question_writer",
        system_prompt_version="prompt-v1",
        payload={},
        idempotency_key=key,
    )


class _Store:
    def __init__(self) -> None:
        self.records: dict[str, list[Any]] = {}

    def list_attempts(self, scope: str, owner_id: str) -> list[Any]:
        return [
            item
            for items in self.records.values()
            for item in items
            if item.scope == scope and item.owner_id == owner_id
        ]

    def list_all_attempts(self) -> list[Any]:
        return [item for items in self.records.values() for item in items]

    def save_attempt(self, attempt: Any) -> str:
        self.records.setdefault(attempt.attempt_id, []).append(attempt)
        return "written"


def _config(**overrides: Any) -> ModelProviderConfig:
    payload: dict[str, Any] = {
        "provider_id": "local",
        "driver": "openai_compatible",
        "base_url": "http://127.0.0.1:1/v1/chat/completions",
        "model_id": "test-model",
        "api_key_env": "TEST_MODEL_KEY",
    }
    payload.update(overrides)
    return ModelProviderConfig(**payload)


# ---- ledgered model ------------------------------------------------------


def test_a_successful_call_is_reserved_and_charged(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "k")
    store = _Store()
    ledger = AttemptLedger(store=store, max_units=10.0)
    model = OpenAICompatibleModel(
        config=_config(),
        transport=lambda url, headers, body, timeout: (
            200,
            {},
            json.dumps(
                {
                    "choices": [
                        {"message": {"content": json.dumps({"value": "x"})}}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            ),
        ),
    )
    wrapped = LedgeredModel(model=model, ledger=ledger, owner_id="t1")
    wrapped.generate_structured(_request(), Answer)
    totals = ledger.totals("run", "t1")
    assert totals.charged_units == 1.0
    assert totals.open_attempts == 0
    assert wrapped.attempt_records[-1].status is AttemptStatus.COMPLETED


def test_a_provider_failure_is_settled_with_the_right_outcome(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "k")
    store = _Store()
    ledger = AttemptLedger(store=store, max_units=10.0)
    model = OpenAICompatibleModel(
        config=_config(),
        transport=lambda url, headers, body, timeout: (401, {}, "{}"),
    )
    wrapped = LedgeredModel(model=model, ledger=ledger, owner_id="t1")
    with pytest.raises(ModelError) as excinfo:
        wrapped.generate_structured(_request(), Answer)
    assert excinfo.value.code is ModelErrorCode.AUTH_ERROR
    record = wrapped.attempt_records[-1]
    assert record.status is AttemptStatus.FAILED
    assert record.error_code == "model_auth_error"


def test_a_schema_repair_is_billed_twice(monkeypatch) -> None:
    """A repair is a second physical call, so it costs a second unit."""
    monkeypatch.setenv("TEST_MODEL_KEY", "k")
    store = _Store()
    ledger = AttemptLedger(store=store, max_units=10.0)
    responses = [
        (200, {}, json.dumps({"choices": [{"message": {"content": "not json"}}]})),
        (
            200,
            {},
            json.dumps(
                {"choices": [{"message": {"content": json.dumps({"value": "ok"})}}]}
            ),
        ),
    ]
    model = OpenAICompatibleModel(
        config=_config(),
        transport=lambda url, headers, body, timeout: responses.pop(0),
    )
    wrapped = LedgeredModel(model=model, ledger=ledger, owner_id="t1")
    result = wrapped.generate_structured(_request(), Answer)
    assert result.parsed["value"] == "ok"
    # Two physical calls, each reserved and charged on its own.
    assert len(model.calls) == 2
    assert len(wrapped.attempt_records) == 2
    assert ledger.totals("run", "t1").charged_units == 2.0


def test_a_ledger_without_budget_blocks_the_call(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "k")
    store = _Store()
    ledger = AttemptLedger(store=store, max_units=0.0)
    model = OpenAICompatibleModel(config=_config())
    wrapped = LedgeredModel(model=model, ledger=ledger, owner_id="t1")
    from medical_qa_data_factory.generation.attempt_ledger import (
        BudgetExhaustedError,
    )

    with pytest.raises(BudgetExhaustedError):
        wrapped.generate_structured(_request(), Answer)
    assert model.calls == []


# ---- registry ------------------------------------------------------------


def _config_file(tmp_path: Path, **overrides: Any) -> Path:
    payload: dict[str, Any] = {
        "default_provider": "local",
        "providers": {
            "local": {
                "driver": "openai_compatible",
                "base_url": "http://127.0.0.1:1/v1/chat/completions",
                "model_id": "test-model",
                "api_key_env": "TEST_MODEL_KEY",
            }
        },
    }
    payload.update(overrides)
    path = tmp_path / "models.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_registry_builds_a_model_port(tmp_path: Path) -> None:
    registry = ProviderRegistry.from_config(_config_file(tmp_path))
    model = registry.build()
    assert isinstance(model, OpenAICompatibleModel)


def test_the_registry_rejects_an_unknown_driver(tmp_path: Path) -> None:
    path = _config_file(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["providers"]["local"]["driver"] = "some_other_driver"
    path.write_text(json.dumps(payload), encoding="utf-8")
    registry = ProviderRegistry.from_config(path)
    with pytest.raises(ModelError) as excinfo:
        registry.build()
    assert excinfo.value.code is ModelErrorCode.UNSUPPORTED_PROVIDER


def test_the_registry_description_never_leaks_the_key(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "super-secret")
    registry = ProviderRegistry.from_config(_config_file(tmp_path))
    described = registry.describe()
    assert "super-secret" not in json.dumps(described)
    assert described[0]["api_key_env"] == "TEST_MODEL_KEY"
    assert described[0]["api_key_present"] is True


def test_the_registry_can_target_a_named_provider(tmp_path: Path) -> None:
    payload = {
        "default_provider": "local",
        "providers": {
            "local": {
                "driver": "openai_compatible",
                "base_url": "http://127.0.0.1:1/a",
                "model_id": "a",
                "api_key_env": "TEST_MODEL_KEY",
            },
            "other": {
                "driver": "openai_compatible",
                "base_url": "http://127.0.0.1:1/b",
                "model_id": "b",
                "api_key_env": "TEST_MODEL_KEY",
            },
        },
    }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    registry = ProviderRegistry.from_config(path)
    model = registry.build("other")
    assert model.config.model_id == "b"


def test_a_transport_override_reaches_the_adapter(tmp_path: Path) -> None:
    import os

    os.environ.setdefault("TEST_MODEL_KEY", "k")
    calls: list[int] = []

    def _transport(url, headers, body, timeout):
        calls.append(1)
        return 200, {}, json.dumps(
            {"choices": [{"message": {"content": json.dumps({"value": "x"})}}]}
        )

    registry = ProviderRegistry.from_config(
        _config_file(tmp_path), transports={"local": _transport}
    )
    model = registry.build()
    model.generate_structured(_request(), Answer)
    assert calls == [1]
