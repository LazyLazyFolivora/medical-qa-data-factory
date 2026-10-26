"""Provider registry for literature retrieval.

``structured``-style assembly: the provider is built from configuration, and
a missing or unusable configuration fails fast. There is no fallback to a demo
provider, because a run that silently degraded would still be labelled as
having real literature evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..domain.common import ToolSnapshot
from .contracts import (
    LiteratureCapability,
    LiteratureChunkReadRequest,
    LiteratureChunkReadResult,
    LiteratureRetrievalRequest,
    LiteratureRetrievalResult,
)
from .errors import LiteratureConfigurationError


class LiteratureRetrievalPort(Protocol):
    """The three capabilities a real provider must offer."""

    def preflight(self) -> LiteratureCapability: ...

    def search(
        self, request: LiteratureRetrievalRequest
    ) -> LiteratureRetrievalResult: ...

    def read_chunk(
        self, request: LiteratureChunkReadRequest
    ) -> LiteratureChunkReadResult: ...


@dataclass
class LiteratureProviderConfig:
    """One provider's configuration. Never holds the key itself."""

    provider_id: str
    driver: str
    base_url: str
    api_key_env: str
    timeout_seconds: int = 60
    knowledge_base_ids: list[str] = field(default_factory=list)
    #: The pinned corpus version, when the deployment exposes one. Without it
    #: the provider may only produce development candidates.
    snapshot_id: str | None = None

    def api_key(self) -> str:
        import os

        value = os.environ.get(self.api_key_env)
        if not value:
            raise LiteratureConfigurationError(
                f"environment variable {self.api_key_env!r} is not set; the "
                "provider configuration stores only the variable name"
            )
        return value

    def has_api_key(self) -> bool:
        import os

        return bool(os.environ.get(self.api_key_env))


@dataclass
class LiteratureConfig:
    """The set of configured literature providers."""

    default_provider: str
    providers: dict[str, LiteratureProviderConfig]

    def provider(self, provider_id: str | None = None) -> LiteratureProviderConfig:
        selected = provider_id or self.default_provider
        try:
            return self.providers[selected]
        except KeyError as exc:
            raise LiteratureConfigurationError(
                f"unknown literature provider {selected!r}; configured providers: "
                f"{sorted(self.providers)}"
            ) from exc


@dataclass
class LiteratureRegistry:
    """Builds literature ports from configuration."""

    config: LiteratureConfig
    transports: dict[str, Any] = field(default_factory=dict)

    def build(self, provider_id: str | None = None) -> LiteratureRetrievalPort:
        provider = self.config.provider(provider_id)
        if provider.driver != "weknora":
            raise LiteratureConfigurationError(
                f"unsupported literature driver {provider.driver!r} for provider "
                f"{provider.provider_id!r}; supported drivers: ['weknora']"
            )
        from .providers.weknora import WeKnoraConfig, WeKnoraProvider

        adapter_config = WeKnoraConfig(
            base_url=provider.base_url,
            api_key_env=provider.api_key_env,
            timeout_seconds=provider.timeout_seconds,
            provider_name=provider.provider_id,
            knowledge_base_ids=list(provider.knowledge_base_ids),
            snapshot_id=provider.snapshot_id,
        )
        return WeKnoraProvider(
            config=adapter_config,
            transport=self.transports.get(provider.provider_id)
            or _default_transport(provider.provider_id),
        )

    def describe(self) -> list[dict[str, Any]]:
        """A secret-free summary for the CLI and the workbench."""
        return [
            {
                "provider_id": provider.provider_id,
                "driver": provider.driver,
                "base_url": provider.base_url,
                "api_key_env": provider.api_key_env,
                "api_key_present": provider.has_api_key(),
                "knowledge_base_ids": list(provider.knowledge_base_ids),
                "snapshot_id": provider.snapshot_id,
            }
            for provider in sorted(
                self.config.providers.values(), key=lambda item: item.provider_id
            )
        ]


def _default_transport(provider_id: str) -> Any:
    from .providers.weknora import requests_transport

    return requests_transport


def load_literature_config(
    path: str | Path | None = None,
) -> LiteratureConfig:
    """Load the literature provider configuration."""
    import json
    import os

    from .config import LITERATURE_CONFIG_ENV_VAR, LITERATURE_CONFIG_PATH

    target = Path(path or os.environ.get(LITERATURE_CONFIG_ENV_VAR) or LITERATURE_CONFIG_PATH)
    if not target.is_file():
        raise LiteratureConfigurationError(
            f"no literature configuration at {target}; set "
            f"{LITERATURE_CONFIG_ENV_VAR} or create {LITERATURE_CONFIG_PATH}"
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LiteratureConfigurationError(
            f"literature configuration at {target} is not valid JSON: {exc}"
        ) from exc
    providers_payload = payload.get("providers")
    if not isinstance(providers_payload, dict) or not providers_payload:
        raise LiteratureConfigurationError(
            f"literature configuration at {target} must define a non-empty "
            "'providers' object"
        )
    providers: dict[str, LiteratureProviderConfig] = {}
    for provider_id, item in providers_payload.items():
        missing = [
            name
            for name in ("driver", "base_url", "api_key_env")
            if not item.get(name)
        ]
        if missing:
            raise LiteratureConfigurationError(
                f"literature provider {provider_id!r} is missing required "
                f"fields: {missing}"
            )
        providers[provider_id] = LiteratureProviderConfig(
            provider_id=provider_id,
            driver=str(item["driver"]),
            base_url=str(item["base_url"]),
            api_key_env=str(item["api_key_env"]),
            timeout_seconds=int(item.get("timeout_seconds", 60)),
            knowledge_base_ids=list(item.get("knowledge_base_ids") or []),
            snapshot_id=item.get("snapshot_id"),
        )
    default_provider = payload.get("default_provider") or next(iter(providers))
    return LiteratureConfig(
        default_provider=str(default_provider), providers=providers
    )