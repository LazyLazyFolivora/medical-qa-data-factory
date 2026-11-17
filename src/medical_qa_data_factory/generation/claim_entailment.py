"""Narrow source/claim checks with question semantics, isolated from acceptance decisions."""
from typing import Literal

from pydantic import Field

from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from .subagent_contracts import RoleContractViolation
from .verification_evidence_view import generate_with_evidence
from .source_question_inputs import SELECTED_EVIDENCE_CHAR_BUDGET


class ClaimEntailment(ContractModel):
    id: int = Field(ge=0)
    support: Literal["entailed", "not_entailed", "unresolved"]
    reason: str = Field(min_length=1, max_length=600)


class ClaimEntailments(ContractModel):
    checks: list[ClaimEntailment] = Field(min_length=1, max_length=4)


def audit_claim_pairs(model, pairs, anchors, *, operation_id):
    """Use the actual cited anchors, never the preceding reviewer's source paraphrase."""
    judgments, responses = [], []
    for offset in range(0, len(pairs), 4):
        batch = pairs[offset:offset + 4]
        ids = {aid for pair in batch for aid in pair["source_anchor_ids"]}
        request = StructuredGenerationRequest(
            role="objective_claim_entailment", system_prompt_version="claim-entailment-v5",
            invocation_id=f"{operation_id}.{offset}", idempotency_key=f"{operation_id}|{offset}",
            payload={"pairs": batch, "evidence_anchors": [anchor for anchor in anchors
                                                            if anchor["anchor_id"] in ids]})
        # Share each cited source once. Existing read-only views keep large records
        # behind exact, paged references instead of copying or truncating them.
        response = generate_with_evidence(model, request, ClaimEntailments, "evidence_anchors",
                                          verbatim_text_budget=SELECTED_EVIDENCE_CHAR_BUDGET)
        checked = ClaimEntailments.model_validate(response.parsed)
        expected = {pair["id"] for pair in batch}
        if len(checked.checks) != len(expected) or {item.id for item in checked.checks} != expected:
            raise RoleContractViolation("Claim entailment must check each supplied pair exactly once")
        judgments.extend(checked.checks)
        responses.append(response)
    return judgments, responses
