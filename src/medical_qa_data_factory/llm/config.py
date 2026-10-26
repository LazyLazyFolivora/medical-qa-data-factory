"""Model provider configuration.

The configuration never stores a secret. It stores the *name* of the
environment variable that holds the key, and the key itself is read at call
time. A configuration file that contains an API key must therefore never be
written by this project, and a configuration that looks like it contains one is
rejected rather than silently ignored.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ModelConfigurationError


#: Environment variable that points at the configuration file.
CONFIG_ENV_VAR = "MEDICAL_QA_MODEL_CONFIG"

#: Default configuration path, relative to the project root.
DEFAULT_CONFIG_PATH = "config/models.json"


@dataclass(frozen=True)
class ModelProviderConfig:
    """One provider's configuration. Never holds the key itself."""

    provider_id: str
    driver: str
    base_url: str
    model_id: str
    #: The name of the environment variable holding the API key.
    api_key_env: str
    timeout_seconds: int = 60
    max_retries: int = 1
    #: Whether the provider honours a strict JSON Schema response format.
    supports_strict_json_schema: bool = True
    request_parameters: dict[str, Any] = field(default_factory=dict)

    def api_key(self) -> str:
        """Read the key from the environment at call time."""
        value = os.environ.get(self.api_key_env)
        if not value:
            raise ModelConfigurationError(
                f"environment variable {self.api_key_env!r} is not set; "
                "the provider configuration stores only the variable name"
            )
        return value

    def has_api_key(self) -> bool:
        return bool(os.environ.get(self.api_key_env))

    def config_hash(self) -> str:
        """Hash of everything that affects the result. The key is excluded."""
        payload = {
            "provider_id": self.provider_id,
            "driver": self.driver,
            "base_url": self.base_url,
            "model_id": self.model_id,
            "api_key_env": self.api_key_env,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "supports_strict_json_schema": self.supports_strict_json_schema,
            "request_parameters": self.request_parameters,
        }
        text = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def model_config_id(self) -> str:
        return f"{self.provider_id}:{self.model_id}"


@dataclass(frozen=True)
class ModelConfig:
    """The set of configured providers plus the default selection."""

    default_provider: str
    providers: dict[str, ModelProviderConfig]

    def provider(self, provider_id: str | None = None) -> ModelProviderConfig:
        selected = provider_id or self.default_provider
        try:
            return self.providers[selected]
        except KeyError as exc:
            known = sorted(self.providers)
            raise ModelConfigurationError(
                f"unknown model provider {selected!r}; configured providers: {known}"
            ) from exc


#: Field names that must never appear in a configuration file. A file that
#: contains one of them is rejected: storing a key in a file is exactly what
#: this module exists to prevent.
FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "key",
        "secret",
        "token",
        "access_token",
        "password",
        "authorization",
    }
)


def _reject_inline_secrets(payload: Any, path: str = "") -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in FORBIDDEN_FIELDS:
                raise ModelConfigurationError(
                    f"configuration field {key!r} at {path or '<root>'} looks like an "
                    "inline secret; store the environment variable name in "
                    "'api_key_env' instead"
                )
            _reject_inline_secrets(value, f"{path}.{key}" if path else str(key))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            _reject_inline_secrets(item, f"{path}[{index}]")


def _provider_from_payload(provider_id: str, payload: dict[str, Any]) -> ModelProviderConfig:
    missing = [
        name
        for name in ("driver", "base_url", "model_id", "api_key_env")
        if not payload.get(name)
    ]
    if missing:
        raise ModelConfigurationError(
            f"provider {provider_id!r} is missing required fields: {missing}"
        )
    return ModelProviderConfig(
        provider_id=provider_id,
        driver=str(payload["driver"]),
        base_url=str(payload["base_url"]),
        model_id=str(payload["model_id"]),
        api_key_env=str(payload["api_key_env"]),
        timeout_seconds=int(payload.get("timeout_seconds", 60)),
        max_retries=int(payload.get("max_retries", 1)),
        supports_strict_json_schema=bool(
            payload.get("supports_strict_json_schema", True)
        ),
        request_parameters=dict(payload.get("request_parameters") or {}),
    )


def load_model_config(path: str | Path | None = None) -> ModelConfig:
    """Load the model configuration, rejecting inline secrets."""
    target = Path(
        path
        or os.environ.get(CONFIG_ENV_VAR)
        or DEFAULT_CONFIG_PATH
    )
    if not target.is_file():
        raise ModelConfigurationError(
            f"no model configuration at {target}; set {CONFIG_ENV_VAR} or create "
            f"{DEFAULT_CONFIG_PATH}"
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ModelConfigurationError(
            f"model configuration at {target} is not valid JSON: {exc}"
        ) from exc
    _reject_inline_secrets(payload)
    if not isinstance(payload, dict):
        raise ModelConfigurationError(
            f"model configuration at {target} must be a JSON object"
        )
    providers_payload = payload.get("providers")
    if not isinstance(providers_payload, dict) or not providers_payload:
        raise ModelConfigurationError(
            f"model configuration at {target} must define a non-empty 'providers' object"
        )
    default_provider = payload.get("default_provider")
    if not default_provider:
        default_provider = next(iter(providers_payload))
    providers = {
        provider_id: _provider_from_payload(provider_id, item)
        for provider_id, item in providers_payload.items()
    }
    return ModelConfig(default_provider=str(default_provider), providers=providers)