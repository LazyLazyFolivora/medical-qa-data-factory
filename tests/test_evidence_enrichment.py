from __future__ import annotations

import gzip
from pathlib import Path

from medical_qa_data_factory.domain import (
    EntityRef,
    EvidenceDirection,
    KGEvidence,
    LinkStatus,
)
from medical_qa_data_factory.evidence_enrichment.ctd import (
    CtdEvidenceIndex,
    build_ctd_index,
)
from medical_qa_data_factory.evidence_enrichment.models import (
    EnrichmentStatus,
    EvidencePolarity,
    EvidenceSupportLevel,
    NormalizedEvidenceClaim,
    QuestionIntent,
)
from medical_qa_data_factory.evidence_enrichment.pubmed import PubMedClient
from medical_qa_data_factory.evidence_enrichment.service import (
    EvidenceEnrichmentResult,
    EvidenceEnrichmentService,
)
from medical_qa_data_factory.generation.contracts import RelationSemantics
from medical_qa_data_factory.generation.curation import (
    CuratedEvidence,
)
from medical_qa_data_factory.generation.planning import (
    EnrichedEntityQuestionPlanner,
)
from medical_qa_data_factory.llm import FakeStructuredModel


NOW = "2026-10-22T00:00:00Z"


def _ref(name: str, canonical_id: str, entity_type: str) -> EntityRef:
    return EntityRef(
        entity_ref_id=f"entity-{canonical_id}",
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


ASPIRIN = _ref("Aspirin", "primekg:exposure:D001241", "exposure")
CANCER = _ref("prostate cancer", "primekg:disease:8315", "disease")


def _edge() -> KGEvidence:
    return KGEvidence(
        evidence_id="kg.ctd.1",
        head=ASPIRIN,
        relation="exposure_disease",
        tail=CANCER,
        direction=EvidenceDirection.FORWARD,
        source_database="primekg",
        source_record_id="ctd-row-1",
        retrieval_action={"provider_id": "primekg"},
        retrieved_at=NOW,
        metadata={"upstream_source": "CTD"},
    )


def _ctd_fixture(path: Path) -> Path:
    source = path / "ctd.csv.gz"
    with gzip.open(source, "wt", encoding="utf-8", newline="") as handle:
        handle.write("# CTD test fixture\n")
        handle.write(
            "Aspirin\tD001241\t\tprostate cancer\tMESH:D011471\t"
            "marker/mechanism\tPTGS1\t3.5\t\t12345678\n"
        )
    return source


def _xml() -> bytes:
    return b"""
    <PubmedArticleSet>
      <PubmedArticle>
        <MedlineCitation>
          <PMID>12345678</PMID>
          <Article>
            <ArticleTitle>Aspirin exposure and prostate cancer</ArticleTitle>
            <Abstract>
              <AbstractText>Aspirin exposure was associated with lower prostate cancer risk.</AbstractText>
            </Abstract>
          </Article>
        </MedlineCitation>
      </PubmedArticle>
    </PubmedArticleSet>
    """


class _PubMed:
    def search(self, query: str, *, limit: int) -> list[str]:
        return ["12345678"]

    def fetch(self, pmids: list[str], *, limit: int):
        return PubMedClient(
            transport=lambda _url, _data: _xml(),
            snapshot_id="pubmed:test",
        ).fetch(["12345678"], limit=limit)

    def fetch_result(self, pmids: list[str], *, limit: int | None = None):
        return PubMedClient(
            transport=lambda _url, _data: _xml(),
            snapshot_id="pubmed:test",
        ).fetch_result(["12345678"], limit=limit)


def test_ctd_index_builds_and_maps_exact_pair(tmp_path: Path) -> None:
    source = _ctd_fixture(tmp_path)
    index_path = tmp_path / "ctd.sqlite"
    metadata = build_ctd_index(source, index_path)
    records = CtdEvidenceIndex(index_path).lookup(
        chemical_id="D001241",
        disease_name="Prostate Cancer",
    )

    assert metadata["rows_processed"] == 1
    assert records[0].direct_evidence == ["marker/mechanism"]
    assert records[0].pubmed_ids == ["12345678"]


def test_ctd_lookup_aggregates_inference_rows(tmp_path: Path) -> None:
    source = tmp_path / "ctd-multi.csv.gz"
    with gzip.open(source, "wt", encoding="utf-8", newline="") as handle:
        handle.write("# fixture\n")
        handle.write(
            "Aspirin\tD001241\t\tProstatic Neoplasms\tMESH:D011471\t"
            "\tPTGS1\t10\t\t111\n"
        )
        handle.write(
            "Aspirin\tD001241\t\tProstatic Neoplasms\tMESH:D011471\t"
            "\tTP53\t20\t\t222\n"
        )
    index_path = tmp_path / "ctd-multi.sqlite"
    build_ctd_index(source, index_path)

    records = CtdEvidenceIndex(index_path).lookup(
        chemical_id="D001241",
        disease_name="prostate cancer",
    )

    assert len(records) == 1
    assert records[0].inference_genes == ["PTGS1", "TP53"]
    assert records[0].inference_score == 20
    assert records[0].pubmed_ids == ["111", "222"]


def test_enrichment_extracts_supported_claim_and_literature(
    tmp_path: Path,
) -> None:
    source = _ctd_fixture(tmp_path)
    index_path = tmp_path / "ctd.sqlite"
    build_ctd_index(source, index_path)
    model = FakeStructuredModel(
        responses={
            "evidence_curator": {
                "status": "supported",
                "question_intent": "risk_decrease",
                "support_level": "literature_supported",
                "polarity": "decrease",
                "claim_text": "Aspirin exposure was associated with lower prostate cancer risk.",
                "study_type": "observational",
                "population": None,
                "supported_pmid_ids": ["12345678"],
                "support_spans": [
                    {
                        "pmid": "12345678",
                        "text_field": "abstract",
                        "start": 37,
                        "end": 63,
                        "quote": "lower prostate cancer risk",
                    }
                ],
                "reason_codes": [],
            }
        }
    )
    service = EvidenceEnrichmentService(
        ctd_index=CtdEvidenceIndex(index_path),
        model=model,
        pubmed=_PubMed(),
    )

    result = service.enrich(
        _edge(),
        seed_entity_id=ASPIRIN.canonical_id or "",
        answer_entity_id=CANCER.canonical_id or "",
    )

    assert result.status is EnrichmentStatus.SUPPORTED
    assert result.claim is not None
    assert result.claim.question_intent is QuestionIntent.RISK_DECREASE
    assert result.claim.answer_entity_id == CANCER.canonical_id
    assert result.literature_evidence[0].pmid == "12345678"
    assert "lower prostate cancer risk" in result.literature_evidence[0].evidence_text
    assert result.normalized_claim is not None
    assert result.normalized_claim.predicate_family == "disease_risk"
    assert result.normalized_claim.polarity == "decrease"
    assert {
        item.provider_id for item in result.evidence_records
    } == {"ctd", "pubmed"}
    assert len(result.pubmed_spans) == 1
    assert result.pubmed_corpus_manifest is not None
    supporting_record = next(
        item
        for item in result.evidence_records
        if item.evidence_id
        in result.normalized_claim.supporting_evidence_ids
    )
    assert result.normalized_claim.supporting_evidence_ids == [
        supporting_record.evidence_id
    ]
    assert supporting_record.document_text == "lower prostate cancer risk"


def test_enrichment_rejects_a_fabricated_pubmed_quote(tmp_path: Path) -> None:
    source = _ctd_fixture(tmp_path)
    index_path = tmp_path / "ctd.sqlite"
    build_ctd_index(source, index_path)
    model = FakeStructuredModel(
        responses={
            "evidence_curator": {
                "status": "supported",
                "question_intent": "risk_decrease",
                "support_level": "literature_supported",
                "polarity": "decrease",
                "claim_text": "lower prostate cancer risk",
                "supported_pmid_ids": ["12345678"],
                "support_spans": [
                    {
                        "pmid": "12345678",
                        "start": 37,
                        "end": 63,
                        "quote": "fabricated prostate claim",
                    }
                ],
                "reason_codes": [],
            }
        }
    )
    service = EvidenceEnrichmentService(
        ctd_index=CtdEvidenceIndex(index_path),
        model=model,
        pubmed=_PubMed(),
    )

    result = service.enrich(
        _edge(),
        seed_entity_id=ASPIRIN.canonical_id or "",
        answer_entity_id=CANCER.canonical_id or "",
    )

    assert result.status is EnrichmentStatus.INSUFFICIENT
    assert result.normalized_claim is None
    assert result.reason_codes == ["pubmed_quote_mismatch"]


def _curated(edge: KGEvidence) -> CuratedEvidence:
    return CuratedEvidence(
        evidence=edge,
        stable_edge_id="ctd-row-1",
        relation=edge.relation,
        answer_endpoint="tail",
        semantics=RelationSemantics.GENERIC_RELATION,
        seed_is_head=True,
    )


def _claim(target_id: str) -> NormalizedEvidenceClaim:
    return NormalizedEvidenceClaim(
        claim_id=f"claim-{target_id}",
        kg_edge_id="kg.ctd.1",
        seed_entity_id=ASPIRIN.canonical_id or "",
        answer_entity_id=target_id,
        answer_entity_type="disease",
        relation="exposure_disease",
        question_intent=QuestionIntent.RISK_DECREASE,
        support_level=EvidenceSupportLevel.LITERATURE_SUPPORTED,
        polarity=EvidencePolarity.DECREASE,
        claim_text="Aspirin exposure was associated with lower prostate cancer risk.",
    )


def test_enriched_planner_preserves_entity_answer_and_rejects_ambiguity() -> None:
    edge = _edge()
    curated = [_curated(edge)]
    supported = EvidenceEnrichmentResult(
        edge_id=edge.evidence_id,
        status=EnrichmentStatus.SUPPORTED,
        claim=_claim(CANCER.canonical_id or ""),
    )
    planner = EnrichedEntityQuestionPlanner()

    blueprints = planner.plan(
        curated,
        [supported],
        seed_canonical_id=ASPIRIN.canonical_id or "",
        question_count=1,
    )

    assert len(blueprints) == 1
    assert blueprints[0].answer_form.value == "entity"
    assert blueprints[0].answer_entity_id == CANCER.canonical_id
    assert blueprints[0].logical_query["answer_candidate_count"] == 1
    assert "风险降低" in (blueprints[0].semantic_skeleton or "")

    ambiguous = planner.plan(
        curated,
        [
            supported,
            EvidenceEnrichmentResult(
                edge_id=edge.evidence_id,
                status=EnrichmentStatus.SUPPORTED,
                claim=_claim("primekg:disease:other"),
            ),
        ],
        seed_canonical_id=ASPIRIN.canonical_id or "",
        question_count=1,
    )
    assert ambiguous == []
