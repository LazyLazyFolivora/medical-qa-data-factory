"""The answer-blind verifier.

Step 7 of the real-generation-agent plan. The verifier checks a written
question against its structure and the evidence, without being shown any
candidate answer and without being shown the writer's rationale.

That separation is the point. A verifier that sees the answer tends to
confirm it; a verifier that sees the writer's reasoning tends to inherit it.
The verifier's input is the question, the structure it was built from, the
evidence it may rely on, and a verification request that names what to check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, Sequence

from pydantic import Field

from ..domain.common import ContractModel, UtcDateTime
from .logical_query import LogicalQuery, QueryAnswer


class BlindVerdict(str, Enum):
    """What the verifier decided about a written question."""

    ACCEPTED = "accepted"
    NEEDS_REVISION = "needs_revision"
    HARD_REJECTED = "hard_rejected"


class BlindCheckCode(str, Enum):
    """The specific problems a verification can find."""

    ANSWER_LEAKED = "answer_leaked"
    INTERMEDIATE_ENTITY_LEAKED = "intermediate_entity_leaked"
    QUESTION_NOT_ENTAILED = "question_not_entailed"
    AMBIGUOUS_ANSWER = "ambiguous_answer"
    WRONG_CARDINALITY = "wrong_cardinality"
    RELATION_SEMANTICS_VIOLATION = "relation_semantics_violation"
    EVIDENCE_MISSING = "evidence_missing"
    TEMPLATE_LIKE = "template_like"


class VerificationRequest(ContractModel):
    """What the verifier is asked to check.

    The request never carries the candidate answer. It carries the question,
    the query it was built from, and the answer set the query resolved to, so
    the verifier can check the question against the *structure* rather than
    against a proposed answer.
    """

    request_id: str = Field(min_length=1)
    question_text: str = Field(min_length=1)
    query: LogicalQuery
    #: The authoritative answer set the query resolved to.
    answer_ids: list[str] = Field(default_factory=list)
    #: The entities that must not appear in the question text.
    forbidden_entity_ids: list[str] = Field(default_factory=list)
    #: The answer names that must not appear in the question text.
    forbidden_names: list[str] = Field(default_factory=list)
    #: The intermediate entity names that must not appear either. They are
    #: tracked separately, because leaking an intermediate is a different
    #: fault from leaking the answer.
    forbidden_intermediate_names: list[str] = Field(default_factory=list)
    #: The relations the question is about, for the semantics check.
    relations: list[str] = Field(default_factory=list)
    #: Whether the question is allowed to name the answer at all.
    answer_must_be_hidden: bool = True


class BlindVerificationResult(ContractModel):
    """The verifier's decision."""

    request_id: str = Field(min_length=1)
    verdict: BlindVerdict
    reason_codes: list[str] = Field(default_factory=list)
    message: str = ""
    #: Whether the failure blocks the question entirely.
    blocking: bool = False
    #: The checks that passed, for the audit trail.
    passed_checks: list[str] = Field(default_factory=list)
    created_at: UtcDateTime


class BlindVerifierPort(Protocol):
    """The model side of the verifier."""

    def verify(
        self, request: VerificationRequest
    ) -> BlindVerificationResult:  # pragma: no cover - interface
        raise NotImplementedError


class DeterministicBlindVerifier:
    """A verifier that checks the question against the structure.

    It is not a model. It compares the question text with the entities the
    query says must be hidden, and checks the question against the answer
    set the query produced.
    """

    version = "blind-verifier-v1"

    def verify(
        self, request: VerificationRequest
    ) -> BlindVerificationResult:
        from ..domain.common import utc_now

        text = request.question_text
        lowered = text.lower()
        reasons: list[str] = []
        passed: list[str] = []

        # ---- leakage ---------------------------------------------------
        leaked_answers = [
            name
            for name in request.forbidden_names
            if name and name.lower() in lowered
        ]
        if request.answer_must_be_hidden and leaked_answers:
            reasons.append(BlindCheckCode.ANSWER_LEAKED.value)
        else:
            passed.append("answer_not_leaked")

        leaked_intermediates = [
            name
            for name in request.forbidden_intermediate_names
            if name and name.lower() in lowered
        ]
        if leaked_intermediates:
            reasons.append(BlindCheckCode.INTERMEDIATE_ENTITY_LEAKED.value)
        else:
            passed.append("intermediates_not_leaked")

        # ---- the question must ask for the right shape ------------------
        if not text.strip():
            reasons.append(BlindCheckCode.QUESTION_NOT_ENTAILED.value)
        else:
            passed.append("question_is_present")

        if request.query.expected_cardinality == "one" and len(
            request.answer_ids
        ) > 1:
            reasons.append(BlindCheckCode.AMBIGUOUS_ANSWER.value)
        else:
            passed.append("cardinality_is_consistent")

        blocking = bool(
            reasons
            and any(
                item
                in (
                    BlindCheckCode.ANSWER_LEAKED.value,
                    BlindCheckCode.QUESTION_NOT_ENTAILED.value,
                    BlindCheckCode.AMBIGUOUS_ANSWER.value,
                )
                for item in reasons
            )
        )
        if blocking:
            verdict = BlindVerdict.HARD_REJECTED
        elif reasons:
            verdict = BlindVerdict.NEEDS_REVISION
        else:
            verdict = BlindVerdict.ACCEPTED
        return BlindVerificationResult(
            request_id=request.request_id,
            verdict=verdict,
            reason_codes=sorted(set(reasons)),
            message="; ".join(sorted(set(reasons))) or "the question is consistent",
            blocking=blocking,
            passed_checks=sorted(set(passed)),
            created_at=utc_now(),
        )


def build_verification_request(
    *,
    request_id: str,
    question_text: str,
    query: LogicalQuery,
    answer: QueryAnswer,
    answer_names: Sequence[str],
    intermediate_names: Sequence[str] = (),
) -> VerificationRequest:
    """Build the request. The candidate answer is never included."""
    return VerificationRequest(
        request_id=request_id,
        question_text=question_text,
        query=query,
        answer_ids=list(answer.answer_ids),
        forbidden_entity_ids=list(answer.answer_ids),
        forbidden_names=sorted({name for name in answer_names if name}),
        forbidden_intermediate_names=sorted(
            {name for name in intermediate_names if name}
        ),
        relations=[step.relation for step in query.steps],
    )