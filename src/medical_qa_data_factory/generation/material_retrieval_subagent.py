"""Provider-scoped material retrieval with no factual interpretation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence_provider import (
    EvidenceProviderRequest,
    EvidenceProviderResult,
    ProviderTerminalStatus,
)
from .objective_question_contracts import (
    DomainOutcome,
    EvidenceAnchor,
    EvidenceMaterial,
    ExecutionStatus,
    SubagentResult,
    SubagentTask,
    VerificationVerdict,
)
from .objective_question_persistence import ObjectiveQuestionPersistence
from .subagent_contracts import (
    RoleContractViolation,
    SubagentRole,
    default_objective_role_registry,
    validate_objective_subagent_task,
)


@dataclass(frozen=True)
class MaterialRetrievalOutcome:
    result: SubagentResult
    provider_result: EvidenceProviderResult
    materials: list[EvidenceMaterial]
    anchors: list[EvidenceAnchor]


class MaterialRetrievalSubagent:
    role = SubagentRole.MATERIAL_RETRIEVAL

    def __init__(self, persistence: ObjectiveQuestionPersistence) -> None:
        self.persistence = persistence
        self.capability = default_objective_role_registry()[self.role]

    def run(
        self,
        task: SubagentTask,
        *,
        provider: Any,
        query: dict[str, Any],
    ) -> MaterialRetrievalOutcome:
        validate_objective_subagent_task(task, self.capability)
        provider_id = provider.capability.provider_id
        allowed_providers = set(task.goal.get("allowed_providers", []))
        if provider_id not in allowed_providers:
            raise RoleContractViolation(
                "provider is outside the task allowlist"
            )
        if task.allowed_tools and provider_id not in task.allowed_tools:
            raise RoleContractViolation(
                "provider is outside the task tool allowlist"
            )
        request = EvidenceProviderRequest(
            request_id=f"request.{task.subtask_id}",
            provider_id=provider_id,
            provider_version=provider.capability.provider_version,
            operation_key=task.operation_key,
            query=query,
            requested_at=_now(),
            material_limit=int(task.goal.get("material_limit", 10)),
        )
        provider_result = provider.retrieve(request)
        if provider_result.request != request:
            raise RoleContractViolation(
                "provider result does not correspond to the dispatched request"
            )
        terminal = provider_result.terminal_status
        if terminal is not ProviderTerminalStatus.SUCCEEDED:
            execution = (
                ExecutionStatus.UNKNOWN
                if terminal is ProviderTerminalStatus.UNKNOWN
                else (
                    ExecutionStatus.FAILED
                    if terminal is ProviderTerminalStatus.FAILED
                    else ExecutionStatus.SUCCEEDED
                )
            )
            stop_reason = {
                ProviderTerminalStatus.EMPTY: "no_materials",
                ProviderTerminalStatus.FAILED: "provider_failure",
                ProviderTerminalStatus.UNKNOWN: "unknown_outcome",
            }[terminal]
            return MaterialRetrievalOutcome(
                result=SubagentResult(
                    subtask_id=task.subtask_id,
                    execution_status=execution,
                    domain_outcome=DomainOutcome.INSUFFICIENT,
                    verdict=VerificationVerdict.NOT_EMITTED,
                    output_artifact_ids=[],
                    evidence_need_ids=[],
                    usage=dict(provider_result.usage),
                    error_category=provider_result.error_category,
                    stop_reason=stop_reason,
                ),
                provider_result=provider_result,
                materials=[],
                anchors=[],
            )

        materials = [
            EvidenceMaterial.model_validate(item)
            for item in provider_result.materials
        ]
        anchors = [
            EvidenceAnchor.model_validate(item)
            for item in provider_result.anchors
        ]
        if not materials:
            raise RoleContractViolation(
                "SUCCEEDED provider result contains no materials"
            )
        if any(
            material.provider_id != provider_id for material in materials
        ):
            raise RoleContractViolation(
                "provider returned material attributed to another provider"
            )
        for material in materials:
            raw_content = provider_result.raw_content_by_material_id[
                material.material_id
            ]
            self.persistence.save_material(
                material,
                raw_content.encode("utf-8"),
            )
        for anchor in anchors:
            self.persistence.save_anchor(anchor)
        result = SubagentResult(
            subtask_id=task.subtask_id,
            execution_status=ExecutionStatus.SUCCEEDED,
            domain_outcome=DomainOutcome.SUPPORTED,
            verdict=VerificationVerdict.NOT_EMITTED,
            output_artifact_ids=[
                *(material.material_id for material in materials),
                *(anchor.anchor_id for anchor in anchors),
            ],
            evidence_need_ids=[],
            usage=dict(provider_result.usage),
            error_category=None,
            stop_reason="materials_collected",
        )
        return MaterialRetrievalOutcome(
            result=result,
            provider_result=provider_result,
            materials=materials,
            anchors=anchors,
        )


def _now():
    from ..domain.common import utc_now

    return utc_now()
