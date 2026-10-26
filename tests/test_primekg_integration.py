"""Read-only integration tests against the real PrimeKG snapshot.

Excluded from the default run. Execute with:
    python -m pytest tests -m primekg_integration -q
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from medical_qa_data_factory.knowledge_graph import EntityLookup, NeighborQuery
from medical_qa_data_factory.knowledge_graph.providers.primekg import (
    PrimeKGSQLiteProvider,
)
from medical_qa_data_factory.knowledge_graph.registry import load_provider_config


pytestmark = pytest.mark.primekg_integration


def _real_database() -> Path | None:
    try:
        config = load_provider_config()
    except Exception:  # noqa: BLE001 - config may be absent in CI
        return None
    settings = config["providers"].get(config.get("default_provider", ""), {})
    if settings.get("driver") != "primekg_sqlite":
        return None
    path = Path(settings["database_path"])
    if not path.is_absolute():
        path = Path(config["_project_root"]) / path
    if not path.exists():
        return None
    return path


DATABASE = _real_database()


def _provider() -> PrimeKGSQLiteProvider:
    if DATABASE is None:
        pytest.skip("real PrimeKG database is not available")
    return PrimeKGSQLiteProvider(DATABASE, sha256=os.environ.get("PRIMEKG_SHA256"))


def test_healthcheck_validates_real_schema() -> None:
    provider = _provider()
    try:
        health = provider.healthcheck()
        assert health.healthy is True
        assert health.detail["relation_count"] == 30
    finally:
        provider.close()


def test_resolve_is_reproducible() -> None:
    provider = _provider()
    try:
        first = provider.resolve_entity(EntityLookup(mention="aspirin"))
        second = provider.resolve_entity(EntityLookup(mention="aspirin"))
        assert [item.entity.canonical_id for item in first.candidates] == [
            item.entity.canonical_id for item in second.candidates
        ]
    finally:
        provider.close()


def test_neighbors_do_not_modify_database() -> None:
    provider = _provider()
    try:
        before = DATABASE.stat()
        entity = provider.resolve_entity(EntityLookup(mention="aspirin")).candidates[0].entity
        provider.neighbors(NeighborQuery(entity=entity, limit=20))
        after = DATABASE.stat()
        assert (after.st_size, after.st_mtime) == (before.st_size, before.st_mtime)
    finally:
        provider.close()
