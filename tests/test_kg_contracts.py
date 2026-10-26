"""Contract-level invariants for the provider-neutral graph models."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from medical_qa_data_factory.knowledge_graph import (
    EntityLookup,
    GraphEntity,
    NeighborQuery,
    PathQuery,
)
from medical_qa_data_factory.knowledge_graph.contracts import MAX_HOPS_CEILING


def test_graph_entity_exposes_a_domain_ref() -> None:
    entity = GraphEntity(
        provider_id="primekg", native_id="1191", canonical_id="primekg:drug:1191", name="aspirin"
    )
    ref = entity.ref()
    assert ref.canonical_id == "primekg:drug:1191"
    assert ref.source == "primekg"


def test_entity_lookup_limit_bounds() -> None:
    with pytest.raises(ValidationError):
        EntityLookup(mention="aspirin", limit=0)
    with pytest.raises(ValidationError):
        EntityLookup(mention="aspirin", limit=101)


def test_path_query_caps_at_the_system_ceiling() -> None:
    entity = GraphEntity(
        provider_id="fake", canonical_id="fake:drug:1", name="aspirin"
    )
    with pytest.raises(ValidationError):
        PathQuery(start=entity, max_hops=MAX_HOPS_CEILING + 1)


def test_path_query_allows_up_to_the_ceiling() -> None:
    entity = GraphEntity(
        provider_id="fake", canonical_id="fake:drug:1", name="aspirin"
    )
    assert PathQuery(start=entity, max_hops=MAX_HOPS_CEILING).max_hops == (
        MAX_HOPS_CEILING
    )


def test_neighbor_query_limit_bounds() -> None:
    entity = GraphEntity(
        provider_id="fake", canonical_id="fake:drug:1", name="aspirin"
    )
    with pytest.raises(ValidationError):
        NeighborQuery(entity=entity, limit=1001)
