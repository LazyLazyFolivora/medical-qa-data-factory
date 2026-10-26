"""Independent style evaluation for user-like question candidates."""
from __future__ import annotations

import re
from typing import Any

from pydantic import Field

from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from .contracts import QuestionBlueprint, QuestionDraft
from .question_skills import QuestionStyleSkill, load_question_skill


class StyleAssessment(ContractModel):
    naturalness_score: float = Field(ge=0, le=1)
    persona_fit_score: float = Field(ge=0, le=1)
    intent_preserved: bool
    unsupported_details: bool = False
    technical_jargon: bool = False
    answer_leaked: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    feedback: str = ""


class StyleJudgement(ContractModel):
    draft_id: str = Field(min_length=1)
    score: float = Field(ge=0, le=1)
    blocking: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    feedback: str = ""


def deterministic_style_issues(
    draft: QuestionDraft,
    blueprint: QuestionBlueprint,
    skill: QuestionStyleSkill,
) -> tuple[list[str], float]:
    """Check style constraints that must not depend on model judgement."""
    reasons: list[str] = []
    text = draft.question_text.casefold()
    if len(draft.question_text) < skill.manifest.min_length:
        reasons.append("question_too_short")
    if len(draft.question_text) > skill.manifest.max_length:
        reasons.append("question_too_long")
    jargon = sorted(
        term
        for term in skill.manifest.forbidden_terms
        if re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(term.casefold())}(?![A-Za-z0-9_])",
            text,
        )
    )
    if jargon:
        reasons.append("technical_jargon")
    phrases = sorted(
        phrase
        for phrase in skill.manifest.forbidden_phrases
        if phrase.casefold() in text
    )
    if phrases:
        reasons.append("unsafe_medical_request")
    answer_names: set[str] = set()
    answer_names.update(
        str(value.get("canonical_name") or "").strip().casefold()
        for value in draft.candidate_answer.values
    )
    answer_names.update(
        str(item).strip().casefold()
        for item in draft.candidate_answer.acceptable_variants
    )
    answer_names.add(str(draft.candidate_answer.short_answer or "").strip().casefold())
    answer_names.discard("")
    if any(name in text for name in answer_names):
        reasons.append("answer_leakage")
    if blueprint.answer_entity_id.casefold() in text:
        reasons.append("answer_id_leakage")
    penalty = min(0.5, len(reasons) * 0.2)
    return sorted(set(reasons)), penalty


class StructuredStyleJudge:
    """Score user-style fidelity without exposing the candidate answer."""

    def __init__(self, model: Any) -> None:
        self.model = model

    def assess(
        self,
        draft: QuestionDraft,
        blueprint: QuestionBlueprint,
    ) -> StyleJudgement:
        if not draft.style_skill_id:
            return StyleJudgement(
                draft_id=draft.draft_id,
                score=0,
                blocking=True,
                reason_codes=["missing_style_skill"],
                feedback="the draft has no style skill",
            )
        skill = load_question_skill(draft.style_skill_id)
        if draft.style_skill_version != skill.version:
            return StyleJudgement(
                draft_id=draft.draft_id,
                score=0,
                blocking=True,
                reason_codes=["style_skill_version_mismatch"],
                feedback=(
                    f"draft requested style version {draft.style_skill_version!r}; "
                    f"loaded version is {skill.version!r}"
                ),
            )
        request = StructuredGenerationRequest(
            invocation_id=f"style.{draft.generation_episode_id}.{draft.draft_id}",
            role="style_judge",
            system_prompt_version="style-v1",
            skill_id=skill.skill_id,
            skill_version=skill.version,
            skill_instruction=skill.model_instruction(),
            payload={
                "question": draft.question_text,
                "style_profile": draft.style_profile,
                "profile_instruction": skill.manifest.profile_guides.get(
                    draft.style_profile or "", ""
                ),
                "language": draft.language,
                "intent": {
                    "focus_entity_id": blueprint.focus_entity_id,
                    "answer_entity_type": blueprint.answer_entity_type,
                    "question_type": blueprint.question_type,
                    "question_intent": blueprint.question_intent,
                    "semantic_skeleton": blueprint.semantic_skeleton,
                    "hop_complexity": blueprint.hop_complexity,
                    "required_relation_chain": blueprint.logical_query.get(
                        "steps", []
                    ),
                    "constraints": list(blueprint.leakage_constraints),
                },
                "style_constraints": {
                    "min_length": skill.manifest.min_length,
                    "max_length": skill.manifest.max_length,
                    "forbidden_terms": list(skill.manifest.forbidden_terms),
                },
                "forbidden": [
                    "do not infer or mention the candidate answer",
                    "do not add medical facts",
                    "do not reward database or implementation jargon",
                ],
            },
            idempotency_key=(
                f"{draft.generation_episode_id}:style_judge:"
                f"{draft.draft_id}:1"
            ),
        )
        response = self.model.generate_structured(request, StyleAssessment)
        assessment = StyleAssessment.model_validate(response.parsed)
        deterministic_reasons, penalty = deterministic_style_issues(
            draft, blueprint, skill
        )
        reasons = sorted(
            set(assessment.reason_codes) | set(deterministic_reasons)
        )
        score = max(
            0.0,
            min(
                1.0,
                (
                    assessment.naturalness_score
                    + assessment.persona_fit_score
                )
                / 2,
            )
            - penalty,
        )
        blocking = bool(
            not assessment.intent_preserved
            or assessment.unsupported_details
            or assessment.technical_jargon
            or assessment.answer_leaked
            or deterministic_reasons
        )
        return StyleJudgement(
            draft_id=draft.draft_id,
            score=score,
            blocking=blocking,
            reason_codes=reasons,
            feedback=assessment.feedback,
        )
