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
