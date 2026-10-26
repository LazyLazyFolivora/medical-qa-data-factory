"""Evidence first, path second, then reload and re-validate.

Step 3 of the real-generation-agent plan. A path that cannot be rebuilt from
the persisted evidence registry is not a path.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from medical_qa_data_factory.domain import (
    EntityRef,
    EvidenceDirection,
    KGEvidence,
    LinkStatus,
)
from medical_qa_data_factory.domain.common import utc_now
from medical_qa_data_factory.generation.contracts import EvidencePath
from medical_qa_data_factory.generation.path_persistence import (
    PathPersistResult,
    PathPersister,
    PathValidationError,
    build_evidence_path,
    validate_evidence_path,
)
from medical_qa_data_factory.knowledge_graph.contracts import (
    GraphEdge,
    GraphEntity,
    OrderedPath,
    OrderedPathStep,
)
from medical_qa_data_factory.repository import LocalArtifactRepository


def _entity(canonical_id: str, name: str) -> GraphEntity:
    return GraphEntity(
        provider_id="fake",
        canonical_id=canonical_id,
        name=name,
        entity_type="disease",
    )


def _ref(canonical_id: str, name: str) -> EntityRef:
    return EntityRef(
        entity_ref_id=f"entity-{canonical_id}",
        raw_mention=name,
        canonical_name=name,
        canonical_id=canonical_id,
        namespace="fake",
        entity_type="disease",
        aliases=[],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="fake",
    )


ASPIRIN = _entity("drug:aspirin", "aspirin")
HEADACHE = _entity("disease:headache", "headache")
MIGRAINE = _entity("disease:migraine", "migraine")


def _edge(head: GraphEntity, relation: str, tail: GraphEntity, edge_id: str) -> GraphEdge:
    return GraphEdge(
        provider_id="fake",
        edge_id=edge_id,
        head=head,
        relation=relation,
        tail=tail,
    )


def _evidence(edge: GraphEdge, evidence_id: str) -> KGEvidence:
    return KGEvidence(
        evidence_id=evidence_id,
        head=_ref(edge.head.canonical_id, edge.head.name),
        relation=edge.relation,
        tail=_ref(edge.tail.canonical_id, edge.tail.name),
        direction=EvidenceDirection.FORWARD,
        source_database="fake",
        source_record_id=edge.edge_id,
        confidence=1.0,
        retrieval_action={"provider_id": "fake"},
        retrieved_at=utc_now(),
    )


def _ordered_path(steps: list[OrderedPathStep], edge_ids: list[str]) -> OrderedPath:
    return OrderedPath(
        path_id="opath.abc",
        provider_id="fake",
        snapshot_id="snap-1",
        start_entity_id=steps[0].from_entity_id,
        end_entity_id=steps[-1].to_entity_id,
        steps=steps,
        intermediate_entity_ids=[item.to_entity_id for item in steps[:-1]],
        source_edge_ids=edge_ids,
        path_hash="hash",
        query_trace_ids=[item.query_trace_id for item in steps],
    )


def _two_hop() -> tuple[OrderedPath, list[KGEvidence]]:
    first = _edge(ASPIRIN, "treats", HEADACHE, "e1")
    second = _edge(HEADACHE, "indication", MIGRAINE, "e2")
    steps = [
        OrderedPathStep(
            position=0,
            edge_id="e1",
            traversal_direction="forward",
            from_entity_id=ASPIRIN.canonical_id,
            relation="treats",
            to_entity_id=HEADACHE.canonical_id,
            query_trace_id="q1",
        ),
        OrderedPathStep(
            position=1,
            edge_id="e2",
            traversal_direction="forward",
            from_entity_id=HEADACHE.canonical_id,
            relation="indication",
            to_entity_id=MIGRAINE.canonical_id,
            query_trace_id="q2",
        ),
    ]
    return _ordered_path(steps, ["e1", "e2"]), [
        _evidence(first, "kg.e1"),
        _evidence(second, "kg.e2"),
    ]


def _three_hop() -> tuple[OrderedPath, list[KGEvidence]]:
    pain = _entity("symptom:pain", "pain")
    first = _edge(ASPIRIN, "treats", HEADACHE, "e1")
    second = _edge(HEADACHE, "indication", MIGRAINE, "e2")
    third = _edge(MIGRAINE, "symptom", pain, "e3")
    steps = [
        OrderedPathStep(
            position=0,
            edge_id="e1",
            traversal_direction="forward",
            from_entity_id=ASPIRIN.canonical_id,
            relation="treats",
            to_entity_id=HEADACHE.canonical_id,
            query_trace_id="q1",
        ),
        OrderedPathStep(
            position=1,
            edge_id="e2",
            traversal_direction="forward",
            from_entity_id=HEADACHE.canonical_id,
            relation="indication",
            to_entity_id=MIGRAINE.canonical_id,
            query_trace_id="q2",
        ),
        OrderedPathStep(
            position=2,
            edge_id="e3",
            traversal_direction="forward",
            from_entity_id=MIGRAINE.canonical_id,
            relation="symptom",
            to_entity_id=pain.canonical_id,
            query_trace_id="q3",
        ),
    ]
    return _ordered_path(steps, ["e1", "e2", "e3"]), [
        _evidence(first, "kg.e1"),
        _evidence(second, "kg.e2"),
        _evidence(third, "kg.e3"),
    ]


def _persister(tmp_path: Path) -> tuple[PathPersister, LocalArtifactRepository]:
    repository = LocalArtifactRepository(tmp_path / "ws")
    persister = PathPersister(
        repository=repository,
        episode_id="ep1",
        task_id="t1",
        bundle_id="b1",
    )
    return persister, repository


# ---- the happy path ------------------------------------------------------


def test_a_two_hop_path_is_persisted_and_validated(tmp_path: Path) -> None:
    persister, repository = _persister(tmp_path)
    path, evidence = _two_hop()
    result = persister.persist([path], evidence)
    assert result.rejected == []
    assert len(result.paths) == 1
    assert result.paths[0].steps[0].evidence_id == "kg.e1"
    # The evidence is on disk before the path refers to it.
    assert repository.list_record_ids("kg_evidence") == ["kg.e1", "kg.e2"]
    assert repository.list_record_ids("paths") == ["opath.abc"]


def test_a_three_hop_path_is_persisted(tmp_path: Path) -> None:
    persister, repository = _persister(tmp_path)
    path, evidence = _three_hop()
    result = persister.persist([path], evidence)
    assert result.rejected == []
    assert len(result.paths[0].steps) == 3
    assert result.paths[0].intermediate_entity_ids == [
        HEADACHE.canonical_id,
        MIGRAINE.canonical_id,
    ]


def test_the_path_is_reloadable_from_the_repository(tmp_path: Path) -> None:
    persister, repository = _persister(tmp_path)
    path, evidence = _two_hop()
    persister.persist([path], evidence)
    reloaded = repository.load("paths", "opath.abc", "opath.abc.v1", EvidencePath)
    assert len(reloaded.steps) == 2
    assert reloaded.source_evidence_ids == ["kg.e1", "kg.e2"]


# ---- rejections ----------------------------------------------------------


def test_a_path_whose_evidence_was_never_persisted_is_rejected(
    tmp_path: Path,
) -> None:
    persister, _ = _persister(tmp_path)
    path, evidence = _two_hop()
    # Only the first edge's evidence is stored.
    result = persister.persist([path], evidence[:1])
    assert result.paths == []
    assert result.rejected
    reason = result.rejected[0][1]
    assert "kg.e2" in reason or "evidence" in reason


def test_a_broken_chain_is_rejected(tmp_path: Path) -> None:
    persister, _ = _persister(tmp_path)
    path, evidence = _two_hop()
    # The break is in the graph-layer walk: the first step ends somewhere the
    # second step does not start.
    broken = path.model_copy(
        update={
            "steps": [
                path.steps[0].model_copy(
                    update={"to_entity_id": "disease:other"}
                ),
                path.steps[1],
            ]
        }
    )
    result = persister.persist([broken], evidence)
    assert result.paths == []
    assert result.rejected


def test_a_step_whose_relation_disagrees_with_the_evidence_is_rejected(
    tmp_path: Path,
) -> None:
    persister, _ = _persister(tmp_path)
    path, evidence = _two_hop()
    wrong = path.model_copy(
        update={
            "steps": [
                path.steps[0].model_copy(update={"relation": "contraindication"}),
                path.steps[1],
            ]
        }
    )
    result = persister.persist([wrong], evidence)
    assert result.paths == []
    # The walk is rejected before it is published, whether the contract or the
    # re-validation catches it.
    assert result.rejected


def test_a_step_whose_direction_disagrees_with_the_evidence_is_rejected(
    tmp_path: Path,
) -> None:
    persister, _ = _persister(tmp_path)
    path, evidence = _two_hop()
    wrong = path.model_copy(
        update={
            "steps": [
                path.steps[0].model_copy(
                    update={
                        "traversal_direction": "reverse",
                        "from_entity_id": HEADACHE.canonical_id,
                        "to_entity_id": ASPIRIN.canonical_id,
                    }
                ),
                path.steps[1],
            ]
        }
    )
    result = persister.persist([wrong], evidence)
    assert result.paths == []
    assert result.rejected


# ---- validation in isolation --------------------------------------------


def test_validate_accepts_a_well_formed_path() -> None:
    path, evidence = _two_hop()
    built = build_evidence_path(
        path,
        episode_id="ep1",
        task_id="t1",
        bundle_id="b1",
        evidence_by_edge={"e1": evidence[0], "e2": evidence[1]},
    )
    validate_evidence_path(built, {item.evidence_id: item for item in evidence})


def test_validate_rejects_unknown_evidence() -> None:
    path, evidence = _two_hop()
    built = build_evidence_path(
        path,
        episode_id="ep1",
        task_id="t1",
        bundle_id="b1",
        evidence_by_edge={"e1": evidence[0], "e2": evidence[1]},
    )
    with pytest.raises(PathValidationError) as excinfo:
        validate_evidence_path(built, {"kg.e1": evidence[0]})
    assert "kg.e2" in str(excinfo.value)


def test_validate_rejects_a_reused_evidence_id() -> None:
    path, evidence = _two_hop()
    built = build_evidence_path(
        path,
        episode_id="ep1",
        task_id="t1",
        bundle_id="b1",
        evidence_by_edge={"e1": evidence[0], "e2": evidence[1]},
    )
    reused = built.model_copy(update={"source_evidence_ids": ["kg.e1", "kg.e1"]})
    with pytest.raises(PathValidationError):
        validate_evidence_path(reused, {item.evidence_id: item for item in evidence})


def test_the_evidence_path_contract_allows_n_hops() -> None:
    """The path schema itself must not cap a walk at two hops."""
    path, evidence = _three_hop()
    built = build_evidence_path(
        path,
        episode_id="ep1",
        task_id="t1",
        bundle_id="b1",
        evidence_by_edge={"e1": evidence[0], "e2": evidence[1], "e3": evidence[2]},
    )
    assert len(built.steps) == 3