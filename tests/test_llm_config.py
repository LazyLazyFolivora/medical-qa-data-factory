"""Model configuration must store a variable name, never a secret."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from medical_qa_data_factory.llm.config import (
    ModelConfigurationError,
    load_model_config,
)
from medical_qa_data_factory.llm.errors import ModelError, ModelErrorCode


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "models.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _valid_payload(**overrides) -> dict:
    payload = {
        "default_provider": "local",
        "providers": {
            "local": {
                "driver": "openai_compatible",
                "base_url": "http://127.0.0.1:9/v1/chat/completions",
                "model_id": "test-model",
                "api_key_env": "TEST_MODEL_KEY",
            }
        },
    }
    payload.update(overrides)
    return payload


def test_a_valid_configuration_loads(tmp_path: Path) -> None:
    config = load_model_config(_write(tmp_path, _valid_payload()))
    provider = config.provider()
    assert provider.provider_id == "local"
    assert provider.model_id == "test-model"
    assert provider.api_key_env == "TEST_MODEL_KEY"


def test_an_inline_api_key_is_rejected(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["providers"]["local"]["api_key"] = "sk-should-never-be-stored"
    with pytest.raises(ModelConfigurationError) as excinfo:
        load_model_config(_write(tmp_path, payload))
    assert "inline secret" in str(excinfo.value)


def test_an_inline_secret_nested_deep_is_rejected(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["providers"]["local"]["request_parameters"] = {"token": "abc"}
    with pytest.raises(ModelConfigurationError):
        load_model_config(_write(tmp_path, payload))


def test_a_missing_configuration_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(ModelConfigurationError) as excinfo:
        load_model_config(tmp_path / "absent.json")
    assert "no model configuration" in str(excinfo.value)


def test_a_provider_without_api_key_env_is_rejected(tmp_path: Path) -> None:
    payload = _valid_payload()
    del payload["providers"]["local"]["api_key_env"]
    with pytest.raises(ModelConfigurationError) as excinfo:
        load_model_config(_write(tmp_path, payload))
    assert "api_key_env" in str(excinfo.value)


def test_a_self_hosted_provider_may_disable_authentication(tmp_path: Path) -> None:
    payload = _valid_payload()
    provider = payload["providers"]["local"]
    provider.pop("api_key_env")
    provider["auth_mode"] = "none"
    provider["structured_output_mode"] = "json_object"
    provider["supports_strict_json_schema"] = False

    config = load_model_config(_write(tmp_path, payload))

    selected = config.provider()
    assert selected.auth_mode == "none"
    assert selected.api_key_env is None
    assert selected.has_api_key() is True
    assert selected.structured_output_mode == "json_object"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("auth_mode", "basic"),
        ("structured_output_mode", "yaml"),
    ],
)
def test_invalid_self_hosted_adapter_modes_are_rejected(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    payload = _valid_payload()
    payload["providers"]["local"][field] = value

    with pytest.raises(ModelConfigurationError):
        load_model_config(_write(tmp_path, payload))


def test_the_api_key_is_read_from_the_environment_at_call_time(
    tmp_path: Path, monkeypatch
) -> None:
    config = load_model_config(_write(tmp_path, _valid_payload()))
    provider = config.provider()
    monkeypatch.delenv("TEST_MODEL_KEY", raising=False)
    assert not provider.has_api_key()
    with pytest.raises(ModelConfigurationError) as excinfo:
        provider.api_key()
    assert "TEST_MODEL_KEY" in str(excinfo.value)
    monkeypatch.setenv("TEST_MODEL_KEY", "secret-value")
    assert provider.api_key() == "secret-value"


def test_the_config_hash_does_not_contain_the_key(tmp_path: Path, monkeypatch) -> None:
    config = load_model_config(_write(tmp_path, _valid_payload()))
    provider = config.provider()
    monkeypatch.setenv("TEST_MODEL_KEY", "secret-value")
    digest = provider.config_hash()
    assert "secret-value" not in digest
    # The hash is stable regardless of the key's value.
    monkeypatch.setenv("TEST_MODEL_KEY", "another-value")
    assert provider.config_hash() == digest


def test_an_unknown_provider_is_rejected(tmp_path: Path) -> None:
    config = load_model_config(_write(tmp_path, _valid_payload()))
    with pytest.raises(ModelConfigurationError) as excinfo:
        config.provider("absent")
    assert "unknown model provider" in str(excinfo.value)


def test_the_default_provider_may_be_omitted(tmp_path: Path) -> None:
    payload = _valid_payload()
    del payload["default_provider"]
    config = load_model_config(_write(tmp_path, payload))
    assert config.default_provider == "local"


def test_the_default_provider_must_exist(tmp_path: Path) -> None:
    payload = _valid_payload(default_provider="absent")
    with pytest.raises(ModelConfigurationError) as excinfo:
        load_model_config(_write(tmp_path, payload))
    assert "default_provider" in str(excinfo.value)


def test_the_error_code_for_a_configuration_problem_is_stable() -> None:
    error = ModelError(ModelErrorCode.CONFIG_ERROR, "bad config")
    assert error.code is ModelErrorCode.CONFIG_ERROR
    assert error.retryable is False
    assert error.as_dict()["code"] == "model_config_error"
