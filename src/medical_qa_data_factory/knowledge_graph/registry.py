"""Provider registry driven by config; no provider branching in the CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from .errors import GraphConfigurationError
from .ports import KnowledgeGraphProvider
from .providers.primekg import PrimeKGSQLiteProvider


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "knowledge_graphs.json"
PRIMEKG_PATH_ENV = "MEDICAL_QA_PRIMEKG_PATH"
CONFIG_PATH_ENV = "MEDICAL_QA_KG_CONFIG"

ProviderFactory = Callable[[str, dict[str, Any]], KnowledgeGraphProvider]


def build_primekg_provider(
    provider_id: str, settings: dict[str, Any], *, project_root: Path | None = None
) -> KnowledgeGraphProvider:
    root = project_root or PROJECT_ROOT
    raw_path = settings.get("database_path")
    if not raw_path:
        raise GraphConfigurationError("primekg provider requires 'database_path'")
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = (root / path).resolve()
    override = os.environ.get(PRIMEKG_PATH_ENV)
    if override:
        path = Path(override).expanduser().resolve()
    raw_catalog_path = settings.get(
        "entity_catalog_path", f"data/cache/{provider_id}-entities.sqlite"
    )
    catalog_path = Path(str(raw_catalog_path))
    if not catalog_path.is_absolute():
        catalog_path = (root / catalog_path).resolve()
    return PrimeKGSQLiteProvider(
        path,
        default_limit=int(settings.get("default_limit", 100)),
        max_limit=int(settings.get("max_limit", 1000)),
        max_hops=int(settings.get("max_hops", 2)),
        query_timeout_ms=int(settings.get("query_timeout_ms", 30000)),
        entity_catalog_path=catalog_path,
        sha256=settings.get("sha256"),
        provider_id=provider_id,
    )


PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    "primekg_sqlite": build_primekg_provider,
}


def load_provider_config(config_path: str | Path | None = None) -> dict[str, Any]:
    if config_path is None:
        override = os.environ.get(CONFIG_PATH_ENV)
        path = Path(override) if override else DEFAULT_CONFIG_PATH
    else:
        path = Path(config_path)
    if not path.exists():
        raise GraphConfigurationError(f"knowledge graph config not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GraphConfigurationError(f"invalid knowledge graph config: {exc}") from exc
    if not isinstance(payload.get("providers"), dict):
        raise GraphConfigurationError("knowledge graph config requires 'providers'")
    payload["_config_path"] = str(path.resolve())
    payload["_project_root"] = str(path.resolve().parent.parent)
    return payload


def provider_from_config(
    provider_id: str | None = None,
    *,
    config_path: str | Path | None = None,
) -> KnowledgeGraphProvider:
    config = load_provider_config(config_path)
    providers = config["providers"]
    target = provider_id or config.get("default_provider")
    if not target:
        raise GraphConfigurationError("no provider selected and no default_provider set")
    settings = providers.get(target)
    if settings is None:
        raise GraphConfigurationError(f"unknown knowledge graph provider: {target}")
    driver = settings.get("driver")
    factory = PROVIDER_FACTORIES.get(str(driver))
    if factory is None:
        raise GraphConfigurationError(f"unknown provider driver: {driver}")
    return factory(target, settings, project_root=Path(config["_project_root"]))


class KnowledgeGraphProviderRegistry:
    """Instantiates and caches providers by id."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or load_provider_config()
        self._instances: dict[str, KnowledgeGraphProvider] = {}

    @property
    def default_provider_id(self) -> str | None:
        value = self._config.get("default_provider")
        return str(value) if value else None

    def available(self) -> list[str]:
        return sorted(self._config.get("providers", {}))

    def get(self, provider_id: str | None = None) -> KnowledgeGraphProvider:
        target = provider_id or self.default_provider_id
        if not target:
            raise GraphConfigurationError("no provider selected")
        if target in self._instances:
            return self._instances[target]
        settings = self._config.get("providers", {}).get(target)
        if settings is None:
            raise GraphConfigurationError(f"unknown knowledge graph provider: {target}")
        driver = settings.get("driver")
        factory = PROVIDER_FACTORIES.get(str(driver))
        if factory is None:
            raise GraphConfigurationError(f"unknown provider driver: {driver}")
        provider = factory(
            target, settings, project_root=Path(self._config.get("_project_root", PROJECT_ROOT))
        )
        self._instances[target] = provider
        return provider

    def close(self) -> None:
        for provider in self._instances.values():
            provider.close()
        self._instances.clear()
