"""Candidate question design from already-persisted candidate claims."""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
import re
from typing import Any, Literal, Sequence

from pydantic import Field

from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from ..llm.errors import ModelError, ModelErrorCode
from .objective_question_contracts import (
    CandidateClaim,
    DomainOutcome,
    EvidenceAnchor,
    ExecutionStatus,
    QuestionHypothesis,
    SubagentResult,
    SubagentTask,
    VerificationVerdict,
    canonical_digest,
)
from .subagent_contracts import (
    RoleContractViolation,
    SubagentRole,
    default_objective_role_registry,
    validate_objective_subagent_task,
)


class FactSelectionNote(ContractModel):
    candidate_claim_ids: list[str]
    observation: str
    additional_fact: str | None = None
    information_gain: str
    composition: Literal["single", "connected", "insufficient"]


class QuestionHypothesisBatch(ContractModel):
    fact_selections: list[FactSelectionNote] = Field(default_factory=list)
    hypotheses: list[QuestionHypothesis] = Field(default_factory=list)
    insufficiency_reason: str | None = None


@dataclass(frozen=True)
class QuestionDesignOutcome:
    result: SubagentResult
    hypotheses: list[QuestionHypothesis]
    fact_selections: list[FactSelectionNote] = field(default_factory=list)


def _answer_signature(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                re.sub(r"\s+", " ", str(value)).strip().casefold()
                for value in values
                if str(value).strip()
            }
        )
    )


def _claim_overlap(
    left: set[str],
    right: set[str],
) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _hypothesis_rank(
    hypothesis: QuestionHypothesis,
    *,
    preferred_claim_ids: set[str],
    claim_by_id: dict[str, CandidateClaim],
) -> tuple[int, int]:
    preferred_hits = int(bool(
        preferred_claim_ids.intersection(hypothesis.candidate_claim_ids)
    ))
    information_hits = int(any(
        _claim_is_information_bearing(claim_by_id[claim_id])
        for claim_id in hypothesis.candidate_claim_ids
        if claim_id in claim_by_id
    ))
    return (
        -preferred_hits,
        -information_hits,
    )


def _claim_is_information_bearing(claim: CandidateClaim) -> bool:
    text = f"{claim.predicate} {claim.object_or_value}".casefold()
    identity_fragments = (
        "_id",
        "symbol",
        "approved_gene_name",
        "gene_name",
        "located_on_chromosome",
        "cytogenetic_location",
        "coordinates",
        "strand",
    )
    if any(fragment in text for fragment in identity_fragments):
        return False
    return any(
        term in text
        for term in (
            "disease",
            "associat",
            "pathogenic",
            "variant",
            "function",
            "mechanism",
            "expression",
            "activation",
            "inhibit",
            "risk",
            "syndrome",
            "toxicity",
            "response",
            "survival",
            "phenotype",
        )
    )


def _generate_design_batch(model, request, response_model):
    """Split oversized fact batches while preserving whole quotes and references."""
    try:
        return model.generate_structured(request, response_model)
    except ModelError as exc:
        detail = f"{exc.message} {exc.provider_response or ''}".lower()
        if exc.code is not ModelErrorCode.BAD_REQUEST or not any(marker in detail for marker in (
            "context budget exceeded", "maximum context length", "context_length_exceeded",
            "prompt contains at least",
        )):
            raise
        claims = request.payload.get("candidate_claims", [])
        if len(claims) < 2:
            raise  # A single indivisible fact must not be silently clipped.
    # Prefer a boundary between source groups to retain connected facts together.
    midpoint = len(claims) // 2
    boundaries = [i for i in range(1, len(claims))
                  if set(claims[i - 1].get("supporting_anchor_ids", []))
                  .isdisjoint(claims[i].get("supporting_anchor_ids", []))]
    if boundaries:
        midpoint = min(boundaries, key=lambda i: abs(i - midpoint))
    responses = []
    for index, subset in enumerate((claims[:midpoint], claims[midpoint:])):
        payload = deepcopy(request.payload)
        ids = {c["claim_id"] for c in subset}
        anchor_ids = {a for c in subset for a in c.get("supporting_anchor_ids", [])}
        payload["candidate_claims"] = subset
        payload["claim_evidence_cards"] = [c for c in payload.get("claim_evidence_cards", []) if c["claim_id"] in ids]
        for key in ("evidence_excerpts", "available_anchor_locations"):
            payload[key] = [a for a in payload.get(key, []) if a["anchor_id"] in anchor_ids]
        payload["claim_groups"] = [
            {**group, "claim_ids": [c for c in group.get("claim_ids", []) if c in ids],
             "anchor_ids": [a for a in group.get("anchor_ids", []) if a in anchor_ids]}
            for group in payload.get("claim_groups", []) if ids.intersection(group.get("claim_ids", []))]
        if "required_preferred_claim_ids" in payload:
            payload["required_preferred_claim_ids"] = [c for c in payload["required_preferred_claim_ids"] if c in ids]
        child = request.model_copy(update={
            "payload": payload, "invocation_id": f"{request.invocation_id}.batch{index}",
            "idempotency_key": f"{request.idempotency_key}|batch:{canonical_digest(sorted(ids))[:16]}",
        })
        responses.append(_generate_design_batch(model, child, response_model))
    batches = [QuestionHypothesisBatch.model_validate(r.parsed) for r in responses]
    combined = QuestionHypothesisBatch(
        hypotheses=[h for b in batches for h in b.hypotheses],
        fact_selections=[n for b in batches for n in b.fact_selections],
        insufficiency_reason="; ".join(b.insufficiency_reason for b in batches if b.insufficiency_reason) or None,
    )
    usage = {key: sum(r.usage.get(key, 0) or 0 for r in responses)
             for key in ("input_tokens", "output_tokens", "attempts")}
    usage["design_batches"] = sum(r.usage.get("design_batches", 1) for r in responses)
    return responses[0].model_copy(update={"invocation_id": request.invocation_id,
                                           "parsed": combined.model_dump(mode="json"), "usage": usage})


class ObjectiveQuestionDesignSubagent:
    role = SubagentRole.QUESTION_DESIGN

    def __init__(self, model: Any) -> None:
        self.model = model
        self.capability = default_objective_role_registry()[self.role]

    def run(
        self,
        task: SubagentTask,
        *,
        claims: Sequence[CandidateClaim],
        anchors: Sequence[EvidenceAnchor],
    ) -> QuestionDesignOutcome:
        validate_objective_subagent_task(task, self.capability)
        claim_ids = [claim.claim_id for claim in claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise RoleContractViolation("candidate claim IDs must be unique")
        anchor_ids = {anchor.anchor_id for anchor in anchors}
        if any(
            anchor_id not in anchor_ids
            for claim in claims
            for anchor_id in claim.supporting_anchor_ids
        ):
            raise RoleContractViolation(
                "candidate claim references an anchor hidden from question design"
            )
        payload = {
            "task": {
                "target_entity": task.goal["target_entity"],
                "allowed_fact_types": list(
                    task.goal.get("allowed_fact_types", [])
                ),
                "question_count_upper_bound": int(
                    task.goal.get("question_count_upper_bound", 1)
                ),
                "difficulty_targets": list(
                    task.goal.get("difficulty_targets", [])
                ),
                "style_targets": list(task.goal.get("style_targets", [])),
                "language": task.goal.get("language"),
                "planner_environment": dict(task.goal.get("planner_environment", {})),
            },
            "candidate_claims": [
                claim.model_dump(mode="json") for claim in claims
            ],
            "claim_evidence_cards": [
                {"claim_id": claim.claim_id, "source_anchor_ids": list(claim.supporting_anchor_ids)}
                for claim in claims
            ],
            "design_method": {
                "version": "fact-pair-v1-production-adaptation-1",
                "steps": [
                    "Read one complete observation with its actors, intervention, effect and conditions.",
                    "Add another supported fact only if it adds useful information for the same reader need.",
                    "Choose single, connected or insufficient; shared source or entity is not a reason to combine.",
                    "Write the minimum sufficient answer, then a natural standalone question in the requested language.",
                ],
                "question_intent_format": "Final publishable wording. Put every necessary qualifier in this text. "
                                          "Supply the expected answers; aliases and translations denote one answer.",
                "notes_boundary": "fact_selections explain design choices; they are not answer evidence.",
            },
            "claim_groups": [
                {key: value for key, value in group.items()
                 if key in {"group_id", "material_id", "claim_ids", "anchor_ids"}}
                for group in task.goal.get("claim_groups", [])
            ],
            # Each source appears once, even when many claims share a record.
            # Keep complete quotes: truncating a qualifier can change the fact.
            "evidence_excerpts": [
                {"anchor_id": anchor.anchor_id, "material_id": anchor.material_id,
                 "document_id": anchor.document_id, "section_or_field": anchor.section_or_field,
                 "quoted_text": anchor.quoted_text}
                for anchor in anchors
                if anchor.anchor_id in {key for claim in claims for key in claim.supporting_anchor_ids}
            ],
            "question_portfolio": dict(
                task.goal.get("question_portfolio", {})
            ),
            "available_anchor_locations": [
                {
                    "anchor_id": anchor.anchor_id,
                    "material_id": anchor.material_id,
                    "document_id": anchor.document_id,
                    "section_or_field": anchor.section_or_field,
                }
                for anchor in anchors
            ],
            "instruction": (
                "Treat proposed answers as candidates only. Do not change any "
                "candidate claim or claim that an answer is verified. "
                "Use evidence_excerpts only to choose precise public wording, "
                "scope and qualifiers; never add a fact absent from candidate_claims. "
                "Use claim_groups as navigation only. First record complete observations "
                "and information gain in fact_selections. Keep a single fact when "
                "a second adds no useful information. question_intent is displayed "
                "to the user as the candidate question: write a natural question, "
                "not '查明', '确定', '查询', '阐明' or 'Ask about' research instructions. "
                "Do not append name meanings, mechanisms or classification explanations "
                "unless they serve the same information need and have their own evidence. "
                "Preserve the classification concept: gene group means 基因群组, "
                "not 基因组 or protein family. Ask about one supported category, "
                "never 'family or group' as interchangeable alternatives. "
                "Prefer diverse, information-bearing claims. When supported "
                "observation or association claims exist, do not exhaust the "
                "requested count on identifier and genomic-location trivia. "
                "proposed_public_scope must describe the real-world subject, "
                "population, species, definition and comparison set needed to "
                "answer. It must never mention PrimeKG, CTD, PubMed, RAG, a "
                "database, a retrieved corpus, or an internal candidate set."
            ),
        }
        visible_claim_ids = set(claim_ids)
        claim_by_id = {claim.claim_id: claim for claim in claims}
        portfolio = dict(task.goal.get("question_portfolio", {}))
        attempted_hypothesis_ids = set(
            portfolio.get("attempted_hypothesis_ids", [])
        )
        previous = portfolio.get("existing_questions", [])
        rejected_wordings = {item["question_intent"].strip().casefold() for item in previous if item.get("redesign_feedback")}
        existing_answers = {
            _answer_signature(item.get("answer_values", []))
            for item in portfolio.get("existing_questions", [])
            if item.get("answer_values") and not item.get("redesign_feedback")
        }
        existing_claim_sets = [
            set(item.get("claim_ids", []))
            for item in portfolio.get("existing_questions", [])
            if item.get("claim_ids") and not item.get("redesign_feedback")
        ]
        preferred_claim_ids = set(
            portfolio.get("preferred_claim_ids", [])
        )
        accepted_hypotheses: list[QuestionHypothesis] = []
        answer_signatures: set[tuple[str, ...]] = set()
        accepted_claim_sets: list[set[str]] = []
        fact_selections: list[FactSelectionNote] = []

        def consume(batch: QuestionHypothesisBatch) -> bool:
            fact_selections.extend(batch.fact_selections)
            added_preferred = False
            for hypothesis in sorted(
                batch.hypotheses,
                key=lambda item: _hypothesis_rank(
                    item,
                    preferred_claim_ids=preferred_claim_ids,
                    claim_by_id=claim_by_id,
                ),
            ):
                if hypothesis.question_intent.strip().casefold() in rejected_wordings:
                    continue
                limit = int(task.goal.get("question_count_upper_bound", 1))
                medium_only = set(task.goal.get("difficulty_targets", [])) == {"medium"}
                supplementary = medium_only and hypothesis.proposed_difficulty == "easy"
                quota_indices = [i for i, item in enumerate(accepted_hypotheses)
                                if not medium_only or item.proposed_difficulty != "easy"]
                replaces_last = (
                    not supplementary and limit > 0 and len(quota_indices) >= limit
                    and bool(preferred_claim_ids.intersection(hypothesis.candidate_claim_ids))
                    and not any(preferred_claim_ids.intersection(item.candidate_claim_ids)
                                for item in accepted_hypotheses)
                )
                if not supplementary and len(quota_indices) >= limit and not replaces_last:
                    continue
                if (
                    hypothesis.candidate_claim_ids
                    and not visible_claim_ids.issuperset(
                        hypothesis.candidate_claim_ids
                    )
                ):
                    raise RoleContractViolation(
                        "hypothesis references unknown claims: "
                        f"{sorted(set(hypothesis.candidate_claim_ids) - visible_claim_ids)}"
                    )
                answer_signature = _answer_signature(
                    hypothesis.proposed_answer_values
                )
                claim_set = set(hypothesis.candidate_claim_ids)
                if answer_signature and (
                    answer_signature in existing_answers
                    or answer_signature in answer_signatures
                ):
                    continue
                if any(
                    _claim_overlap(claim_set, prior) >= 0.8
                    for prior in [*existing_claim_sets, *accepted_claim_sets]
                ):
                    continue
                supporting_anchor_ids = {
                    anchor_id
                    for claim_id in hypothesis.candidate_claim_ids
                    for anchor_id in claim_by_id[
                        claim_id
                    ].supporting_anchor_ids
                }
                hypothesis = hypothesis.model_copy(
                    update={
                        "discovery_anchor_ids": sorted(
                            supporting_anchor_ids
                        )
                    }
                )
                semantic_payload = hypothesis.model_dump(
                    mode="json",
                    exclude={"hypothesis_id"},
                )
                accepted = hypothesis.model_copy(
                    update={
                        "hypothesis_id": (
                            "hypothesis."
                            + canonical_digest(semantic_payload)[:24]
                        )
                    }
                )
                if accepted.hypothesis_id in attempted_hypothesis_ids:
                    continue
                if replaces_last:
                    removed = accepted_hypotheses.pop(quota_indices[-1])
                    accepted_claim_sets.pop(quota_indices[-1])
                    answer_signatures.discard(_answer_signature(removed.proposed_answer_values))
                accepted_hypotheses.append(accepted)
                accepted_claim_sets.append(claim_set)
                if answer_signature:
                    answer_signatures.add(answer_signature)
                added_preferred = added_preferred or bool(
                    preferred_claim_ids.intersection(claim_set)
                )
            return added_preferred

        response = _generate_design_batch(self.model,
            StructuredGenerationRequest(
                invocation_id=task.initial_attempt_id,
                role="objective_question_design",
                system_prompt_version=self.capability.prompt_version,
                payload=payload,
                idempotency_key=task.operation_key,
            ),
            QuestionHypothesisBatch,
        )
        initial_batch = QuestionHypothesisBatch.model_validate(response.parsed)
        consume(initial_batch)
        if not initial_batch.hypotheses:
            return QuestionDesignOutcome(
                result=SubagentResult(
                    subtask_id=task.subtask_id,
                    execution_status=ExecutionStatus.SUCCEEDED,
                    domain_outcome=DomainOutcome.INSUFFICIENT,
                    verdict=VerificationVerdict.NOT_EMITTED,
                    output_artifact_ids=[], evidence_need_ids=[],
                    usage=dict(response.usage), error_category=None,
                    stop_reason=initial_batch.insufficiency_reason or "no_supported_hypothesis",
                ),
                hypotheses=[],
                fact_selections=fact_selections,
            )

        has_preferred = any(
            preferred_claim_ids.intersection(
                hypothesis.candidate_claim_ids
            )
            for hypothesis in accepted_hypotheses
        )
        if preferred_claim_ids and not has_preferred:
            repair_payload = {
                **payload,
                "required_preferred_claim_ids": sorted(
                    preferred_claim_ids
                ),
                "instruction": (
                    payload["instruction"]
                    + " Consider required_preferred_claim_ids for a useful supported "
                    "question. Return no hypothesis and explain insufficiency if "
                    "these claims cannot support one; do not force a compound question."
                ),
            }
            try:
                repair_response = _generate_design_batch(self.model,
                    StructuredGenerationRequest(
                        invocation_id=(
                            f"{task.initial_attempt_id}.portfolio"
                        ),
                        role="objective_question_design",
                        system_prompt_version=self.capability.prompt_version,
                        payload=repair_payload,
                        idempotency_key=(
                            f"{task.operation_key}|portfolio"
                        ),
                    ),
                    QuestionHypothesisBatch,
                )
                consume(
                    QuestionHypothesisBatch.model_validate(
                        repair_response.parsed
                    )
                )
            except Exception:
                pass

        if not accepted_hypotheses:
            raise RoleContractViolation(
                "question design produced no non-duplicate hypothesis"
            )
        result = SubagentResult(
            subtask_id=task.subtask_id,
            execution_status=ExecutionStatus.SUCCEEDED,
            domain_outcome=DomainOutcome.NOT_APPLICABLE,
            verdict=VerificationVerdict.NOT_EMITTED,
            output_artifact_ids=[
                hypothesis.hypothesis_id
                for hypothesis in accepted_hypotheses
            ],
            evidence_need_ids=[],
            usage=dict(response.usage),
            error_category=None,
            stop_reason="hypotheses_proposed",
        )
        return QuestionDesignOutcome(
            result=result,
            hypotheses=accepted_hypotheses,
            fact_selections=fact_selections,
        )
