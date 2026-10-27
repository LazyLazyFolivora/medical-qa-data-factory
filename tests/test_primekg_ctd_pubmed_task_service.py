from __future__ import annotations

import hashlib
from datetime import datetime, timezone

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
    PrimeKgCtdDiseaseMapper,
)
from medical_qa_data_factory.generation.primekg_ctd_pubmed_task import (
    PrimeKGCtdPubmedTaskService,
)
from medical_qa_data_factory.generation.task_release import (
    TaskReleaseExporter,
)
from medical_qa_data_factory.generation.task_contracts import (
    ProviderSnapshotRef,
    ValidationEnvironment,
)
from medical_qa_data_factory.knowledge_graph.contracts import (
    GraphEdge,
    GraphEntity,
    GraphQueryResult,
    GraphSnapshot,
)
from medical_qa_data_factory.llm import FakeStructuredModel


NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
SNAPSHOT_ID = "primekg:test-snapshot"
CTD_SNAPSHOT_ID = "ctd:test-snapshot"
PUBMED_SNAPSHOT_ID = "pubmed:test-snapshot"
SEED_ID = "primekg:exposure:C000001"
ANSWER_A_ID = "primekg:disease:8315"
ANSWER_B_ID = "primekg:disease:9999"


def _entity(canonical_id: str, name: str) -> GraphEntity:
    provider_id, entity_type, native_id = canonical_id.split(":", 2)
    return GraphEntity(
        provider_id=provider_id,
        native_id=native_id,
        canonical_id=canonical_id,
        name=name,
        entity_type=entity_type,
    )


def _edge(
    answer_id: str,
    answer_name: str,
    *,
    source: str,
) -> GraphEdge:
    return GraphEdge(
        provider_id="primekg",
        edge_id=f"primekg-edge:{answer_id}",
        head=_entity(SEED_ID, "Test chemical"),
        relation="exposure_disease",
        tail=_entity(answer_id, answer_name),
        source=source,
    )


class _Provider:
    provider_id = "primekg"

    def __init__(self, edges: list[GraphEdge]) -> None:
        self.edges = edges

    def snapshot(self) -> GraphSnapshot:
        return GraphSnapshot(
            provider_id="primekg",
            adapter_version="test",
            data_snapshot_id=SNAPSHOT_ID,
            config_hash="test-config",
        )

    def neighbors(self, query) -> GraphQueryResult:
        edges = [
            edge
            for edge in self.edges
            if query.direction == "outgoing"
            and edge.head.canonical_id == query.entity.canonical_id
            and (not query.relations or edge.relation in query.relations)
        ]
        return GraphQueryResult(
            edges=edges,
            trace={
                "query_id": f"query-{query.entity.canonical_id}",
                "provider_id": "primekg",
                "operation": "neighbors",
                "normalized_request": {},
                "started_at": NOW,
                "rows_examined": len(edges),
                "records_returned": len(edges),
            },
            snapshot=self.snapshot(),
            truncated=False,
        )


def _ctd_record(
    *,
    disease_id: str,
    disease_name: str,
    pmid: str,
    direct_evidence: str = "therapeutic",
) -> CtdEvidenceRecord:
    return CtdEvidenceRecord(
        chemical_id="C000001",
        chemical_name="Test chemical",
        disease_id=disease_id,
        disease_name=disease_name,
        disease_aliases=[disease_name],
        direct_evidence=[direct_evidence],
        pubmed_ids=[pmid],
        source_snapshot_id=CTD_SNAPSHOT_ID,
        source_record_locator=f"ctd-record:{disease_id}",
        content_hash=hashlib.sha256(
            f"{disease_id}:{pmid}".encode("utf-8")
        ).hexdigest(),
    )


class _CtdIndex:
    def __init__(self, records: list[CtdEvidenceRecord]) -> None:
        self.records = records

    def enumerate_diseases(self, *, chemical_id: str):
        assert chemical_id == "C000001"
        return list(self.records)

    def metadata(self):
        return {"snapshot_id": CTD_SNAPSHOT_ID}


def _pmid_for_disease(record: CtdEvidenceRecord) -> str:
    return record.pubmed_ids[0]


class _PubMed:
    def __init__(self, records: list[CtdEvidenceRecord]) -> None:
        self.by_pmid = {
            _pmid_for_disease(record): record for record in records
        }

    def fetch_result(self, pmids, *, limit=None):
        requested = sorted(set(pmids))
        documents = []
        for pmid in requested:
            record = self.by_pmid[pmid]
            if "marker/mechanism" in record.direct_evidence:
                abstract = (
                    f"Test chemical exposure was associated with "
                    f"{record.disease_name} in a controlled study."
                )
            else:
                abstract = (
                    f"Test chemical was studied as a treatment for "
                    f"{record.disease_name} in a controlled study."
                )
            documents.append(
                PubMedEvidenceRecord(
                    pmid=pmid,
                    title=f"{record.disease_name} treatment study",
                    abstract=abstract,
                    snapshot_id=PUBMED_SNAPSHOT_ID,
                    content_hash=hashlib.sha256(
                        f"{record.disease_name}\n{abstract}".encode("utf-8")
                    ).hexdigest(),
                    retrieved_at=NOW,
                )
            )
        return PubMedFetchResult(
            status=PubMedProviderStatus.SUCCESS,
            snapshot_id=PUBMED_SNAPSHOT_ID,
            input_pmids=requested,
            requested_pmids=requested,
            records=documents,
        )


def _crosswalk() -> CtdDiseaseCrosswalk:
    return CtdDiseaseCrosswalk(
        entries=[
            CtdDiseaseCrosswalkEntry(
                primekg_disease_id=ANSWER_A_ID,
                ctd_disease_ids=["MESH:A"],
            ),
            CtdDiseaseCrosswalkEntry(
                primekg_disease_id=ANSWER_B_ID,
                ctd_disease_ids=["MESH:B"],
            ),
        ]
    )


def _environment() -> ValidationEnvironment:
    return ValidationEnvironment(
        environment_id="primekg-ctd-pubmed-test-world",
        providers=[
            ProviderSnapshotRef(
                provider_id="primekg",
                snapshot_id=SNAPSHOT_ID,
                completeness="full_snapshot_query",
            ),
            ProviderSnapshotRef(
                provider_id="ctd",
                snapshot_id=CTD_SNAPSHOT_ID,
                completeness="all_direct_and_inference_rows",
            ),
            ProviderSnapshotRef(
                provider_id="pubmed",
                snapshot_id=PUBMED_SNAPSHOT_ID,
                completeness="all_ctd_pmids_read",
            ),
        ],
        completeness_scope="all PrimeKG exposure answers under PubMed evidence",
    )


def _extraction(request) -> dict:
    edge = request.payload["primekg_edge"]
    record = request.payload["pubmed_records"][0]
    answer_name = edge["object_name"]
    quote = (
        f"was studied as a treatment for {answer_name} in a controlled study"
    )
    abstract = record["abstract"]
    start = abstract.index(quote)
    supports = edge["object_id"] == ANSWER_A_ID
    finding = {
        "pmid": record["pmid"],
        "verdict": "supports" if supports else "context_only",
        "intent": "therapeutic_research" if supports else None,
        "support_level": "literature_supported",
        "polarity": "neutral",
        "claim_text": (
            f"Test chemical was studied as a treatment for {answer_name}."
            if supports
            else ""
        ),
        "qualifiers": {
            "study_type": "controlled study",
            "disease_category": "metabolic disease",
        },
        "citation": {
            "pmid": record["pmid"],
            "start": start,
            "end": start + len(quote),
            "quote": quote,
        },
    }
    return {"findings": [finding]}


def _question_writer(request) -> dict:
    task = request.payload["task"]
    chemical = task["public_entity_names"][task["seed_entity_id"]]
    if (
        task["public_study_context"]["relation_intent"]
        == "association_research"
    ):
        question = (
            f"在受控实验研究中，哪类代谢疾病被研究与"
            f"{chemical} 暴露存在关联？"
        )
    else:
        question = (
            f"在受控实验研究中，{chemical} 暴露后被研究作为"
            "哪类代谢疾病的治疗干预？"
        )
    return {
        "question_text": question,
        "language": "zh-CN",
    }


def _semantic_verifier(request) -> dict:
    task = request.payload["validated_task"]
    context = task["public_study_context"]
    return {
        "chemical_name": task["public_entity_names"][task["seed_entity_id"]],
        "relation_intent": context["relation_intent"],
        "polarity": context["polarity"],
        "population": context["population"],
        "exposure_context": context["exposure_context"],
        "dose_or_regimen": context["dose_or_regimen"],
        "exposure_duration": context["exposure_duration"],
        "study_type": context["study_type"],
        "sample_type": context["sample_type"],
        "disease_category": context["disease_category"],
        "temporal_scope": context["temporal_scope"],
        "additional_qualifiers": context["additional_qualifiers"],
        "cardinality": "exactly_one",
        "uncertain": False,
    }


def _model(*, support_both: bool = False) -> FakeStructuredModel:
    def extraction(request):
        payload = _extraction(request)
        if support_both:
            finding = payload["findings"][0]
            finding["verdict"] = "supports"
            finding["intent"] = "therapeutic_research"
            finding["claim_text"] = (
                "Test chemical was studied as a treatment for "
                f"{request.payload['primekg_edge']['object_name']}."
            )
        return payload

    return FakeStructuredModel(
        responses={
            "evidence_curator": extraction,
            "question_writer": _question_writer,
            "answer_verifier": _semantic_verifier,
        }
    )


def _association_model() -> FakeStructuredModel:
    def extraction(request):
        edge = request.payload["primekg_edge"]
        record = request.payload["pubmed_records"][0]
        answer_name = edge["object_name"]
        quote = f"was associated with {answer_name} in a controlled study"
        message = f"Test chemical exposure {quote}."
        start = message.index(quote)
        supports = edge["object_id"] == ANSWER_A_ID
        return {
            "findings": [
                {
                    "pmid": record["pmid"],
                    "verdict": "supports" if supports else "context_only",
                    "intent": "association_research" if supports else None,
                    "support_level": "literature_supported",
                    "polarity": "neutral",
                    "claim_text": message if supports else "",
                    "qualifiers": {
                        "study_type": "controlled study",
                        "disease_category": "metabolic disease",
                    },
                    "citation": {
                        "pmid": record["pmid"],
                        "start": start,
                        "end": start + len(quote),
                        "quote": quote,
                    },
                }
            ]
        }

    return FakeStructuredModel(
        responses={
            "evidence_curator": extraction,
            "question_writer": _question_writer,
            "answer_verifier": _semantic_verifier,
        }
    )


def _service(
    *,
    records: list[CtdEvidenceRecord] | None = None,
    crosswalk: CtdDiseaseCrosswalk | None = None,
    model: FakeStructuredModel | None = None,
    edges: list[GraphEdge] | None = None,
) -> PrimeKGCtdPubmedTaskService:
    ctd_records = records or [
        _ctd_record(
            disease_id="MESH:A",
            disease_name="Disease A",
            pmid="123",
        ),
        _ctd_record(
            disease_id="MESH:B",
            disease_name="Disease B",
            pmid="124",
        ),
    ]
    provider = _Provider(
        edges
        or [
            _edge(ANSWER_A_ID, "Disease A", source="CTD"),
            _edge(ANSWER_B_ID, "Disease B", source="MONDO"),
        ]
    )
    return PrimeKGCtdPubmedTaskService(
        provider=provider,
        ctd_index=_CtdIndex(ctd_records),
        pubmed=_PubMed(ctd_records),
        disease_mapper=PrimeKgCtdDiseaseMapper(crosswalk or _crosswalk()),
        model=model or _model(),
    )


def test_service_publishes_one_natural_question_with_complete_pubmed_competition():
    model = _model()
    result = _service(model=model).generate(
        edge=_edge(ANSWER_A_ID, "Disease A", source="CTD"),
        evidence_kind="therapeutic",
        environment=_environment(),
    )

    assert result.rejections == []
    assert len(result.published) == 1
    published = result.published[0]
    assert published.validated_task.answer_contract.canonical_answer_ids == [
        ANSWER_A_ID
    ]
    assert published.validated_task.task.question_type == (
        "primekg_ctd_pubmed_therapeutic_research_disease"
    )
    assert published.validation_report.result_complete is True
    assert {
        item.answer_id: item.status
        for item in published.validation_report.competitor_outcomes
    } == {
        ANSWER_A_ID: "matched",
        ANSWER_B_ID: "not_matched",
    }
    assert "Disease A" not in published.planner_input.question
    assert "primekg:" not in published.planner_input.question
    assert published.semantic_report.passed is True
    assert len(model.calls_for("evidence_curator")) == 2
    assert len(model.calls_for("question_writer")) == 1
    assert len(model.calls_for("answer_verifier")) == 1


def test_unknown_competitor_blocks_publication_and_writer_calls():
    model = _model()
    crosswalk = CtdDiseaseCrosswalk(
        entries=[
            CtdDiseaseCrosswalkEntry(
                primekg_disease_id=ANSWER_A_ID,
                ctd_disease_ids=["MESH:A"],
            )
        ]
    )
    result = _service(model=model, crosswalk=crosswalk).generate(
        edge=_edge(ANSWER_A_ID, "Disease A", source="CTD"),
        evidence_kind="therapeutic",
        environment=_environment(),
    )

    assert result.published == []
    assert result.rejections
    assert "unknown_competitor" in result.rejections[0].reason_codes
    assert model.calls_for("question_writer") == []


def test_two_evidence_supported_primekg_answers_are_rejected():
    result = _service(model=_model(support_both=True)).generate(
        edge=_edge(ANSWER_A_ID, "Disease A", source="CTD"),
        evidence_kind="therapeutic",
        environment=_environment(),
    )

    assert result.published == []
    assert result.validation_reports
    assert "world_cardinality_mismatch" in (
        result.validation_reports[0].reason_codes
    )


def test_marker_mechanism_support_produces_neutral_association_task():
    records = [
        _ctd_record(
            disease_id="MESH:A",
            disease_name="Disease A",
            pmid="123",
            direct_evidence="marker/mechanism",
        ),
        _ctd_record(
            disease_id="MESH:B",
            disease_name="Disease B",
            pmid="124",
            direct_evidence="marker/mechanism",
        ),
    ]

    result = _service(
        model=_association_model(),
        records=records,
    ).generate(
        edge=_edge(ANSWER_A_ID, "Disease A", source="CTD"),
        evidence_kind="marker/mechanism",
        environment=_environment(),
    )

    assert len(result.published) == 1
    assert result.published[0].planner_input.qtype == (
        "primekg_ctd_pubmed_association"
    )


def test_writer_failure_keeps_validated_task_unpublished():
    model = _model()
    model.set_response(
        "question_writer",
        {"question_text": "Disease A 是否有效？", "language": "zh-CN"},
    )
    result = _service(model=model).generate(
        edge=_edge(ANSWER_A_ID, "Disease A", source="CTD"),
        evidence_kind="therapeutic",
        environment=_environment(),
    )

    assert result.published == []
    assert result.rejections[-1].stage == "text_semantic_verification"
    assert "answer_leakage" in result.rejections[-1].reason_codes


def test_model_failure_is_an_unknown_competitor_not_a_crash():
    result = _service(model=FakeStructuredModel()).generate(
        edge=_edge(ANSWER_A_ID, "Disease A", source="CTD"),
        evidence_kind="therapeutic",
        environment=_environment(),
    )

    assert result.published == []
    assert result.rejections
    assert "unknown_competitor" in result.rejections[0].reason_codes


def test_caller_cannot_label_a_provider_edge_as_ctd():
    result = _service(
        model=_model(),
        edges=[
            _edge(ANSWER_A_ID, "Disease A", source="MONDO"),
            _edge(ANSWER_B_ID, "Disease B", source="MONDO"),
        ],
    ).generate(
        edge=_edge(ANSWER_A_ID, "Disease A", source="CTD"),
        evidence_kind="therapeutic",
        environment=_environment(),
    )

    assert result.published == []
    assert result.rejections[0].stage == "primekg_competition"
    assert "source_not_ctd" in result.rejections[0].reason_codes


def test_pubmed_snapshot_outside_environment_blocks_publication():
    environment = _environment()
    poison = environment.model_copy(
        update={
            "providers": [
                item.model_copy(
                    update={
                        "snapshot_id": (
                            "pubmed:other-snapshot"
                            if item.provider_id == "pubmed"
                            else item.snapshot_id
                        )
                    }
                )
                for item in environment.providers
            ]
        }
    )

    result = _service(model=_model()).generate(
        edge=_edge(ANSWER_A_ID, "Disease A", source="CTD"),
        evidence_kind="therapeutic",
        environment=poison,
    )

    assert result.published == []
    assert "unknown_competitor" in result.rejections[0].reason_codes


def test_release_keeps_pubmed_truth_and_competition_hidden(tmp_path):
    result = _service(model=_model()).generate(
        edge=_edge(ANSWER_A_ID, "Disease A", source="CTD"),
        evidence_kind="therapeutic",
        environment=_environment(),
    )

    output = tmp_path / "release"
    summary = TaskReleaseExporter().export(result, output)

    assert summary.published_count == 1
    hidden = {path.name for path in (output / "hidden").iterdir()}
    assert {
        "pubmed_corpus_manifest.json",
        "pubmed_documents.jsonl",
        "pubmed_spans.jsonl",
        "pubmed_assessments.jsonl",
        "ctd_pubmed_fusions.jsonl",
        "competitor_reports.jsonl",
    }.issubset(hidden)
    public_payload = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (output / "public").iterdir()
        if path.is_file()
    )
    assert "Disease A" not in public_payload
    assert ANSWER_A_ID not in public_payload
    assert "pubmed-corpus." in (
        output / "hidden" / "pubmed_corpus_manifest.json"
    ).read_text(encoding="utf-8")
