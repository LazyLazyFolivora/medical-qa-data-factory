"""Answer verification.

The verifier returns a machine suggestion, not a human decision. A plain
``rejected`` verdict is advisory: a structurally valid question still reaches
the human queue unless a hard error applies.
"""
from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field

from ..domain import Claim, KGEvidence, LiteratureEvidence
from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from .contracts import (
    QuestionBlueprint,
    QuestionDraft,
    VerificationResult,
)
from .semantics import RelationSemanticsPolicy


@runtime_checkable
class AnswerVerifier(Protocol):
    def verify(
        self,
        draft: QuestionDraft,
        claims: list[Claim],
        evidence: list[KGEvidence],
        *,
        focus_entity_id: str | None = None,
    ) -> VerificationResult: ...


def check_relation_semantics_safety(
    question_text: str, relation: str, policy: RelationSemanticsPolicy
) -> tuple[str | None, str]:
    """Blocking only for explicit causal or therapeutic misstatement."""
    lowered = question_text.lower()
    offenders = sorted(
        word for word in policy.forbidden_words(relation) if word.lower() in lowered
    )
    if offenders:
        return (
            "relation_semantics_violation",
            f"relation '{relation}' does not assert: {', '.join(offenders)}",
        )
    return None, ""


class RuleBasedAnswerVerifier:
    """Checks references, answer endpoint, leakage, and wording safety."""

    def __init__(self, policy: RelationSemanticsPolicy | None = None) -> None:
        self.policy = policy or RelationSemanticsPolicy()

    def verify(
        self,
        draft: QuestionDraft,
        claims: list[Claim],
        evidence: list[KGEvidence],
        *,
        focus_entity_id: str | None = None,
        literature_evidence: list[LiteratureEvidence] | None = None,
    ) -> VerificationResult:
        reason_codes: list[str] = []
        messages: list[str] = []
        blocking = False
        claim_ids = {item.claim_id for item in claims}
        evidence_by_id = {item.evidence_id: item for item in evidence}
        # The seed identity must be stated explicitly. Deriving it from the
        # head/tail position of one edge is wrong: an incoming edge stores the
        # seed as its head, an outgoing edge stores it as its tail.
        seed_canonical_ids = (
            {focus_entity_id} if focus_entity_id else set()
        )

        unknown_claims = sorted(set(draft.claim_ids) - claim_ids)
        if unknown_claims:
            return VerificationResult(
                draft_id=draft.draft_id,
                verdict="rejected",
                reason_codes=["unknown_claim_reference"],
                message=f"unknown claim ids: {unknown_claims}",
                blocking=True,
            )
        unknown_evidence = sorted(set(draft.evidence_ids) - set(evidence_by_id))
        if unknown_evidence:
            return VerificationResult(
                draft_id=draft.draft_id,
                verdict="rejected",
                reason_codes=["unknown_evidence_reference"],
                message=f"unknown evidence ids: {unknown_evidence}",
                blocking=True,
            )

        # The answer must equal the endpoint the blueprint promised.
        endpoints: list[str] = []
        for evidence_id in draft.evidence_ids:
            edge = evidence_by_id[evidence_id]
            for value in draft.candidate_answer.values:
                if value.get("canonical_id") == edge.tail.canonical_id:
                    endpoints.append("tail")
                elif value.get("canonical_id") == edge.head.canonical_id:
                    endpoints.append("head")
        if not endpoints:
            reason_codes.append("answer_not_on_evidence")
            messages.append("candidate answer matches no evidence endpoint")
            blocking = True
        else:
            seed_answers = {
                value.get("canonical_id") for value in draft.candidate_answer.values
            }
            if len(seed_answers) == 1 and seed_answers & seed_canonical_ids:
                reason_codes.append("answer_is_seed")
                messages.append("candidate answer is the seed entity itself")
                blocking = True
            answer_name = (draft.candidate_answer.short_answer or "").strip().lower()
            if answer_name and answer_name in draft.question_text.lower():
                reason_codes.append("answer_leakage")
                messages.append("answer text appears in the question")
                blocking = True

        for evidence_id in draft.evidence_ids:
            edge = evidence_by_id[evidence_id]
            code, message = check_relation_semantics_safety(
                draft.question_text, edge.relation, self.policy
            )
            if code:
                reason_codes.append(code)
                messages.append(message)
                blocking = True

        if blocking:
            return VerificationResult(
                draft_id=draft.draft_id,
                verdict="rejected",
                reason_codes=sorted(set(reason_codes)),
                message="; ".join(messages),
                blocking=True,
            )
        if reason_codes:
            return VerificationResult(
                draft_id=draft.draft_id,
                verdict="needs_revision",
                reason_codes=sorted(set(reason_codes)),
                message="; ".join(messages),
                blocking=False,
            )
        return VerificationResult(
            draft_id=draft.draft_id,
            verdict="accepted",
            reason_codes=[],
            message="references, answer endpoint, and wording are consistent",
            blocking=False,
        )

    def verify_with_blueprint(
        self,
        draft: QuestionDraft,
        blueprint: QuestionBlueprint,
        claims: list[Claim],
        evidence: list[KGEvidence],
        literature_evidence: list[LiteratureEvidence] | None = None,
    ) -> VerificationResult:
        result = self.verify(
            draft,
            claims,
            evidence,
            focus_entity_id=blueprint.focus_entity_id,
            literature_evidence=literature_evidence,
        )
        expected = {
            value.get("canonical_id") for value in draft.candidate_answer.values
        }
        if expected and blueprint.answer_entity_id not in expected:
            return VerificationResult(
                draft_id=draft.draft_id,
                verdict="rejected",
                reason_codes=["answer_endpoint_mismatch"],
                message=(
                    "candidate answer does not match the blueprint answer endpoint "
                    f"{blueprint.answer_endpoint}"
                ),
                blocking=True,
            )
        return result


class IndependentVerification(ContractModel):
    """The verifier's answer-blind output.

    It never receives the writer's candidate answer or rationale. The server
    compares its recomputed identities with the candidate afterward.
    """

    draft_id: str = Field(min_length=1)
    recomputed_answer_ids: list[str] = Field(default_factory=list)
    alternative_answer_ids: list[str] = Field(default_factory=list)
    evidence_supported: bool = False
    answer_leaked: bool = False
    difficulty: Literal["easy", "medium", "hard"] | None = None
    reason_codes: list[str] = Field(default_factory=list)


class StructuredAnswerVerifier:
    """Structured verifier with a context independent from the writer."""

    def __init__(
        self,
        model: Any,
        *,
        policy: RelationSemanticsPolicy | None = None,
    ) -> None:
        self.model = model
        self.policy = policy or RelationSemanticsPolicy()

    def verify(
        self,
        draft: QuestionDraft,
        claims: list[Claim],
        evidence: list[KGEvidence],
        *,
        focus_entity_id: str | None = None,
        literature_evidence: list[LiteratureEvidence] | None = None,
    ) -> VerificationResult:
        return self._verify(
            draft,
            evidence,
            literature_evidence or [],
            focus_entity_id=focus_entity_id,
        )

    def verify_with_blueprint(
        self,
        draft: QuestionDraft,
        blueprint: QuestionBlueprint,
        claims: list[Claim],
        evidence: list[KGEvidence],
        literature_evidence: list[LiteratureEvidence] | None = None,
    ) -> VerificationResult:
        return self._verify(
            draft,
            evidence,
            literature_evidence or [],
            focus_entity_id=blueprint.focus_entity_id,
            blueprint=blueprint,
        )

    def _verify(
        self,
        draft: QuestionDraft,
        evidence: list[KGEvidence],
        literature: list[LiteratureEvidence],
        *,
        focus_entity_id: str | None,
        blueprint: QuestionBlueprint | None = None,
    ) -> VerificationResult:
        known_kg = {item.evidence_id for item in evidence}
        known_lit = {item.evidence_id for item in literature}
        unknown_kg = sorted(set(draft.evidence_ids) - known_kg)
        unknown_lit = sorted(
            set(draft.literature_evidence_ids) - known_lit
        )
        if unknown_kg or unknown_lit:
            return VerificationResult(
                draft_id=draft.draft_id,
                verdict="rejected",
                reason_codes=["unknown_evidence_reference"],
                message=(
                    f"unknown KG evidence: {unknown_kg}; "
                    f"unknown literature evidence: {unknown_lit}"
                ),
                blocking=True,
            )
        unsupported_literature = [
            item.evidence_id
            for item in literature
            if item.evidence_id in draft.literature_evidence_ids
            and item.location.get("support_verdict") != "supports"
        ]
        if unsupported_literature:
            return VerificationResult(
                draft_id=draft.draft_id,
                verdict="rejected",
                reason_codes=["literature_does_not_support"],
                message=(
                    "literature evidence used by the question did not pass "
                    f"independent support assessment: {unsupported_literature}"
                ),
                blocking=True,
            )
        allowed_answer_ids: set[str] = set()
        for item in evidence:
            allowed_answer_ids.update(
                value
                for value in (
                    item.head.canonical_id,
                    item.tail.canonical_id,
                )
                if value
            )
        request = StructuredGenerationRequest(
            invocation_id=f"verifier.{draft.generation_episode_id}.{draft.draft_id}",
            role="answer_verifier",
            system_prompt_version="verifier-v1",
            payload={
                "draft_id": draft.draft_id,
                "question_text": draft.question_text,
                "question_type": draft.question_type,
                "question_intent": draft.question_intent,
                "semantic_skeleton": draft.semantic_skeleton,
                "language": draft.language,
                "seed_entity_id": focus_entity_id,
                "allowed_answer_ids": sorted(allowed_answer_ids),
                "required_kg_evidence_ids": list(draft.evidence_ids),
                "kg_evidence": [
                    item.model_dump(mode="json")
                    for item in evidence
                ],
                "literature_evidence": [
                    {
                        "evidence_id": item.evidence_id,
                        "text": item.evidence_text,
                        "location": dict(item.location),
                    }
                    for item in literature
                    if item.evidence_id in draft.literature_evidence_ids
                ],
                "constraints": (
                    list(blueprint.leakage_constraints) if blueprint else []
                ),
            },
            idempotency_key=(
                f"{draft.generation_episode_id}:answer_verifier:"
                f"{draft.draft_id}:1"
            ),
        )
        response = self.model.generate_structured(
            request, IndependentVerification
        )
        assessment = IndependentVerification.model_validate(response.parsed)
        candidate_ids = {
            str(item["canonical_id"])
            for item in draft.candidate_answer.values
            if item.get("canonical_id")
        }
        reasons = list(assessment.reason_codes)
        blocking = False
        if not assessment.recomputed_answer_ids:
            reasons.append("answer_not_recomputed")
            blocking = True
        if assessment.alternative_answer_ids:
            reasons.append("answer_ambiguity")
            blocking = True
        if set(assessment.recomputed_answer_ids) - candidate_ids:
            reasons.append("answer_recomputation_mismatch")
            blocking = True
        if not assessment.evidence_supported:
            reasons.append("evidence_not_sufficient")
            blocking = True
        if assessment.answer_leaked:
            reasons.append("answer_leakage")
            blocking = True
        if blueprint is not None and candidate_ids & {blueprint.focus_entity_id}:
            reasons.append("answer_is_seed")
            blocking = True
        if blueprint is not None and blueprint.answer_entity_id not in candidate_ids:
            reasons.append("answer_endpoint_mismatch")
            blocking = True
        for evidence_id in draft.evidence_ids:
            edge = next(
                item for item in evidence if item.evidence_id == evidence_id
            )
            code, message = check_relation_semantics_safety(
                draft.question_text, edge.relation, self.policy
            )
            if code:
                reasons.append(code)
                blocking = True
        return VerificationResult(
            draft_id=draft.draft_id,
            verdict="rejected" if blocking else "accepted",
            reason_codes=sorted(set(reasons)),
            message=(
                "independent verifier rejected the candidate"
                if blocking
                else "independent verifier recomputed the answer from frozen evidence"
            ),
            blocking=blocking,
        )
