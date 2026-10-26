"""Deterministic mock adapters for local development only.

Artifacts produced by these adapters remain draft and must never be treated as
validated medical data.
"""
from __future__ import annotations

from .domain import (
    AnswerForm,
    Answerability,
    Claim,
    ClaimStatus,
    ClaimType,
    EndpointKind,
    EntityRef,
    EvidenceBundle,
    EvidenceDirection,
    EvidenceRequirements,
    GenerationMode,
    GoldAnswer,
    InvocationConfig,
    KGEvidence,
    LinkStatus,
    LiteratureEvidence,
    MeasurementSource,
    QuestionSpec,
    QuestionTaxonomy,
    QuestionVariant,
    Route,
    ToolSnapshot,
    UsageRecord,
    UsageScope,
    ValidationLevel,
)
from .domain.common import new_id, utc_now
from .ports import KGCollectionResult, LiteratureCollectionResult


def _mock_snapshot(name: str) -> ToolSnapshot:
    return ToolSnapshot(
        tool_name=name,
        tool_version="demo-v0",
        data_snapshot_id="demo-only",
        config_hash=f"{name}-demo-config",
        endpoint_kind=EndpointKind.MOCK,
        retrieved_at=utc_now(),
    )


def _invocation(name: str) -> InvocationConfig:
    return InvocationConfig(
        invocation_id=new_id("invocation"),
        provider="local-demo",
        model_id=None,
        request_parameters={},
        config_hash=f"{name}-demo-config",
    )


def _usage(name: str, invocation_id: str) -> UsageRecord:
    return UsageRecord(
        usage_id=new_id("usage"),
        usage_scope=UsageScope.GENERATION,
        invocation_id=invocation_id,
        tool_name=name,
        tool_calls=1,
        measurement_source=MeasurementSource.TOOL,
        meter_version="demo-meter-v0",
        recorded_at=utc_now(),
    )


class DemoKGTool:
    def collect(self, seed_entity: EntityRef) -> KGCollectionResult:
        invocation = _invocation("demo_kg")
        target = EntityRef(
            entity_ref_id=new_id("entity"),
            raw_mention="PTGS1",
            canonical_name="PTGS1",
            canonical_id="demo:PTGS1",
            namespace="demo",
            entity_type="gene",
            aliases=["COX-1"],
            link_status=LinkStatus.EXACT,
            link_score=1.0,
            source="demo_kg",
        )
        evidence = KGEvidence(
            evidence_id=new_id("kg"),
            head=seed_entity,
            relation="targets",
            tail=target,
            direction=EvidenceDirection.FORWARD,
            source_database="demo_kg",
            source_record_id="demo-record",
            confidence=1.0,
            retrieval_action={"kind": "entity_neighbors"},
            retrieved_at=utc_now(),
            metadata={"demo": True},
        )
        return KGCollectionResult(
            evidence=[evidence],
            invocation=invocation,
            usage=_usage("demo_kg", invocation.invocation_id),
            snapshot=_mock_snapshot("demo_kg"),
        )


class DemoLiteratureTool:
    def collect(self, seed_entity: EntityRef) -> LiteratureCollectionResult:
        invocation = _invocation("demo_literature")
        evidence = LiteratureEvidence(
            evidence_id=new_id("lit"),
            document_id="demo-document",
            title="Demo evidence — not a real publication",
            year=2026,
            publication_type="demo",
            evidence_text=(
                f"Development-only text about {seed_entity.canonical_name}; "
                "not valid medical evidence."
            ),
            location={"section": "demo"},
            entities=[seed_entity],
            retrieval_query=f"{seed_entity.canonical_name} mechanism",
            retrieval_rank=1,
            retrieved_at=utc_now(),
            license_note="synthetic development fixture",
        )
        return LiteratureCollectionResult(
            evidence=[evidence],
            invocation=invocation,
            usage=_usage("demo_literature", invocation.invocation_id),
            snapshot=_mock_snapshot("demo_literature"),
        )


class DemoDraftAgent:
    def propose_claims(
        self,
        seed_entity: EntityRef,
        kg_evidence: list[KGEvidence],
        literature_evidence: list[LiteratureEvidence],
    ) -> list[Claim]:
        claims: list[Claim] = []
        if kg_evidence:
            item = kg_evidence[0]
            claims.append(
                Claim(
                    claim_id=new_id("claim"),
                    claim_text=(
                        f"{item.head.canonical_name} targets "
                        f"{item.tail.canonical_name}."
                    ),
                    claim_type=ClaimType.ASSOCIATION,
                    subject=item.head,
                    predicate=item.relation,
                    object=item.tail,
                    supporting_evidence_ids=[item.evidence_id],
                    status=ClaimStatus.SUPPORTED,
                    confidence=1.0,
                    rationale="Deterministic demo claim from mock KG evidence.",
                )
            )
        if literature_evidence:
            item = literature_evidence[0]
            claims.append(
                Claim(
                    claim_id=new_id("claim"),
                    claim_text=f"A demo literature record mentions {seed_entity.canonical_name}.",
                    claim_type=ClaimType.FACT,
                    subject=seed_entity,
                    predicate="mentioned_in_demo_literature",
                    object=item.document_id,
                    supporting_evidence_ids=[item.evidence_id],
                    status=ClaimStatus.SUPPORTED,
                    confidence=1.0,
                    rationale="Deterministic demo claim from mock literature evidence.",
                )
            )
        return claims

    def draft_questions(
        self, bundle: EvidenceBundle, limit: int | None = None
    ) -> list[QuestionSpec]:
        if not bundle.claims or limit == 0:
            return []
        claim = bundle.claims[0]
        kg_ids = {item.evidence_id for item in bundle.kg_evidence}
        lit_ids = {item.evidence_id for item in bundle.literature_evidence}
        required_kg = [eid for eid in claim.supporting_evidence_ids if eid in kg_ids]
        required_lit = [eid for eid in claim.supporting_evidence_ids if eid in lit_ids]
        route = Route.HYBRID if required_kg and required_lit else (
            Route.KG if required_kg else Route.LITERATURE
        )
        answer = claim.object
        if isinstance(answer, EntityRef):
            values = [{"entity_id": answer.canonical_id, "name": answer.canonical_name}]
            short_answer = answer.canonical_name or answer.raw_mention
            answer_form = AnswerForm.ENTITY
        else:
            values = [{"value": answer}]
            short_answer = str(answer or "")
            answer_form = AnswerForm.FACT
        question_id = new_id("question")
        seed_name = bundle.seed_entities[0].canonical_name or "该种子实体"
        question_text = f"{seed_name}在该知识图谱中直接作用的靶点实体是什么？"
        return [
            QuestionSpec(
                question_id=question_id,
                logical_question_id=new_id("logical_question"),
                question_version=new_id("version"),
                bundle_ids=[bundle.bundle_id],
                generation_mode=GenerationMode.AUTOMATIC,
                canonical_question=question_text,
                question_variants=[
                    QuestionVariant(
                        variant_id=new_id("variant"),
                        text=question_text,
                        language="zh-CN",
                        persona="biomedical_researcher",
                        style="natural",
                        generation_source="agent",
                        semantic_check_status="warning",
                    )
                ],
                taxonomy=QuestionTaxonomy(
                    domain_intent="demo_fact",
                    answer_form=answer_form,
                    reasoning_pattern=["single_hop"],
                    hop_complexity=1,
                    difficulty_tags=["demo_only"],
                ),
                query_intent={"demo": True, "claim_id": claim.claim_id},
                answerability=Answerability.ANSWERABLE,
                gold_answer=GoldAnswer(
                    answer_mode=answer_form,
                    values=values,
                    short_answer=short_answer,
                    acceptable_variants=[],
                    confidence=1.0,
                ),
                evidence_requirements=EvidenceRequirements(
                    required_claim_ids=[claim.claim_id],
                    required_kg_evidence_ids=required_kg,
                    required_literature_evidence_ids=required_lit,
                    optional_evidence_ids=[],
                    checks=[{"type": "demo_only"}],
                    sufficiency_rule_version="demo-v0",
                ),
                expected_route=route,
                verified_route=Route.UNKNOWN,
                validation_level=ValidationLevel.DRAFT,
                split_group_id=bundle.seed_entities[0].canonical_id or bundle.bundle_id,
                created_at=utc_now(),
            )
        ]

