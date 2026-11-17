"""Iterative evidence search and candidate screening."""
from __future__ import annotations

from datetime import datetime, timezone

from medical_qa_data_factory.generation.evidence_provider import (
    EvidenceProviderCapability,
    EvidenceProviderRequest,
    EvidenceProviderResult,
    ProviderTerminalStatus,
    SideEffectClass,
)
from medical_qa_data_factory.generation.evidence_search_agent import (
    EvidenceSearchAgent,
    SearchRoundDecision,
)
from medical_qa_data_factory.generation.objective_question_contracts import (
    EvidenceAnchor,
    EvidenceMaterial,
    SubagentTask,
    text_digest,
)
from medical_qa_data_factory.generation.objective_question_persistence import (
    ObjectiveQuestionPersistence,
    immutable_blob_ref,
)
from medical_qa_data_factory.generation.subagent_contracts import (
    SubagentRole,
    build_objective_subagent_task,
    default_objective_role_registry,
)
from medical_qa_data_factory.llm.fake import FakeStructuredModel


NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


class CandidateProvider:
    def __init__(self) -> None:
        self.capability = EvidenceProviderCapability(
            provider_id="pubmed",
            provider_version="v1",
            side_effect_class=SideEffectClass.READ_ONLY,
            supports_idempotency=True,
            supports_status_lookup=False,
            safe_to_repeat=True,
        )
        self.requests: list[EvidenceProviderRequest] = []

    def lookup_status(self, operation_key: str):
        return None

    def retrieve(
        self,
        request: EvidenceProviderRequest,
    ) -> EvidenceProviderResult:
        self.requests.append(request)
        term = str(request.query.get("term", ""))
        content = (
            "Off-target mention."
            if term == "broad"
            else "BRCA1 is located on human chromosome 17."
        )
        material_id = (
            "material.precise" if term != "broad" else "material.broad"
        )
        anchor_id = f"anchor.{material_id}"
        material = EvidenceMaterial(
            material_id=material_id,
            provider_id="pubmed",
            provider_version="v1",
            source_record_id=f"pmid:{term}",
            retrieved_at=NOW,
            content_hash=text_digest(content),
            raw_content_ref=immutable_blob_ref(text_digest(content)),
            material_type="literature_abstract",
            retrieval_request_id=request.request_id,
            terminal_status=ProviderTerminalStatus.SUCCEEDED,
        )
        anchor = EvidenceAnchor(
            anchor_id=anchor_id,
            material_id=material_id,
            document_id=f"pmid:{term}",
            section_or_field="title_and_abstract",
            start_offset=0,
            end_offset=len(content),
            quoted_text=content,
            quote_hash=text_digest(content),
        )
        return EvidenceProviderResult(
            request=request,
            terminal_status=ProviderTerminalStatus.SUCCEEDED,
            material_ids=[material_id],
            anchor_ids=[anchor_id],
            materials=[material.model_dump(mode="json")],
            anchors=[anchor.model_dump(mode="json")],
            raw_content_by_material_id={material_id: content},
            snapshot_id="pubmed:test",
        )


def _task() -> SubagentTask:
    capability = default_objective_role_registry()[
        SubagentRole.MATERIAL_RETRIEVAL
    ]
    return build_objective_subagent_task(
        task_id="subtask-search-agent",
        role=SubagentRole.MATERIAL_RETRIEVAL,
        capability=capability,
        goal={
            "target_entity": "BRCA1",
            "evidence_need": "find chromosome location evidence",
            "allowed_providers": ["pubmed"],
            "material_limit": 5,
        },
        input_artifact_ids=[],
        allowed_tools=["pubmed"],
    )


def test_search_agent_refines_and_selects_only_relevant_materials(tmp_path) -> None:
    def decide(request):
        terms = {
            item["preview"] for item in request.payload["candidates"]
        } if request.payload["candidates"] else set()
        if any("chromosome 17" in item for item in terms):
            return SearchRoundDecision(
                selected_material_ids=["material.precise"],
                sufficient=True,
                rationale="the precise query found the target fact",
            )
        return SearchRoundDecision(
            follow_up_queries=[
                {"term": "BRCA1 chromosome 17"}
            ],
            sufficient=False,
            rationale="the broad query was off target",
        )

    model = FakeStructuredModel(
        responses={"material_retrieval": decide}
    )
    provider = CandidateProvider()
    outcome = EvidenceSearchAgent(
        model=model,
        persistence=ObjectiveQuestionPersistence(tmp_path / "ws"),
        max_rounds=3,
    ).run(
        _task(),
        provider=provider,
        initial_query={"term": "broad"},
    )

    assert [request.query["term"] for request in provider.requests] == [
        "broad",
        "BRCA1 chromosome 17",
    ]
    assert [item.material_id for item in outcome.materials] == [
        "material.precise"
    ]
    trace = outcome.provider_result.usage["search_trace"]
    assert len(trace) == 2
    assert outcome.result.execution_status.value == "SUCCEEDED"


def test_search_agent_falls_back_when_the_search_model_fails(tmp_path) -> None:
    model = FakeStructuredModel(responses={})
    provider = CandidateProvider()
    outcome = EvidenceSearchAgent(
        model=model,
        persistence=ObjectiveQuestionPersistence(tmp_path / "ws"),
    ).run(
        _task(),
        provider=provider,
        initial_query={"term": "precise"},
    )

    assert [item.material_id for item in outcome.materials] == [
        "material.precise"
    ]
    assert outcome.provider_result.usage["search_rationale"].startswith(
        "search model failed"
    )


def test_search_agent_replaces_selected_abstract_with_full_text(
    tmp_path,
) -> None:
    class DetailProvider(CandidateProvider):
        def retrieve_details(self, request, *, material_ids):
            material_id = "material.precise"
            content = (
                "BRCA1 is located on human chromosome 17. "
                "The result was reproduced in an independent cohort."
            )
            digest = text_digest(content)
            return EvidenceProviderResult(
                request=request,
                terminal_status=ProviderTerminalStatus.SUCCEEDED,
                material_ids=[material_id],
                anchor_ids=["anchor.precise.results"],
                materials=[
                    EvidenceMaterial(
                        material_id=material_id,
                        provider_id="pubmed",
                        provider_version="v1",
                        source_record_id="pmid:precise",
                        retrieved_at=NOW,
                        content_hash=digest,
                        raw_content_ref=immutable_blob_ref(digest),
                        material_type="literature_full_text",
                        retrieval_request_id=request.request_id,
                        terminal_status=ProviderTerminalStatus.SUCCEEDED,
                    ).model_dump(mode="json")
                ],
                anchors=[
                    EvidenceAnchor(
                        anchor_id="anchor.precise.results",
                        material_id=material_id,
                        document_id="pmid:precise",
                        section_or_field="results",
                        start_offset=0,
                        end_offset=len(content),
                        quoted_text=content,
                        quote_hash=digest,
                    ).model_dump(mode="json")
                ],
                raw_content_by_material_id={material_id: content},
            )

    model = FakeStructuredModel(
        responses={
            "material_retrieval": lambda request: SearchRoundDecision(
                selected_material_ids=["material.precise"],
                sufficient=True,
                rationale="selected the precise result",
            )
        }
    )
    outcome = EvidenceSearchAgent(
        model=model,
        persistence=ObjectiveQuestionPersistence(tmp_path / "ws"),
    ).run(
        _task(),
        provider=DetailProvider(),
        initial_query={"term": "precise"},
    )

    assert outcome.materials[0].material_type == "literature_full_text"
    assert "independent cohort" in outcome.provider_result.raw_content_by_material_id[
        "material.precise"
    ]
    assert outcome.provider_result.usage["detail_trace"][0][
        "terminal_status"
    ] == "SUCCEEDED"

def test_search_failure_preserves_original_error_in_trace(tmp_path):
    class FailedProvider(CandidateProvider):
        def retrieve(self, request):
            return EvidenceProviderResult(
                request=request,
                terminal_status=ProviderTerminalStatus.FAILED,
                error_category="PUBMED_PROVIDER_ERROR",
                usage={"error_message": "Read timed out (read timeout=12)"},
            )

    outcome = EvidenceSearchAgent(
        model=FakeStructuredModel(responses={}),
        persistence=ObjectiveQuestionPersistence(tmp_path / "ws"),
        max_rounds=1,
    ).run(_task(), provider=FailedProvider(), initial_query={"term": "PHYHIP"})
    trace = outcome.provider_result.usage["search_trace"]
    assert trace[0]["query"] == {"term": "PHYHIP"}
    assert trace[0]["error_message"] == "Read timed out (read timeout=12)"
