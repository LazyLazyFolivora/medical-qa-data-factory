"""The literature provider registry: no secret in the config, no silent fallback."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from medical_qa_data_factory.literature.errors import LiteratureConfigurationError
from medical_qa_data_factory.literature.providers.weknora import WeKnoraProvider
from medical_qa_data_factory.literature.registry import (
    LiteratureRegistry,
    load_literature_config,
)


def _config(tmp_path: Path, **overrides: Any) -> Path:
    payload: dict[str, Any] = {
        "default_provider": "weknora",
        "providers": {
            "weknora": {
                "driver": "weknora",
                "base_url": "http://127.0.0.1:8080",
                "api_key_env": "WEKNORA_API_KEY",
                "knowledge_base_ids": ["kb-00000001"],
            }
        },
    }
    payload.update(overrides)
    path = tmp_path / "literature.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_valid_configuration_loads(tmp_path: Path) -> None:
    config = load_literature_config(_config(tmp_path))
    provider = config.provider()
    assert provider.provider_id == "weknora"
    assert provider.api_key_env == "WEKNORA_API_KEY"
    assert provider.knowledge_base_ids == ["kb-00000001"]


def test_a_missing_configuration_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(LiteratureConfigurationError) as excinfo:
        load_literature_config(tmp_path / "absent.json")
    assert "no literature configuration" in str(excinfo.value)


def test_a_provider_without_api_key_env_is_rejected(tmp_path: Path) -> None:
    path = _config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["providers"]["weknora"]["api_key_env"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LiteratureConfigurationError) as excinfo:
        load_literature_config(path)
    assert "api_key_env" in str(excinfo.value)


def test_the_registry_builds_a_provider(tmp_path: Path) -> None:
    registry = LiteratureRegistry(config=load_literature_config(_config(tmp_path)))
    provider = registry.build()
    assert isinstance(provider, WeKnoraProvider)


def test_an_unsupported_driver_is_rejected(tmp_path: Path) -> None:
    path = _config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["providers"]["weknora"]["driver"] = "something_else"
    path.write_text(json.dumps(payload), encoding="utf-8")
    registry = LiteratureRegistry(config=load_literature_config(path))
    with pytest.raises(LiteratureConfigurationError) as excinfo:
        registry.build()
    assert "unsupported literature driver" in str(excinfo.value)


def test_the_description_never_leaks_the_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WEKNORA_API_KEY", "super-secret")
    registry = LiteratureRegistry(config=load_literature_config(_config(tmp_path)))
    described = registry.describe()
    assert "super-secret" not in json.dumps(described)
    assert described[0]["api_key_env"] == "WEKNORA_API_KEY"
    assert described[0]["api_key_present"] is True


def test_a_snapshot_label_does_not_claim_a_pinned_provider(tmp_path: Path) -> None:
    path = _config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["providers"]["weknora"]["snapshot_id"] = "corpus-2026-10"
    path.write_text(json.dumps(payload), encoding="utf-8")
    registry = LiteratureRegistry(config=load_literature_config(path))
    provider = registry.build()
    assert provider.config.snapshot_id == "corpus-2026-10"
    capability = provider.preflight()
    assert capability.supports_pinned_snapshot is False
    assert capability.snapshot is not None
    assert capability.snapshot.pinned is False
    assert capability.snapshot.snapshot_id is None