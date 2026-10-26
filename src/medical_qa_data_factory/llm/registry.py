"""Provider registry for real structured model calls.

``structured`` mode assembles its model from here. There is no fallback: if a
provider cannot be built, the run fails with a configuration error rather than
quietly degrading to rules and still claiming it used a model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .ports import LanguageModelPort
from .config import ModelConfig, ModelProviderConfig, load_model_config
from .errors import ModelConfigurationError, UnsupportedProviderError


@dataclass
class ProviderRegistry:
    """Builds model ports from configuration."""

    config: ModelConfig
    #: Overrides for the transport, keyed by provider id. Used by tests to
    #: point an adapter at a local server.
    transports: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(
        cls,
        path: str | Path | None = None,
        *,
        transports: dict[str, Any] | None = None,
    ) -> "ProviderRegistry":
        return cls(
            config=load_model_config(path),
            transports=dict(transports or {}),
        )

    def build(self, provider_id: str | None = None) -> LanguageModelPort:
        """Build the model port for one provider."""
        provider = self.config.provider(provider_id)
        driver = provider.driver
        if driver != "openai_compatible":
            raise UnsupportedProviderError(
                f"unsupported model driver {driver!r} for provider "
                f"{provider.provider_id!r}; supported drivers: "
                "['openai_compatible']"
            )
        from .providers.openai_compatible import OpenAICompatibleModel

        transport = self.transports.get(provider.provider_id)
        return OpenAICompatibleModel(config=provider, transport=transport)

    def provider_config(self, provider_id: str | None = None) -> ModelProviderConfig:
        return self.config.provider(provider_id)

    def describe(self) -> list[dict[str, Any]]:
        """A secret-free summary for the CLI and the workbench."""
        return [
            {
                "provider_id": provider.provider_id,
                "driver": provider.driver,
                "base_url": provider.base_url,
                "model_id": provider.model_id,
                "api_key_env": provider.api_key_env,
                "api_key_present": provider.has_api_key(),
                "supports_strict_json_schema": (
                    provider.supports_strict_json_schema
                ),
                "config_hash": provider.config_hash(),
            }
            for provider in sorted(
                self.config.providers.values(), key=lambda item: item.provider_id
            )
        ]


def build_model_port(
    provider_id: str | None = None,
    *,
    config_path: str | Path | None = None,
    transports: dict[str, Any] | None = None,
) -> LanguageModelPort:
    """Convenience wrapper used by the service and the CLI."""
    return ProviderRegistry.from_config(config_path, transports=transports).build(
        provider_id
    )
