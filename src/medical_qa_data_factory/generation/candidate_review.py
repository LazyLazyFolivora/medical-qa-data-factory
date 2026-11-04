"""LLM usability review before candidates enter verification or evidence gathering."""
from typing import Literal
from pydantic import Field
from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from .subagent_contracts import RoleContractViolation


class CandidateReview(ContractModel):
    hypothesis_id: str
    disposition: Literal["KEEP", "REWRITE", "NEEDS_EVIDENCE", "REJECT"]
    rationale: str = Field(min_length=1)
    revised_question: str | None = None
    revised_scope: str | None = None
    evidence_need: str | None = None


class CandidateReviewBatch(ContractModel):
    reviews: list[CandidateReview]


def review_candidates(model, hypotheses, claims, anchors, *, operation_id):
    """Allow one rewrite followed by an independent recheck; fail closed on missing decisions."""
    current = {h.hypothesis_id: h for h in hypotheses}
    pending = list(hypotheses)
    final = {}
    history = []
    for round_number in range(2):
        if not pending:
            break
        claim_ids = {key for h in pending for key in h.candidate_claim_ids}
        selected_claims = [c for c in claims if c.claim_id in claim_ids]
        anchor_ids = {key for c in selected_claims for key in c.supporting_anchor_ids}
        response = model.generate_structured(StructuredGenerationRequest(
            invocation_id=f"{operation_id}.usability.{round_number}",
            role="objective_candidate_review", system_prompt_version="candidate-review-v1",
            idempotency_key=f"{operation_id}|usability|{round_number}",
            payload={"stage": "initial" if round_number == 0 else "recheck",
                     "hypotheses": [h.model_dump(mode="json") for h in pending],
                     "claims": [c.model_dump(mode="json") for c in selected_claims],
                     "evidence_excerpts": [{"anchor_id": a.anchor_id, "quoted_text": a.quoted_text}
                                           for a in anchors if a.anchor_id in anchor_ids]},
        ), CandidateReviewBatch)
        reviews = CandidateReviewBatch.model_validate(response.parsed).reviews
        if len(reviews) != len(pending) or {r.hypothesis_id for r in reviews} != {h.hypothesis_id for h in pending}:
            raise RoleContractViolation("candidate review must cover each input exactly once")
        pending = []
        for review in reviews:
            history.append(review.model_dump(mode="json"))
            key = review.hypothesis_id
            if review.disposition == "NEEDS_EVIDENCE" and not (review.evidence_need or "").strip():
                raise RoleContractViolation("evidence review must identify a concrete gap")
            if review.disposition == "REWRITE":
                if round_number == 1:
                    review = review.model_copy(update={"disposition": "REJECT", "rationale": "Rewrite did not produce a usable question: " + review.rationale})
                else:
                    if not (review.revised_question or "").strip() or not (review.revised_scope or "").strip():
                        raise RoleContractViolation("rewrite requires question and public scope")
                    current[key] = current[key].model_copy(update={
                        "question_intent": review.revised_question, "proposed_public_scope": review.revised_scope})
                    pending.append(current[key])
                    continue
            final[key] = review
    return list(current.values()), list(final.values()), history
