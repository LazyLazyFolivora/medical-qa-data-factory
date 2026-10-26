"""Per-hop needs, round-trip verification, and support assessment.

Step 4 of the real-generation-agent plan. A chunk becomes gold text evidence
only after it survives an independent read-back with a matching content hash.
"""
from __future__ import annotations

from typing import Any

import pytest

from medical_qa_data_factory.domain import (
    EntityRef,
    EvidenceDirection,
    KGEvidence,
    LinkStatus,
)
from medical_qa_data_factory.domain.common import utc_now
from medical_qa_data_factory.generation.evidence_fusion import (
    EvidenceSupportAssessment,
    SupportVerdict,
    assess_support,
    contradicting_evidence_ids,
    fuse_retrieval,
    supporting_evidence_ids,
)
from medical_qa_data_factory.generation.evidence_needs import (
    build_needs_for_path,
    build_query_for_need,
    needs_for_paths,
    relation_phrases,
)
from medical_qa_data_factory.literature.contracts import (
    LiteratureCallStatus,
    LiteratureChunkReadRequest,
    LiteratureChunkReadResult,
    LiteratureEvidenceNeed,
    LiteratureRetrievalRequest,
    LiteratureRetrievalResult,
    LiteratureSnapshot,
    RetrievedChunk,
)


def _ref(canonical_id: str, name: str, aliases: list[str] | None = None) -> EntityRef:
    return EntityRef(
        entity_ref_id=f"entity-{canonical_id}",
        raw_mention=name,
        canonical_name=name,
        canonical_id=canonical_id,
        namespace="primekg",
        entity_type="disease",
        aliases=aliases or [],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="primekg",
    )


ASPIRIN = _ref("drug:aspirin", "aspirin", ["ASA"])
HEADACHE = _ref("disease:headache", "headache", ["cephalalgia"])
MIGRAINE = _ref("disease:migraine", "migraine")
PAIN = _ref("symptom:pain", "pain")


def _evidence(evidence_id: str, head: EntityRef, relation: str, tail: EntityRef) -> KGEvidence:
    return KGEvidence(
        evidence_id=evidence_id,
        head=head,
        relation=relation,
        tail=tail,
        direction=EvidenceDirection.FORWARD,
        source_database="primekg",
        source_record_id=evidence_id.replace("kg.", ""),
        confidence=1.0,
        retrieval_action={"provider_id": "primekg"},
        retrieved_at=utc_now(),
    )


class _Step:
    def __init__(self, position: int, evidence_id: str, relation: str):
        self.position = position
        self.evidence_id = evidence_id
        self.relation = relation
        self.traversal_direction = "forward"
        self.from_entity_id = "x"
        self.to_entity_id = "y"
        self.provider_id = "primekg"
        self.snapshot_id = "snap-1"
        self.query_trace_id = f"q{position}"


class _Path:
    def __init__(self, path_id: str, steps: list[_Step]):
        self.path_id = path_id
        self.steps = steps
        self.path_version = f"{path_id}.v1"
        self.snapshot_id = "snap-1"


def _evidence_registry() -> dict[str, KGEvidence]:
    return {
        "kg.e1": _evidence("kg.e1", ASPIRIN, "treats", HEADACHE),
        "kg.e2": _evidence("kg.e2", HEADACHE, "indication", MIGRAINE),
        "kg.e3": _evidence("kg.e3", MIGRAINE, "symptom", PAIN),
    }


def _path() -> _Path:
    return _Path(
        "opath.1",
        [
            _Step(0, "kg.e1", "treats"),
            _Step(1, "kg.e2", "indication"),
            _Step(2, "kg.e3", "symptom"),
        ],
    )


# ---- per-hop needs -------------------------------------------------------


def test_every_hop_gets_its_own_need() -> None:
    needs = build_needs_for_path(
        _path(),
        run_id="run1",
        episode_id="ep1",
        evidence_by_id=_evidence_registry(),
    )
    assert len(needs) == 3
    assert [item.hop_position for item in needs] == [0, 1, 2]


def test_a_need_binds_its_hop_and_its_edge() -> None:
    needs = build_needs_for_path(
        _path(),
        run_id="run1",
        episode_id="ep1",
        evidence_by_id=_evidence_registry(),
    )
    first = needs[0]
    assert first.kg_evidence_id == "kg.e1"
    assert first.hop_position == 0
    assert first.path_id == "opath.1"
    assert first.subject_entity_id == "drug:aspirin"
    assert first.object_entity_id == "disease:headache"
    assert first.canonical_relation == "treats"


def test_a_need_carries_the_entity_aliases() -> None:
    needs = build_needs_for_path(
        _path(),
        run_id="run1",
        episode_id="ep1",
        evidence_by_id=_evidence_registry(),
    )
    assert needs[0].subject_aliases == ["ASA"]
    assert needs[0].object_aliases == ["cephalalgia"]


def test_a_need_carries_a_dependency_digest() -> None:
    needs = build_needs_for_path(
        _path(),
        run_id="run1",
        episode_id="ep1",
        evidence_by_id=_evidence_registry(),
    )
    assert all(item.dependency_digest for item in needs)
    assert len({item.dependency_digest for item in needs}) == 3


def test_the_same_path_produces_the_same_digest() -> None:
    first = build_needs_for_path(
        _path(),
        run_id="run1",
        episode_id="ep1",
        evidence_by_id=_evidence_registry(),
    )
    second = build_needs_for_path(
        _path(),
        run_id="run1",
        episode_id="ep1",
        evidence_by_id=_evidence_registry(),
    )
    assert [item.dependency_digest for item in first] == [
        item.dependency_digest for item in second
    ]


def test_a_step_without_persisted_evidence_produces_no_need() -> None:
    path = _Path("opath.2", [_Step(0, "kg.missing", "treats")])
    needs = build_needs_for_path(
        path, run_id="run1", episode_id="ep1", evidence_by_id=_evidence_registry()
    )
    assert needs == []


def test_needs_for_several_paths_are_deduplicated() -> None:
    needs = needs_for_paths(
        [_path(), _path()],
        run_id="run1",
        episode_id="ep1",
        evidence_by_id=_evidence_registry(),
    )
    assert len(needs) == 3


def test_the_query_names_both_entities_and_the_relation() -> None:
    need = build_needs_for_path(
        _path(),
        run_id="run1",
        episode_id="ep1",
        evidence_by_id=_evidence_registry(),
    )[0]
    query = build_query_for_need(need)
    # The query uses the canonical names, because that is what a paper writes.
    assert "aspirin" in query
    assert "headache" in query
    assert need.relation_phrases[0] in query


def test_an_unknown_relation_still_gets_a_usable_phrasing() -> None:
    assert relation_phrases("some_unknown_relation") == [
        "some unknown relation",
        "some_unknown_relation",
    ]


# ---- the read-back round trip -------------------------------------------


def _snapshot(pinned: bool = True) -> LiteratureSnapshot:
    return LiteratureSnapshot(
        provider="weknora",
        capability_version="weknora-v0.10",
        snapshot_id="corpus-1" if pinned else None,
        knowledge_base_ids=["kb-1"],
        pinned=pinned,
        retrieved_at=utc_now(),
    )


def _retrieval(chunks: list[RetrievedChunk]) -> LiteratureRetrievalResult:
    return LiteratureRetrievalResult(
        request_id="req-1",
        need_id="need-1",
        status=(
            LiteratureCallStatus.SUCCESS if chunks else LiteratureCallStatus.EMPTY
        ),
        chunks=chunks,
        snapshot=_snapshot(),
        trace_id="trace-1",
    )


def _chunk(chunk_id: str, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id="doc-1",
        content=content,
        title="A paper",
        chunk_index=1,
        start_at=10,
        end_at=60,
        score=0.9,
    )


def _read_back(
    chunk_id: str, content: str | None = "", **overrides: Any
) -> LiteratureChunkReadResult:
    payload: dict[str, Any] = {
        "request_id": "read-1",
        "chunk_id": chunk_id,
        "status": LiteratureCallStatus.SUCCESS,
        "document_id": "doc-1",
        "content": content,
        "chunk_index": 1,
        "start_at": 10,
        "end_at": 60,
        "snapshot": _snapshot(),
        "trace_id": "read-trace-1",
    }
    payload.update(overrides)
    return LiteratureChunkReadResult(**payload)


def _need() -> LiteratureEvidenceNeed:
    return LiteratureEvidenceNeed(
        need_id="need-1",
        run_id="run1",
        episode_id="ep1",
        path_id="opath.1",
        hop_position=0,
        kg_evidence_id="kg.e1",
        subject_entity_id="drug:aspirin",
        subject_name="aspirin",
        subject_aliases=["ASA"],
        canonical_relation="treats",
        relation_phrases=["treatment"],
        object_entity_id="disease:headache",
        object_name="headache",
        object_aliases=["cephalalgia"],
        information_need="aspirin treatment headache",
        dependency_digest="digest-1",
    )


def test_a_chunk_that_survives_the_read_back_becomes_evidence() -> None:
    text = "Aspirin (ASA) is used in the treatment of headache and cephalalgia."
    retrieval = _retrieval([_chunk("c1", text)])
    outcome = fuse_retrieval(
        retrieval,
        {"c1": _read_back("c1", text)},
        need=_need(),
        provider="weknora",
        entity_link_version="link-v1",
        retrieval_query="q",
    )
    assert len(outcome.evidence) == 1
    evidence = outcome.evidence[0]
    assert evidence.round_trip_verified is True
    assert evidence.content_hash
    assert evidence.read_trace_id == "read-trace-1"
    assert outcome.unverified == []


def test_a_chunk_that_was_never_read_back_is_not_evidence() -> None:
    retrieval = _retrieval([_chunk("c1", "some text")])
    outcome = fuse_retrieval(
        retrieval,
        {},
        need=_need(),
        provider="weknora",
        entity_link_version="link-v1",
        retrieval_query="q",
    )
    assert outcome.evidence == []
    assert outcome.unverified[0][0] == "c1"


def test_a_chunk_whose_text_changed_on_read_back_is_not_evidence() -> None:
    retrieval = _retrieval([_chunk("c1", "original text")])
    outcome = fuse_retrieval(
        retrieval,
        {"c1": _read_back("c1", "different text")},
        need=_need(),
        provider="weknora",
        entity_link_version="link-v1",
        retrieval_query="q",
    )
    assert outcome.evidence == []
    assert "does not match" in outcome.unverified[0][1]


def test_a_chunk_read_from_another_snapshot_is_not_evidence() -> None:
    text = "Aspirin treats headache."
    retrieval = _retrieval([_chunk("c1", text)])
    outcome = fuse_retrieval(
        retrieval,
        {"c1": _read_back("c1", text, snapshot=_snapshot(pinned=False))},
        need=_need(),
        provider="weknora",
        entity_link_version="link-v1",
        retrieval_query="q",
    )
    assert outcome.evidence == []
    assert "snapshot" in outcome.unverified[0][1]


def test_a_chunk_with_moved_offsets_is_not_evidence() -> None:
    text = "Aspirin treats headache."
    retrieval = _retrieval([_chunk("c1", text)])
    outcome = fuse_retrieval(
        retrieval,
        {"c1": _read_back("c1", text, start_at=999)},
        need=_need(),
        provider="weknora",
        entity_link_version="link-v1",
        retrieval_query="q",
    )
    assert outcome.evidence == []
    assert "offsets" in outcome.unverified[0][1]


def test_a_failed_read_back_is_not_evidence() -> None:
    text = "Aspirin treats headache."
    retrieval = _retrieval([_chunk("c1", text)])
    outcome = fuse_retrieval(
        retrieval,
        {
            "c1": _read_back(
                "c1",
                status=LiteratureCallStatus.PROVIDER_ERROR,
                content=None,
            ),
        },
        need=_need(),
        provider="weknora",
        entity_link_version="link-v1",
        retrieval_query="q",
    )
    assert outcome.evidence == []


def test_every_result_is_judged_even_the_irrelevant_ones() -> None:
    """The assessment universe must include every applicable result."""
    text = "Something entirely unrelated to the hop."
    retrieval = _retrieval([_chunk("c1", text)])
    outcome = fuse_retrieval(
        retrieval,
        {"c1": _read_back("c1", text)},
        need=_need(),
        provider="weknora",
        entity_link_version="link-v1",
        retrieval_query="q",
    )
    assert len(outcome.assessments) == 1
    assert outcome.assessments[0].verdict is SupportVerdict.IRRELEVANT
    assert outcome.judged_chunk_ids == ["c1"]


# ---- support assessment --------------------------------------------------


def test_a_supporting_passage_is_judged_as_supporting() -> None:
    text = "Aspirin (ASA) is used in the treatment of headache and cephalalgia."
    retrieval = _retrieval([_chunk("c1", text)])
    outcome = fuse_retrieval(
        retrieval,
        {"c1": _read_back("c1", text)},
        need=_need(),
        provider="weknora",
        entity_link_version="link-v1",
        retrieval_query="q",
    )
    assert outcome.assessments[0].verdict is SupportVerdict.SUPPORTS
    assert outcome.assessments[0].subject_link_status == "exact"
    assert outcome.assessments[0].object_link_status == "exact"
    assert outcome.assessments[0].relation_match == "matched"


def test_a_passage_naming_both_entities_but_no_relation_is_context_only() -> None:
    text = "Aspirin and headache were both mentioned in this study."
    retrieval = _retrieval([_chunk("c1", text)])
    outcome = fuse_retrieval(
        retrieval,
        {"c1": _read_back("c1", text)},
        need=_need(),
        provider="weknora",
        entity_link_version="link-v1",
        retrieval_query="q",
    )
    assert outcome.assessments[0].verdict is SupportVerdict.CONTEXT_ONLY


def test_the_assessment_never_overwrites_the_evidence() -> None:
    text = "Aspirin (ASA) is used in the treatment of headache and cephalalgia."
    retrieval = _retrieval([_chunk("c1", text)])
    outcome = fuse_retrieval(
        retrieval,
        {"c1": _read_back("c1", text)},
        need=_need(),
        provider="weknora",
        entity_link_version="link-v1",
        retrieval_query="q",
    )
    assert outcome.evidence[0].evidence_text == text
    # The assessment is a separate record.
    assert outcome.assessments[0].literature_evidence_id == outcome.evidence[0].evidence_id


def test_the_assessment_records_the_deterministic_checks() -> None:
    text = "Aspirin (ASA) is used in the treatment of headache and cephalalgia."
    retrieval = _retrieval([_chunk("c1", text)])
    outcome = fuse_retrieval(
        retrieval,
        {"c1": _read_back("c1", text)},
        need=_need(),
        provider="weknora",
        entity_link_version="link-v1",
        retrieval_query="q",
    )
    checks = outcome.assessments[0].deterministic_checks
    assert checks["subject_found"] is True
    assert checks["object_found"] is True
    assert checks["relation_found"] is True
    assert checks["round_trip_verified"] is True


def test_the_supporting_and_contradicting_lists_are_separated() -> None:
    assessments = [
        EvidenceSupportAssessment(
            assessment_id="a1",
            assessment_version="v1",
            need_id="n1",
            need_version="v1",
            literature_evidence_id="lit.1",
            literature_evidence_version="v1",
            content_hash="h",
            entity_link_version="l",
            verdict=SupportVerdict.SUPPORTS,
            subject_link_status="exact",
            object_link_status="exact",
            relation_match="matched",
            evaluator_config_id="deterministic-v1",
            rationale="",
            created_at=utc_now(),
        ),
        EvidenceSupportAssessment(
            assessment_id="a2",
            assessment_version="v1",
            need_id="n1",
            need_version="v1",
            literature_evidence_id="lit.2",
            literature_evidence_version="v1",
            content_hash="h",
            entity_link_version="l",
            verdict=SupportVerdict.CONTRADICTS,
            subject_link_status="exact",
            object_link_status="exact",
            relation_match="matched",
            evaluator_config_id="deterministic-v1",
            rationale="",
            created_at=utc_now(),
        ),
    ]
    assert supporting_evidence_ids(assessments) == ["lit.1"]
    assert contradicting_evidence_ids(assessments) == ["lit.2"]