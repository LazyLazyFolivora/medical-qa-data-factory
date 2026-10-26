"""Question writing.

``RuleBasedQuestionWriter`` keeps the wording neutral and never leaks the
answer entity into the question text.
"""
from __future__ import annotations

import re
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field

from ..domain import EntityRef, GoldAnswer, KGEvidence
from ..domain.common import ContractModel
from ..domain import LiteratureEvidence
from ..llm.contracts import StructuredGenerationRequest
from .contracts import GenerationSpec
from .contracts import QuestionBlueprint, QuestionDraft
from .question_skills import (
    DEFAULT_QUESTION_STYLE_ID,
    load_question_skills,
)
from .semantics import RelationSemanticsPolicy


def _question_diversity_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


@runtime_checkable
class QuestionWriter(Protocol):
    def write(
        self,
        blueprints: list[QuestionBlueprint],
        *,
        evidence: list[KGEvidence],
        entities: dict[str, EntityRef],
        episode_id: str,
        natives: dict[str, str] | None = None,
        literature_evidence: list[LiteratureEvidence] | None = None,
        generation_spec: GenerationSpec | None = None,
    ) -> list[QuestionDraft]: ...


def build_candidate_answer(
    blueprint: QuestionBlueprint,
    entities: dict[str, EntityRef],
    natives: dict[str, str] | None = None,
) -> GoldAnswer | None:
    """Build the answer strictly from the blueprint endpoint."""
    entity = entities.get(blueprint.answer_entity_id)
    if entity is None:
        return None
    namespace = entity.namespace or ""
    native_id = (natives or {}).get(blueprint.answer_entity_id)
    values = [
        {
            "provider_id": namespace.split(":")[0] if namespace else None,
            "canonical_id": entity.canonical_id,
            "native_id": native_id or entity.raw_mention,
            "entity_type": entity.entity_type,
            "canonical_name": entity.canonical_name,
        }
    ]
    return GoldAnswer(
        answer_mode=blueprint.answer_form,
        values=values,
        short_answer=entity.canonical_name or "",
        explanation=None,
        acceptable_variants=[],
        # Unconfirmed until a human confirms the candidate.
        confidence=None,
    )


class RuleBasedQuestionWriter:
    """Relation-safe neutral phrasing derived from the semantics policy."""

    def __init__(self, policy: RelationSemanticsPolicy | None = None) -> None:
        self.policy = policy or RelationSemanticsPolicy()

    def write(
        self,
        blueprints: list[QuestionBlueprint],
        *,
        evidence: list[KGEvidence],
        entities: dict[str, EntityRef],
        episode_id: str,
        natives: dict[str, str] | None = None,
        literature_evidence: list[LiteratureEvidence] | None = None,
        generation_spec: GenerationSpec | None = None,
    ) -> list[QuestionDraft]:
        by_id = {item.evidence_id: item for item in evidence}
        drafts: list[QuestionDraft] = []
        for blueprint in blueprints:
            answer = build_candidate_answer(blueprint, entities, natives)
            if answer is None:
                continue
            edge = next(
                (
                    item
                    for item in (by_id[key] for key in blueprint.required_evidence_ids)
                    if item is not None
                ),
                None,
            )
            if edge is None:
                continue
            focus = entities.get(blueprint.focus_entity_id)
            relation = edge.relation
            template = self.policy.template_for(relation, blueprint.answer_endpoint)
            text = template.format(
                focus=(focus.canonical_name if focus else blueprint.focus_entity_id),
                relation=relation,
            )
            drafts.append(
                QuestionDraft(
                    draft_id=f"draft.{blueprint.blueprint_id}",
                    blueprint_id=blueprint.blueprint_id,
                    question_text=text,
                    candidate_answer=answer,
                    claim_ids=list(blueprint.claim_ids),
                    evidence_ids=list(blueprint.required_evidence_ids),
                    generation_episode_id=episode_id,
                )
            )
        return drafts


class StructuredWriterCandidate(ContractModel):
    """The writer's constrained output.

    Entity and evidence identities are explicit so the server can verify them.
    The server, not the writer, fills claim ids, draft ids and the final
    candidate-answer structure.
    """

    question_text: str = Field(min_length=1)
    answer_entity_id: str = Field(min_length=1)
    kg_evidence_ids: list[str] = Field(min_length=1)
    literature_evidence_ids: list[str] = Field(default_factory=list)
    question_type: Literal[
        "single_fact", "constrained_entity", "relation_chain"
    ] = "single_fact"
    language: str = Field(default="zh-CN", min_length=2)


class StructuredQuestionWriter:
    """Structured writer over the episode's frozen evidence only."""

    def __init__(
        self,
        model: Any,
        *,
        policy: RelationSemanticsPolicy | None = None,
    ) -> None:
        self.model = model
        self.policy = policy or RelationSemanticsPolicy()

    def write(
        self,
        blueprints: list[QuestionBlueprint],
        *,
        evidence: list[KGEvidence],
        entities: dict[str, EntityRef],
        episode_id: str,
        natives: dict[str, str] | None = None,
        literature_evidence: list[LiteratureEvidence] | None = None,
        generation_spec: GenerationSpec | None = None,
    ) -> list[QuestionDraft]:
        evidence_by_id = {item.evidence_id: item for item in evidence}
        literature = list(literature_evidence or [])
        allowed_literature_ids = {item.evidence_id for item in literature}
        drafts: list[QuestionDraft] = []
        style_ids = (
            generation_spec.question_style_ids
            if generation_spec is not None
            else [DEFAULT_QUESTION_STYLE_ID]
        )
        variants_per_blueprint = (
            generation_spec.variants_per_blueprint
            if generation_spec is not None
            else 1
        )
        style_skills = load_question_skills(style_ids)
        self.last_rejections: list[dict[str, Any]] = []
        seen_question_texts: set[tuple[str, str]] = set()
        for blueprint in blueprints:
            required_kg = list(blueprint.required_evidence_ids)
            required_evidence = [
                evidence_by_id[item]
                for item in required_kg
                if item in evidence_by_id
            ]
            if len(required_evidence) != len(required_kg):
                raise ValueError(
                    "the structured writer received an incomplete evidence set"
                )
            answer = build_candidate_answer(blueprint, entities, natives)
            if answer is None:
                raise ValueError(
                    f"the blueprint answer {blueprint.answer_entity_id!r} has no entity"
                )
            for variant_index in range(variants_per_blueprint):
                skill = style_skills[variant_index % len(style_skills)]
                profile_index = variant_index // len(style_skills)
                style_profile = skill.manifest.style_profiles[
                    profile_index % len(skill.manifest.style_profiles)
                ]
                draft_id = (
                    f"draft.{blueprint.blueprint_id}."
                    f"{skill.skill_id}.{variant_index + 1}"
                )
                if blueprint.question_type not in skill.manifest.question_types:
                    self.last_rejections.append(
                        {
                            "draft_id": draft_id,
                            "skill_id": skill.skill_id,
                            "skill_version": skill.version,
                            "style_profile": style_profile,
                            "message": (
                                f"skill does not support question type "
                                f"{blueprint.question_type!r}"
                            ),
                        }
                    )
                    continue
                request = StructuredGenerationRequest(
                    invocation_id=(
                        f"writer.{episode_id}.{blueprint.blueprint_id}."
                        f"{skill.skill_id}.{variant_index + 1}"
                    ),
                    role="question_writer",
                    system_prompt_version="writer-v1",
                    skill_id=skill.skill_id,
                    skill_version=skill.version,
                    skill_instruction=skill.model_instruction(),
                    payload={
                    "episode_id": episode_id,
                    "blueprint": blueprint.model_dump(mode="json"),
                    "question_intent": blueprint.question_intent,
                    "semantic_skeleton": blueprint.semantic_skeleton,
                    "candidate_answer": {
                        "canonical_entity_id": blueprint.answer_entity_id,
                        "text": answer.short_answer,
                    },
                    "kg_evidence": [
                        {
                            "evidence_id": item.evidence_id,
                            "head": item.head.model_dump(mode="json"),
                            "relation": item.relation,
                            "tail": item.tail.model_dump(mode="json"),
                            "direction": item.direction.value,
                        }
                        for item in required_evidence
                    ],
                    "literature_evidence": [
                        {
                            "evidence_id": item.evidence_id,
                            "title": item.title,
                            "text": item.evidence_text,
                            "location": dict(item.location),
                        }
                        for item in literature
                        if item.location.get("kg_evidence_id")
                        in set(required_kg)
                    ],
                    "allowed_entity_ids": sorted(entities),
                    "target_difficulties": (
                        [
                            item.value
                            for item in generation_spec.difficulty_targets
                        ]
                        if generation_spec
                        else ["easy"]
                    ),
                    "language": (
                        generation_spec.language
                        if generation_spec is not None
                        else "zh-CN"
                    ),
                    "forbidden": [
                        "do not state the answer",
                        "do not introduce an entity or fact not present in the payload",
                        "do not reinterpret an association as causation or treatment",
                        *skill.manifest.forbidden_terms,
                    ],
                    "style": {
                        "skill_id": skill.skill_id,
                        "skill_version": skill.version,
                        "display_name": skill.display_name,
                        "profile": style_profile,
                        "profile_instruction": skill.manifest.profile_guides.get(
                            style_profile, ""
                        ),
                    },
                    },
                    idempotency_key=(
                        f"{episode_id}:question_writer:"
                        f"{blueprint.blueprint_id}:{skill.skill_id}:{variant_index + 1}"
                    ),
                )
                try:
                    response = self.model.generate_structured(
                        request, StructuredWriterCandidate
                    )
                    candidate = StructuredWriterCandidate.model_validate(
                        response.parsed
                    )
                    if candidate.answer_entity_id != blueprint.answer_entity_id:
                        raise ValueError(
                            "the writer changed the blueprint's answer identity"
                        )
                    if not set(blueprint.required_evidence_ids) <= set(
                        candidate.kg_evidence_ids
                    ):
                        raise ValueError(
                            "the writer omitted required KG evidence"
                        )
                    if not set(candidate.kg_evidence_ids) <= set(evidence_by_id):
                        raise ValueError(
                            "the writer cited KG evidence outside the frozen episode"
                        )
                    if not set(candidate.literature_evidence_ids) <= allowed_literature_ids:
                        raise ValueError(
                            "the writer cited literature outside the frozen episode"
                        )
                    if (
                        generation_spec is not None
                        and candidate.language != generation_spec.language
                    ):
                        raise ValueError(
                            "the writer output language does not match the generation spec"
                        )
                    if answer.short_answer.lower() in candidate.question_text.lower():
                        raise ValueError(
                            "the writer leaked the answer into the question"
                        )
                    diversity_key = (
                        blueprint.blueprint_id,
                        _question_diversity_key(candidate.question_text),
                    )
                    if diversity_key in seen_question_texts:
                        raise ValueError(
                            "the writer produced a duplicate question among style variants"
                        )
                    seen_question_texts.add(diversity_key)
                    drafts.append(
                        QuestionDraft(
                            draft_id=draft_id,
                            blueprint_id=blueprint.blueprint_id,
                            question_text=candidate.question_text,
                            candidate_answer=answer,
                            claim_ids=list(blueprint.claim_ids),
                            evidence_ids=list(candidate.kg_evidence_ids),
                            literature_evidence_ids=list(
                                candidate.literature_evidence_ids
                            ),
                            question_type=candidate.question_type,
                            language=candidate.language,
                            style_skill_id=skill.skill_id,
                            style_skill_version=skill.version,
                            style_profile=style_profile,
                            question_intent=blueprint.question_intent,
                            semantic_skeleton=blueprint.semantic_skeleton,
                            generation_episode_id=episode_id,
                        )
                    )
                except ValueError as exc:
                    self.last_rejections.append(
                        {
                            "draft_id": draft_id,
                            "skill_id": skill.skill_id,
                            "skill_version": skill.version,
                            "style_profile": style_profile,
                            "message": str(exc),
                        }
                    )
        return drafts
