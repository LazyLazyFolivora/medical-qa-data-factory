"""Formal, evidence-first execution of a sealed literature action."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..domain import KGEvidence, LiteratureEvidence
from ..literature import (
    LiteratureCallStatus,
    LiteratureChunkReadRequest,
    LiteratureEvidenceNeed,
    LiteratureRetrievalPort,
    LiteratureRetrievalRequest,
)
from .evidence_fusion import (
    EvidenceSupportAssessment,
    FusedLiteratureEvidence,
    fuse_retrieval,
)
from .executor import ToolCallError
from .repository import GenerationRepository


#: Deterministic support evaluator version. Changing its rules requires a
#: new value so old assessments are not silently reinterpreted.
SUPPORT_EVALUATOR_VERSION = "deterministic-v1"


@dataclass
class LiteratureEvidenceExecutor:
    """Searches one need, reads every candidate back, and persists verified text."""

    provider: LiteratureRetrievalPort
    repository_root: str
    episode_id: str
    run_id: str
    knowledge_base_ids: Sequence[str]
    needs: dict[str, LiteratureEvidenceNeed]
    evidence_by_id: dict[str, KGEvidence]
    provider_id: str | None = None
    on_satisfied: Callable[[str], None] | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def preflight(self):
        capability = self.provider.preflight()
        if not capability.supports_search or not capability.supports_read_chunk:
            raise ToolCallError(
                "literature_capability_unavailable",
                "the literature provider cannot search and read chunks back",
            )
        if not capability.supports_pinned_snapshot:
            raise ToolCallError(
                "literature_snapshot_unavailable",
                "formal evidence requires a pinned literature snapshot",
            )
        return capability

    def search(
        self, request: dict[str, object]
    ) -> tuple[list[str], str, str | None]:
        need_ids = [str(item) for item in request.get("need_ids", [])]
        if not need_ids:
            raise ToolCallError(
                "need_not_found", "the sealed action serves no evidence need"
            )
        need_id = need_ids[0]
        try:
            need = self.needs[need_id]
        except KeyError as exc:
            raise ToolCallError(
                "need_not_found", f"evidence need {need_id!r} is not open"
            ) from exc
        capability = self.preflight()
        expected_snapshot = (
            capability.snapshot.snapshot_id if capability.snapshot else None
        )
        query = str(request.get("query") or need.information_need)
        limit = int(request.get("max_results", 10))
        retrieval_request = LiteratureRetrievalRequest(
            request_id=f"{self.episode_id}.literature.{need_id}",
            sealed_action_id=request.get("sealed_action_id"),
            need_id=need.need_id,
            query=query,
            knowledge_base_ids=list(self.knowledge_base_ids),
            entity_ids=list(need.filters.get("entity_ids", [])),
            filters=dict(need.filters),
            limit=limit,
            expected_snapshot_id=expected_snapshot,
            provider_capability_version=capability.capability_version,
        )
        retrieval = self.provider.search(retrieval_request)
        self.calls.append(
            {
                "need_id": need.need_id,
                "query": query,
                "status": retrieval.status.value,
                "trace_id": retrieval.trace_id,
            }
        )
        if retrieval.status is LiteratureCallStatus.EMPTY:
            raise ToolCallError(
                "literature_empty",
                "the literature provider returned no candidate chunk",
            )
        if retrieval.status is not LiteratureCallStatus.SUCCESS:
            raise ToolCallError(
                retrieval.error_code or "literature_error",
                retrieval.message or f"literature retrieval ended as {retrieval.status.value}",
                retryable=retrieval.status
                in (LiteratureCallStatus.TIMEOUT, LiteratureCallStatus.RATE_LIMITED),
            )
        read_backs = {}
        for chunk in retrieval.chunks:
            read_backs[chunk.chunk_id] = self.provider.read_chunk(
                LiteratureChunkReadRequest(
                    request_id=f"{retrieval_request.request_id}.read.{chunk.chunk_id}",
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id or None,
                    expected_snapshot_id=expected_snapshot,
                    provider_capability_version=capability.capability_version,
                )
            )
        outcome = fuse_retrieval(
            retrieval,
            read_backs,
            need=need,
            provider=self.provider_id or capability.provider,
            entity_link_version="entity-link-v1",
            retrieval_query=query,
            evaluator_config_id=SUPPORT_EVALUATOR_VERSION,
        )
        if not outcome.evidence:
            detail = "; ".join(reason for _, reason in outcome.unverified)
            raise ToolCallError(
                "literature_readback_unverified",
                detail or "no search result survived independent read-back",
            )
        evidence_ids: list[str] = []
        repository = GenerationRepository(self.repository_root)
        for fused, assessment in zip(outcome.evidence, outcome.assessments):
            evidence = _to_domain_evidence(
                fused,
                need=need,
                kg=self.evidence_by_id.get(need.kg_evidence_id or ""),
                assessment=assessment,
            )
            repository.save_idempotent(
                "literature_evidence",
                evidence.evidence_id,
                evidence.evidence_id,
                evidence,
            )
            repository.save_idempotent(
                "literature_support_assessments",
                assessment.assessment_id,
                assessment.assessment_version,
                assessment,
            )
            evidence_ids.append(evidence.evidence_id)
        if self.on_satisfied is not None:
            self.on_satisfied(need.need_id)
        return evidence_ids, retrieval.trace_id or retrieval_request.request_id, expected_snapshot


def _to_domain_evidence(
    fused: FusedLiteratureEvidence,
    *,
    need: LiteratureEvidenceNeed,
    kg: KGEvidence | None,
    assessment: EvidenceSupportAssessment,
) -> LiteratureEvidence:
    entities = []
    if kg is not None:
        entities = [kg.head, kg.tail]
    return LiteratureEvidence(
        evidence_id=fused.evidence_id,
        document_id=fused.document_id,
        title=fused.title,
        evidence_text=fused.evidence_text,
        location={
            "chunk_id": fused.chunk_id,
            "chunk_index": fused.location.get("chunk_index"),
            "start_at": fused.location.get("start_at"),
            "end_at": fused.location.get("end_at"),
            "content_hash": fused.content_hash,
            "provider_snapshot_id": fused.index_snapshot_id,
            "retrieval_trace_id": fused.trace_id,
            "read_trace_id": fused.read_trace_id,
            "need_ids": list(fused.need_ids),
            "kg_evidence_id": need.kg_evidence_id,
            "support_verdict": assessment.verdict.value,
            "round_trip_verified": fused.round_trip_verified,
        },
        entities=entities,
        retrieval_query=fused.retrieval_query,
        retrieval_rank=fused.retrieval_rank,
        retrieved_at=fused.retrieved_at,
    )
