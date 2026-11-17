"""Material retrieval is provider-scoped and cannot write claims or gold."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medical_qa_data_factory.generation.evidence_provider import (
    EvidenceProviderCapability,
    EvidenceProviderRequest,
    EvidenceProviderResult,
    ProviderTerminalStatus,
    SideEffectClass,
)
from medical_qa_data_factory.generation.material_retrieval_subagent import (
    MaterialRetrievalSubagent,
)
from medical_qa_data_factory.generation.objective_provider_adapters import (
    PubMedMaterialProvider,
)
from medical_qa_data_factory.evidence_enrichment.models import (
    PubMedEvidenceRecord,
)
from medical_qa_data_factory.evidence_enrichment.pubmed import (
    PubMedFetchResult,
    PubMedProviderStatus,
    PubMedSearchResult,
)
from medical_qa_data_factory.generation.objective_question_contracts import (
    EvidenceAnchor,
    EvidenceMaterial,
    ExecutionStatus,
    SubagentTask,
    VerificationVerdict,
    text_digest,
)
from medical_qa_data_factory.generation.objective_question_persistence import (
    ObjectiveQuestionPersistence,
    immutable_blob_ref,
)
from medical_qa_data_factory.generation.subagent_contracts import (
    RoleContractViolation,
    SubagentRole,
    build_objective_subagent_task,
    default_objective_role_registry,
)


NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
RAW = "BRCA1 is located on human chromosome 17."


class FakeProvider:
    def __init__(
        self,
        provider_id: str,
        *,
        terminal_status: ProviderTerminalStatus = ProviderTerminalStatus.SUCCEEDED,
        error_category: str | None = None,
    ) -> None:
        self.capability = EvidenceProviderCapability(
            provider_id=provider_id,
            provider_version="v1",
            side_effect_class=SideEffectClass.READ_ONLY,
            supports_idempotency=True,
            supports_status_lookup=False,
            safe_to_repeat=True,
        )
        self.terminal_status = terminal_status
        self.error_category = error_category
        self.requests: list[EvidenceProviderRequest] = []

    def retrieve(self, request: EvidenceProviderRequest) -> EvidenceProviderResult:
        self.requests.append(request)
        material = EvidenceMaterial(
            material_id=f"material.{request.request_id}",
            provider_id=self.capability.provider_id,
            provider_version=self.capability.provider_version,
            source_record_id="gene:BRCA1",
            retrieved_at=NOW,
            content_hash=text_digest(RAW),
            raw_content_ref=immutable_blob_ref(text_digest(RAW)),
            material_type="structured_record",
            retrieval_request_id=request.request_id,
            terminal_status=ProviderTerminalStatus.SUCCEEDED,
        )
        anchor = EvidenceAnchor(
            anchor_id=f"anchor.{request.request_id}",
            material_id=material.material_id,
            document_id="authority:gene:BRCA1",
            section_or_field="description",
            start_offset=0,
            end_offset=5,
            quoted_text="BRCA1",
            quote_hash=text_digest("BRCA1"),
        )
        succeeded = self.terminal_status is ProviderTerminalStatus.SUCCEEDED
        return EvidenceProviderResult(
            request=request,
            terminal_status=self.terminal_status,
            material_ids=[material.material_id] if succeeded else [],
            anchor_ids=[anchor.anchor_id] if succeeded else [],
            error_category=self.error_category,
            materials=[material.model_dump(mode="json")] if succeeded else [],
            anchors=[anchor.model_dump(mode="json")] if succeeded else [],
            raw_content_by_material_id=(
                {material.material_id: RAW} if succeeded else {}
            ),
        )

    def lookup_status(self, operation_key: str):
        return None


def _task(
    provider_id: str = "pubmed",
    allowed: list[str] | None = None,
) -> SubagentTask:
    capability = default_objective_role_registry()[
        SubagentRole.MATERIAL_RETRIEVAL
    ]
    return build_objective_subagent_task(
        task_id=f"subtask-materials-{provider_id}",
        role=SubagentRole.MATERIAL_RETRIEVAL,
        capability=capability,
        goal={
            "target_entity": "BRCA1",
            "evidence_need": "find an authoritative chromosome location",
            "allowed_providers": allowed or [provider_id],
            "material_limit": 5,
        },
        input_artifact_ids=[],
        allowed_tools=[provider_id],
    )


def test_material_agent_persists_raw_content_anchor_and_terminal_status(
    tmp_path,
) -> None:
    provider = FakeProvider("pubmed")
    persistence = ObjectiveQuestionPersistence(tmp_path / "ws")
    outcome = MaterialRetrievalSubagent(persistence).run(
        _task(),
        provider=provider,
        query={"term": "BRCA1 chromosome"},
    )

    assert outcome.result.execution_status is ExecutionStatus.SUCCEEDED
    assert outcome.result.verdict is VerificationVerdict.NOT_EMITTED
    assert len(outcome.materials) == 1
    assert len(outcome.anchors) == 1
    assert (
        persistence.load_raw_content(outcome.materials[0]).decode("utf-8")
        == RAW
    )
    assert provider.requests[0].operation_key == _task().operation_key


def test_material_agent_rejects_a_provider_outside_the_task_allowlist(
    tmp_path,
) -> None:
    provider = FakeProvider("ctd")

    with pytest.raises(RoleContractViolation, match="allowlist"):
        MaterialRetrievalSubagent(
            ObjectiveQuestionPersistence(tmp_path / "ws")
        ).run(
            _task("pubmed", allowed=["pubmed"]),
            provider=provider,
            query={"term": "BRCA1"},
        )


@pytest.mark.parametrize(
    ("status", "error", "domain", "stop"),
    [
        (ProviderTerminalStatus.EMPTY, None, "INSUFFICIENT", "no_materials"),
        (
            ProviderTerminalStatus.FAILED,
            "RATE_LIMITED",
            "INSUFFICIENT",
            "provider_failure",
        ),
        (
            ProviderTerminalStatus.UNKNOWN,
            "TIMEOUT_UNKNOWN_OUTCOME",
            "INSUFFICIENT",
            "unknown_outcome",
        ),
    ],
)
def test_empty_failure_and_unknown_are_never_negative_claims(
    tmp_path,
    status: ProviderTerminalStatus,
    error: str | None,
    domain: str,
    stop: str,
) -> None:
    outcome = MaterialRetrievalSubagent(
        ObjectiveQuestionPersistence(tmp_path / "ws")
    ).run(
        _task(),
        provider=FakeProvider(
            "pubmed",
            terminal_status=status,
            error_category=error,
        ),
        query={"term": "BRCA1"},
    )

    assert outcome.materials == []
    assert outcome.anchors == []
    assert outcome.result.domain_outcome.value == domain
    assert outcome.result.stop_reason == stop


def test_pubmed_adapter_preserves_empty_query_for_controller_replanning() -> None:
    class FakePubMed:
        def __init__(self):
            self.queries = []

        def search_result(self, query, *, limit):
            self.queries.append(query)
            if query == "ZRSR2":
                return PubMedSearchResult(
                    status=PubMedProviderStatus.SUCCESS,
                    snapshot_id="pubmed:test",
                    query=query,
                    pmids=["123"],
                )
            return PubMedSearchResult(
                status=PubMedProviderStatus.EMPTY,
                snapshot_id="pubmed:test",
                query=query,
            )

        def fetch_result(self, pmids, *, limit):
            return PubMedFetchResult(
                status=PubMedProviderStatus.SUCCESS,
                snapshot_id="pubmed:test",
                input_pmids=list(pmids),
                requested_pmids=list(pmids),
                records=[
                    PubMedEvidenceRecord(
                        pmid="123",
                        title="ZRSR2 localization",
                        abstract="ZRSR2 maps to chromosome X.",
                        snapshot_id="pubmed:test",
                        content_hash="a" * 64,
                        retrieved_at=NOW,
                    )
                ],
            )

    client = FakePubMed()
    provider = PubMedMaterialProvider(client)
    request = EvidenceProviderRequest(
        request_id="request.pubmed.relax",
        provider_id="pubmed",
        provider_version="pubmed-eutils-v1",
        operation_key="run|pubmed|relax",
        query={
            "term": (
                "ZRSR2 Assess all proposed hypotheses and identify every "
                "independent verification requirement"
            )
        },
        requested_at=NOW,
        material_limit=5,
    )
    result = provider.retrieve(request)

    assert result.terminal_status is ProviderTerminalStatus.EMPTY
    assert client.queries == [request.query["term"]]

    # A subsequent explicit controller query must return fetched records successfully.
    successful = provider.retrieve(request.model_copy(update={"query": {"term": "ZRSR2"}}))
    assert successful.terminal_status is ProviderTerminalStatus.SUCCEEDED
    assert successful.material_ids == ["material.pubmed.123"]
    assert successful.usage["provider_calls"] == 2
    assert "ZRSR2 maps to chromosome X." in successful.raw_content_by_material_id["material.pubmed.123"]


def test_pubmed_adapter_fetches_full_text_for_selected_materials() -> None:
    provider = PubMedMaterialProvider(
        FakePubMedWithoutContent(),
        fulltext_transport=lambda url: {
            "PubTator3": [
                {
                    "pmid": "123",
                    "passages": [
                        {
                            "infons": {"type": "title"},
                            "text": "ZRSR2 localization",
                        },
                        {
                            "infons": {"section_type": "results"},
                            "text": (
                                "ZRSR2 loss impaired minor intron splicing "
                                "and increased hematopoietic self-renewal."
                            ),
                        },
                    ],
                }
            ]
        },
    )
    request = EvidenceProviderRequest(
        request_id="request.pubmed.details",
        provider_id="pubmed",
        provider_version="pubmed-eutils-v1",
        operation_key="run|pubmed|details",
        query={"term": "ZRSR2"},
        requested_at=NOW,
        material_limit=5,
    )

    result = provider.retrieve_details(
        request,
        material_ids=["material.pubmed.123"],
    )

    assert result.terminal_status is ProviderTerminalStatus.SUCCEEDED
    assert result.materials[0]["material_type"] == "literature_full_text"
    content = result.raw_content_by_material_id["material.pubmed.123"]
    assert "[results]" in content
    assert "minor intron splicing" in content
    assert any(
        anchor["section_or_field"] == "results"
        and "hematopoietic self-renewal" in anchor["quoted_text"]
        for anchor in result.anchors
    )


class FakePubMedWithoutContent:
    def search_result(self, query, *, limit):
        return PubMedSearchResult(
            status=PubMedProviderStatus.EMPTY,
            snapshot_id="pubmed:test",
            query=query,
        )

    def fetch_result(self, pmids, *, limit):
        return PubMedFetchResult(
            status=PubMedProviderStatus.EMPTY,
            snapshot_id="pubmed:test",
            input_pmids=list(pmids),
        )


def test_open_article_fallback_keeps_late_results_and_original_anchor_offsets(tmp_path):
    from medical_qa_data_factory.generation.research_documents import parse_article
    document = parse_article("<article><front><article-meta><title-group><article-title>Study</article-title>"
        "</title-group></article-meta></front><body><sec><title>Introduction</title><p>" + "x" * 18000 +
        "</p></sec><sec><title>Results FcγRIIIa " + "detailed comparison " * 12 +
        "</title><p>Distinct outcome in the comparator group.</p></sec></body></article>")
    def failed_primary(url):
        raise ConnectionError("primary endpoint unavailable")
    provider = PubMedMaterialProvider(fulltext_transport=failed_primary, open_article_loader=lambda pmid: document)
    request = EvidenceProviderRequest(request_id="request.details.fallback", provider_id="pubmed",
        provider_version=provider.capability.provider_version, operation_key="fallback", query={"term": "Study"},
        requested_at=NOW, material_limit=1)
    result = provider.retrieve_details(request, material_ids=["material.pubmed.123"])
    assert result.terminal_status is ProviderTerminalStatus.SUCCEEDED
    assert "Distinct outcome" in result.raw_content_by_material_id["material.pubmed.123"]
    persistence = ObjectiveQuestionPersistence(tmp_path)
    for m in result.materials:
        persistence.save_material(EvidenceMaterial.model_validate(m), result.raw_content_by_material_id[m["material_id"]].encode())
    for a in result.anchors:
        assert a["anchor_id"].isascii()
        assert len(a["anchor_id"] + ".detail." + "a" * 16 + "." + "b" * 16) <= 128
        persistence.save_anchor(EvidenceAnchor.model_validate(a))
    assert result.usage["fallback_errors"]
