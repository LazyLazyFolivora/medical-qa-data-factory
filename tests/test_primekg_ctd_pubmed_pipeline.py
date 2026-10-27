from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from medical_qa_data_factory.domain import (
    EntityRef,
    EvidenceDirection,
    KGEvidence,
    LinkStatus,
)
from medical_qa_data_factory.evidence_enrichment.ctd import (
    CtdDiseaseCrosswalk,
    CtdDiseaseCrosswalkEntry,
)
from medical_qa_data_factory.evidence_enrichment.models import (
    CtdEvidenceRecord,
    PubMedEvidenceRecord,
)
from medical_qa_data_factory.evidence_enrichment.pubmed import (
    PubMedFetchResult,
    PubMedProviderStatus,
)
from medical_qa_data_factory.generation.primekg_ctd_pubmed import (
    PrimeKGCtdEdgeVerifier,
    PrimeKgCtdDiseaseMapper,
    PrimeKgCtdPubMedService,
)
from medical_qa_data_factory.knowledge_graph.contracts import (
    GraphEdge,
    GraphEntity,
)
from medical_qa_data_factory.llm import FakeStructuredModel


NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


def _ref(canonical_id: str, name: str, entity_type: str) -> EntityRef:
    return EntityRef(
        entity_ref_id=f"ref-{canonical_id}",
        raw_mention=name,
        canonical_name=name,
        canonical_id=canonical_id,
        namespace="primekg",
        entity_type=entity_type,
        aliases=[],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="primekg",
    )


def _edge(source: str = "CTD") -> KGEvidence:
    return KGEvidence(
        evidence_id="kg-edge-1",
        head=_ref("primekg:exposure:C000001", "Test chemical", "exposure"),
        relation="exposure_disease",
        tail=_ref("primekg:disease:8315", "Disease A", "disease"),
        direction=EvidenceDirection.FORWARD,
        source_database="primekg",
        source_record_id="ctd-row-1",
        retrieval_action={"provider_id": "primekg"},
        retrieved_at=NOW,
        metadata={"upstream_source": source},
    )


def _ctd_record() -> CtdEvidenceRecord:
    return CtdEvidenceRecord(
        chemical_id="C000001",
        chemical_name="Test chemical",
        disease_id="MESH:A",
        disease_name="Disease A",
        disease_aliases=["Disease A"],
        direct_evidence=["marker/mechanism"],
        pubmed_ids=["123"],
        source_snapshot_id="ctd:test",
        source_record_locator="ctd:record:A",
        content_hash=hashlib.sha256(b"ctd-record-a").hexdigest(),
    )


ABSTRACT = (
    "In an experimental study of disease, Test chemical exposure "
    "increased Disease A marker activity."
)
QUOTE = ABSTRACT


def _pubmed_record() -> PubMedEvidenceRecord:
    return PubMedEvidenceRecord(
        pmid="123",
        title="Test chemical and Disease A",
        abstract=ABSTRACT,
        snapshot_id="pubmed:test",
        content_hash=hashlib.sha256(
            f"Test chemical and Disease A\n{ABSTRACT}".encode()
        ).hexdigest(),
        retrieved_at=NOW,
    )


def _pubmed_result() -> PubMedFetchResult:
    return PubMedFetchResult(
        status=PubMedProviderStatus.SUCCESS,
        snapshot_id="pubmed:test",
        input_pmids=["123"],
        requested_pmids=["123"],
        records=[_pubmed_record()],
    )


def _extraction() -> dict:
    start = ABSTRACT.index(QUOTE)
    return {
        "findings": [
            {
                "pmid": "123",
                "verdict": "supports",
                "intent": "association_research",
                "support_level": "literature_supported",
                "polarity": "neutral",
                "claim_text": "Test chemical increased Disease A marker activity.",
                "qualifiers": {
                    "study_type": "experimental study",
                    "disease_category": "disease",
                },
                "qualifier_evidence": {
                    "study_type": "experimental study",
                    "disease_category": "disease",
                },
                "citation": {
                    "pmid": "123",
                    "start": start,
                    "end": start + len(QUOTE),
                    "quote": QUOTE,
                },
            }
        ]
    }


def _crosswalk() -> CtdDiseaseCrosswalk:
    return CtdDiseaseCrosswalk(
        entries=[
            CtdDiseaseCrosswalkEntry(
                primekg_disease_id="primekg:disease:8315",
                ctd_disease_ids=["MESH:A"],
            )
        ]
    )


class _CtdIndex:
    def enumerate_diseases(self, *, chemical_id: str):
        assert chemical_id == "C000001"
        return [_ctd_record()]


class _PubMed:
    def fetch_result(self, pmids, *, limit=None):
        assert pmids == ["123"]
        return _pubmed_result()


def _model() -> FakeStructuredModel:
    return FakeStructuredModel(
        responses={"evidence_curator": _extraction()}
    )


def _service(
    *,
    crosswalk=None,
    model=None,
    edge_verifier=None,
) -> PrimeKgCtdPubMedService:
    return PrimeKgCtdPubMedService(
        ctd_index=_CtdIndex(),
        disease_mapper=PrimeKgCtdDiseaseMapper(
            crosswalk or _crosswalk()
        ),
        pubmed=_PubMed(),
        model=model or _model(),
        primekg_edge_verifier=edge_verifier or (lambda edge: True),
    )


def test_pipeline_binds_ctd_pubmed_evidence_to_primekg_entities() -> None:
    result = _service().validate(
        edge=_edge(),
        seed_entity_id="primekg:exposure:C000001",
        answer_entity_id="primekg:disease:8315",
        evidence_kind="marker/mechanism",
    )

    assert result.status == "accepted"
    assert result.fusion is not None
    assert result.fusion.claim is not None
    assert result.fusion.claim.subject_id == "primekg:exposure:C000001"
    assert result.fusion.claim.object_id == "primekg:disease:8315"
    assert result.mapping_status == "mapped_crosswalk"
    assert result.pubmed_corpus is not None


def test_pipeline_rejects_ambiguous_disease_mapping() -> None:
    crosswalk = CtdDiseaseCrosswalk(
        entries=[
            CtdDiseaseCrosswalkEntry(
                primekg_disease_id="primekg:disease:8315",
                ctd_disease_ids=["MESH:A", "MESH:B"],
            )
        ]
    )
    result = _service(crosswalk=crosswalk).validate(
        edge=_edge(),
        seed_entity_id="primekg:exposure:C000001",
        answer_entity_id="primekg:disease:8315",
        evidence_kind="marker/mechanism",
    )

    assert result.status == "rejected"
    assert "ctd_mapping_ambiguous" in result.reason_codes
    assert result.fusion is None


def test_pipeline_rejects_unmapped_disease() -> None:
    result = _service(
        crosswalk=CtdDiseaseCrosswalk()
    ).validate(
        edge=_edge(),
        seed_entity_id="primekg:exposure:C000001",
        answer_entity_id="primekg:disease:8315",
        evidence_kind="marker/mechanism",
    )

    assert result.status == "rejected"
    assert "ctd_mapping_unmapped" in result.reason_codes


def test_pipeline_rejects_non_ctd_primekg_edge() -> None:
    result = _service().validate(
        edge=_edge("MONDO"),
        seed_entity_id="primekg:exposure:C000001",
        answer_entity_id="primekg:disease:8315",
        evidence_kind="marker/mechanism",
    )

    assert result.status == "rejected"
    assert result.reason_codes == ["source_not_ctd"]


def test_pipeline_rejects_false_primekg_edge() -> None:
    result = _service(
        edge_verifier=lambda edge: False
    ).validate(
        edge=_edge(),
        seed_entity_id="primekg:exposure:C000001",
        answer_entity_id="primekg:disease:8315",
        evidence_kind="marker/mechanism",
    )

    assert result.status == "rejected"
    assert "primekg_edge_mismatch" in result.reason_codes


def test_pipeline_rejects_fabricated_pubmed_quote() -> None:
    payload = _extraction()
    payload["findings"][0]["citation"]["quote"] = "fabricated quote"
    result = _service(
        model=FakeStructuredModel(
            responses={"evidence_curator": payload}
        )
    ).validate(
        edge=_edge(),
        seed_entity_id="primekg:exposure:C000001",
        answer_entity_id="primekg:disease:8315",
        evidence_kind="marker/mechanism",
    )

    assert result.status == "rejected"
    assert "pubmed_quote_mismatch" in result.reason_codes


def test_pipeline_rejects_unreadable_pubmed_result() -> None:
    class _UnreadablePubMed(_PubMed):
        def fetch_result(self, pmids, *, limit=None):
            return PubMedFetchResult(
                status=PubMedProviderStatus.NO_ABSTRACT,
                snapshot_id="pubmed:test",
                input_pmids=list(pmids),
                requested_pmids=list(pmids),
                records=[],
            )

    service = PrimeKgCtdPubMedService(
        ctd_index=_CtdIndex(),
        disease_mapper=PrimeKgCtdDiseaseMapper(_crosswalk()),
        pubmed=_UnreadablePubMed(),
        model=_model(),
        primekg_edge_verifier=lambda edge: True,
    )

    result = service.validate(
        edge=_edge(),
        seed_entity_id="primekg:exposure:C000001",
        answer_entity_id="primekg:disease:8315",
        evidence_kind="marker/mechanism",
    )

    assert result.status == "rejected"
    assert "pubmed_no_abstract" in result.reason_codes


def test_provider_edge_verifier_requires_a_ctd_source_edge() -> None:
    edge = _edge()
    graph_edge = GraphEdge(
        provider_id="primekg",
        edge_id="provider-edge-1",
        head=GraphEntity(
            provider_id="primekg",
            native_id="C000001",
            canonical_id="primekg:exposure:C000001",
            name="Test chemical",
            entity_type="exposure",
        ),
        relation="exposure_disease",
        tail=GraphEntity(
            provider_id="primekg",
            native_id="8315",
            canonical_id="primekg:disease:8315",
            name="Disease A",
            entity_type="disease",
        ),
        source="CTD",
    )

    class _Provider:
        def neighbors(self, query):
            return type(
                "Result",
                (),
                {
                    "edges": [graph_edge],
                    "truncated": False,
                    "next_cursor": None,
                },
            )()

    assert PrimeKGCtdEdgeVerifier(_Provider())(edge) is True
    graph_edge.source = "MONDO"
    assert PrimeKGCtdEdgeVerifier(_Provider())(edge) is False
