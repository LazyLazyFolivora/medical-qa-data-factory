"""Question blueprint planning.

The answer is always the endpoint opposite the seed. Relations rotate so one
busy relation cannot monopolise the requested question count.
"""
from __future__ import annotations

from ..domain.questions import AnswerForm
from ..evidence_enrichment.models import (
    EnrichmentStatus,
    NormalizedEvidenceClaim,
    QuestionIntent,
)
from ..evidence_enrichment.service import EvidenceEnrichmentResult
from .contracts import QuestionBlueprint
from .curation import CuratedEvidence


class QuestionPlanner:
    def __init__(self, answer_form: AnswerForm = AnswerForm.ENTITY) -> None:
        self.answer_form = answer_form

    def plan(
        self,
        curated: list[CuratedEvidence],
        *,
        seed_canonical_id: str,
        question_count: int,
    ) -> list[QuestionBlueprint]:
        blueprints: list[QuestionBlueprint] = []
        for item in curated:
            if len(blueprints) >= question_count:
                break
            edge = item.evidence
            answer_entity = (
                edge.tail if item.answer_endpoint == "tail" else edge.head
            )
            blueprints.append(
                QuestionBlueprint(
                    blueprint_id=f"blueprint.{item.stable_edge_id}",
                    claim_ids=[f"claim.{item.stable_edge_id}"],
                    focus_entity_id=seed_canonical_id,
                    answer_entity_id=answer_entity.canonical_id or "",
                    answer_endpoint=item.answer_endpoint,  # type: ignore[arg-type]
                    answer_form=self.answer_form,
                    relation_semantics=item.semantics,
                    required_evidence_ids=[edge.evidence_id],
                    answer_entity_type=answer_entity.entity_type,
                )
            )
        return blueprints


def _answer_type_label(value: str | None) -> str:
    return {
        "disease": "疾病",
        "drug": "药物",
        "exposure": "暴露物",
        "gene/protein": "基因或蛋白",
        "effect/phenotype": "表型",
        "pathway": "通路",
        "anatomy": "解剖部位",
    }.get((value or "").casefold(), "实体")


def _natural_skeleton(claim: NormalizedEvidenceClaim, seed_name: str) -> str:
    target = _answer_type_label(claim.answer_entity_type)
    intent = claim.question_intent
    if intent is QuestionIntent.RISK_DECREASE:
        return f"使用{seed_name}与哪种{target}的风险降低相关？"
    if intent is QuestionIntent.RISK_INCREASE:
        return f"使用{seed_name}与哪种{target}的风险升高相关？"
    if intent is QuestionIntent.THERAPEUTIC_RESEARCH:
        return f"{seed_name}被研究用于治疗哪种{target}？"
    if intent is QuestionIntent.MECHANISM_RESEARCH:
        return f"{seed_name}被研究与哪种{target}的发生机制相关？"
    if intent is QuestionIntent.CONTRAINDICATION:
        return f"哪种{target}被记录为{seed_name}的禁忌情况？"
    return f"研究报道中，{seed_name}与哪种{target}存在关联？"


class EnrichedEntityQuestionPlanner:
    """Build entity-answer questions from evidence-enriched claims only.

    Existing GoldAnswer/QAItem contracts remain unchanged. A claim is eligible
    only when its subject, relation, intent and answer type identify exactly
    one answer entity in the enriched evidence pool.
    """

    def plan(
        self,
        curated: list[CuratedEvidence],
        enrichments: list[EvidenceEnrichmentResult],
        *,
        seed_canonical_id: str,
        question_count: int,
    ) -> list[QuestionBlueprint]:
        by_edge = {item.evidence.evidence_id: item for item in curated}
        claims = [
            result.claim
            for result in enrichments
            if result.status is EnrichmentStatus.SUPPORTED
            and result.claim is not None
        ]
        groups: dict[tuple[str, str, str, str], list[NormalizedEvidenceClaim]] = {}
        for claim in claims:
            key = (
                claim.seed_entity_id,
                claim.relation,
                claim.question_intent.value,
                claim.answer_entity_type or "",
            )
            groups.setdefault(key, []).append(claim)
        blueprints: list[QuestionBlueprint] = []
        for group in groups.values():
            answer_ids = {item.answer_entity_id for item in group}
            if len(answer_ids) != 1:
                continue
            claim = sorted(group, key=lambda item: item.claim_id)[0]
            curated_item = by_edge.get(claim.kg_edge_id)
            if curated_item is None:
                continue
            seed_ref = (
                curated_item.evidence.head
                if curated_item.evidence.head.canonical_id == seed_canonical_id
                else curated_item.evidence.tail
            )
            seed_name = seed_ref.canonical_name or seed_ref.raw_mention
            skeleton = _natural_skeleton(claim, seed_name)
            blueprints.append(
                QuestionBlueprint(
                    blueprint_id=f"blueprint.{claim.claim_id}",
                    claim_ids=[f"claim.{curated_item.stable_edge_id}"],
                    focus_entity_id=seed_canonical_id,
                    answer_entity_id=claim.answer_entity_id,
                    answer_endpoint=curated_item.answer_endpoint,  # type: ignore[arg-type]
                    question_type="single_fact",
                    answer_form=AnswerForm.ENTITY,
                    relation_semantics=curated_item.semantics,
                    question_intent=claim.question_intent.value,
                    semantic_skeleton=skeleton,
                    required_evidence_ids=[claim.kg_edge_id],
                    answer_entity_type=claim.answer_entity_type,
                    leakage_constraints=[
                        "preserve the evidence question intent",
                        "do not name the answer entity",
                        "do not widen the candidate set",
                    ],
                    logical_query={
                        "enrichment_claim_id": claim.claim_id,
                        "question_intent": claim.question_intent.value,
                        "polarity": claim.polarity.value,
                        "answer_candidate_count": len(answer_ids),
                        "required_literature_evidence_ids": list(
                            claim.literature_evidence_ids
                        ),
                        "support_level": claim.support_level.value,
                    },
                )
            )
            if len(blueprints) >= question_count:
                break
        return blueprints
