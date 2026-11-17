"""Candidate question design from already-persisted candidate claims."""
from __future__ import annotations
from .question_strategies import question_strategy

from dataclasses import dataclass, field
from copy import deepcopy
import re
from typing import Any, Literal, Sequence

from pydantic import Field, create_model, model_validator

from ..domain.common import ContractModel
from ..domain.source_spans import sentence_spans
from ..llm.contracts import StructuredGenerationRequest
from ..llm.errors import ModelError, ModelErrorCode
from .objective_question_contracts import (
    CandidateClaim,
    DomainOutcome,
    EvidenceAnchor,
    ExecutionStatus,
    QuestionHypothesis,
    SuggestedRetrieval,
    SubagentResult,
    SubagentTask,
    VerificationVerdict,
    canonical_digest,
)
from .subagent_contracts import (
    RoleContractViolation,
    SubagentRole,
    build_objective_subagent_task,
    default_objective_role_registry,
    validate_objective_subagent_task,
)


class FactSelectionNote(ContractModel):
    candidate_claim_ids: list[str]
    observation: str
    additional_fact: str | None = None
    information_gain: str
    composition: Literal["single", "connected", "insufficient"]


_RETRIEVAL_SOURCES = {"pubmed", "europe pmc", "primekg", "hgnc", "ncbi gene", "ensembl", "ctd"}


def public_retrieval_suggestion(question: str, proposal: SuggestedRetrieval | None) -> SuggestedRetrieval:
    """Build the search seed from what a planner can read in the question."""
    query = question
    sources = []
    if proposal is not None:
        query_terms = set(re.findall(r"\w+", proposal.query.casefold()))
        question_terms = set(re.findall(r"\w+", question.casefold()))
        if query_terms and query_terms <= question_terms:
            query = proposal.query
        sources = [
            source for source in proposal.sources
            if source.strip().casefold().replace("_", " ") in _RETRIEVAL_SOURCES
        ]
    return SuggestedRetrieval(
        query=query,
        sources=sources or ["PubMed"],
        expected_evidence=question,
        stop_condition="Find evidence for every part of the question in its stated scope.",
    )


class QuestionHypothesisBatch(ContractModel):
    fact_selections: list[FactSelectionNote] = Field(default_factory=list)
    hypotheses: list[QuestionHypothesis] = Field(default_factory=list)
    insufficiency_reason: str | None = Field(
        default=None,
        description="When hypotheses is empty, explain the concrete missing evidence or question defect; an empty response is not an assessment.",
    )

    @model_validator(mode="after")
    def require_candidate_or_explanation(self):
        if not self.hypotheses and not (self.insufficiency_reason or "").strip():
            raise ValueError(
                "Return a supported hypothesis, or empty hypotheses with a concrete insufficiency_reason. "
                "An empty object or unexplained empty list is not an evidence assessment."
            )
        return self


class SourceQuestionHypothesis(QuestionHypothesis):
    evidence_basis: Literal["source_passages"] = "source_passages"
    candidate_claim_ids: list[str] = Field(default_factory=list, max_length=0)


class SourceFactSelectionNote(FactSelectionNote):
    candidate_claim_ids: list[str] = Field(default_factory=list, max_length=0)


class SourceQuestionHypothesisBatch(QuestionHypothesisBatch):
    fact_selections: list[SourceFactSelectionNote] = Field(default_factory=list)
    hypotheses: list[SourceQuestionHypothesis] = Field(default_factory=list)


class AnswerSourceObservation(ContractModel):
    span_id: int = Field(ge=1)
    subject_or_intervention: str
    conditions: str
    result: str
    relationship: Literal["description", "observed_intervention_effect", "association", "demonstrated_mechanism",
                          "proposed_explanation"]
    relevance: Literal["direct", "background", "different_subject_or_intervention"]


class AnswerSourceReading(ContractModel):
    requested_result: str = Field(min_length=1, max_length=500)
    quantitative_result_needed: bool
    direct_observations_cover_question: bool = False
    observations: list[AnswerSourceObservation] = Field(max_length=10)
    missing_relationships: list[str] = Field(max_length=5)
    unestablished_links: list[str] = Field(max_length=5)


def fixed_question_batch(question: str, *, direct_sources: bool, source_anchor_ids: tuple[str, ...] = ()):
    """Answer a supplied question without asking the model to redesign it."""
    fields = {"question_intent": (Literal[question], Field(...))}
    if direct_sources and source_anchor_ids:
        fields["discovery_anchor_ids"] = (list[Literal[source_anchor_ids]], Field(min_length=1))
    hypothesis = create_model(
        "FixedQuestionHypothesis",
        __base__=SourceQuestionHypothesis if direct_sources else QuestionHypothesis,
        **fields,
    )
    return create_model(
        "FixedQuestionHypothesisBatch",
        __base__=SourceQuestionHypothesisBatch if direct_sources else QuestionHypothesisBatch,
        hypotheses=(list[hypothesis], Field(default_factory=list, max_length=1)),
    )


@dataclass(frozen=True)
class QuestionDesignOutcome:
    result: SubagentResult
    hypotheses: list[QuestionHypothesis]
    fact_selections: list[FactSelectionNote] = field(default_factory=list)
    answer_source_reading: dict | None = None


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


def _read_answer_sources(model, task, question, excerpts):
    spans = {}
    sources = []
    for excerpt in excerpts:
        text = excerpt["quoted_text"]
        source = {key: value for key, value in excerpt.items() if key != "quoted_text"}
        source["spans"] = []
        for start, end in sentence_spans(text):
            span_id = len(spans) + 1
            span = {"span_id": span_id, "quote": text[start:end]}
            source["spans"].append(span)
            spans[span_id] = {**span, "anchor_id": excerpt["anchor_id"],
                              "start_in_anchor": start, "end_in_anchor": end}
        sources.append(source)
    request = StructuredGenerationRequest(
        role="objective_answer_source_reading", system_prompt_version="answer-source-reading-v7",
        invocation_id=f"{task.initial_attempt_id}.sources", idempotency_key=f"{task.operation_key}|answer-spans-v7",
        payload={"question": question, "sources": sources},
    )
    try:
        response = model.generate_structured(request, AnswerSourceReading)
    except ModelError as exc:
        return {"status": "failed", "error_code": exc.code.value}, {"attempts": exc.attempts}
    reading = AnswerSourceReading.model_validate(response.parsed)
    observations = [{**item.model_dump(mode="json"), **spans[item.span_id]}
                    for item in reading.observations if item.span_id in spans]
    return {
        "status": "read",
        "requested_result": reading.requested_result,
        "quantitative_result_needed": reading.quantitative_result_needed,
        "direct_observations_cover_question": reading.direct_observations_cover_question,
        "observations": observations,
        "missing_relationships": reading.missing_relationships,
        "unestablished_links": reading.unestablished_links,
        "unknown_span_ids": len(reading.observations) - len(observations),
    }, response.usage


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

    def draft_reviewed_alternative(self, task, record, anchors):
        """Draft the separately reviewed question as its own fixed public question."""
        from .research_tools import discovery_question_context

        context = discovery_question_context(record["artifact_id"], record)
        selected = [anchor for anchor in anchors if anchor.material_id in record["source_ids"]]
        followup = build_objective_subagent_task(
            task_id=f"{task.subtask_id}.alternative", role=self.role, capability=self.capability,
            goal={**task.goal, "research_context": context, "revision_of": None, "revision_context": None,
                  "question_count_upper_bound": 1, "design_instruction": "",
                  "evidence_sufficiency_reason": "Answer the independently reviewed alternative from its original source."},
            input_artifact_ids=[anchor.anchor_id for anchor in selected],
            budget={"max_model_calls": 2, "max_provider_calls": 0},
        )
        return self.run(followup, claims=[], anchors=selected), context

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
        if task.goal.get("llm_owned_design"):
            portfolio = dict(task.goal.get("question_portfolio", {}))
            portfolio["preferred_claim_ids"] = []
            portfolio["requirement"] = "Use the controller-selected facts and instruction. Judge usefulness, duplication and difficulty yourself; return no candidate when unsupported."
            task = task.model_copy(update={"goal": {**task.goal, "question_portfolio": portfolio}})
        strategy = question_strategy(task.goal)
        payload = {
            "design_instruction": task.goal.get("design_instruction", ""),
            "evidence_sufficiency_reason": task.goal.get("evidence_sufficiency_reason", ""),
            "readiness_boundary": "The controller's evidence_sufficiency_reason explains its intended question and why it chose to draft now. "
                "Check that judgment against the selected original passages. It is not evidence or a verification result. "
                "Do not invent missing support or force a multi-source question; report insufficiency when the proposed scope is unsupported.",
            "research_context": ({**{k: v for k, v in task.goal["research_context"].items()
                                     if k != "source_passages"},
                                  "passage_text_location": "evidence_excerpts contains the selected original passages"}
                                 if task.goal.get("research_context") else None),
            "revision_of": task.goal.get("revision_of"),
            "revision_context": task.goal.get("revision_context"),
            "task": {
                "target_entity": task.goal["target_entity"],
                "generation_strategy": strategy.name,
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
                **strategy.payload(),
                "steps": list(strategy.design_steps),
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
                if task.goal.get("direct_sources") or anchor.anchor_id in {key for claim in claims for key in claim.supporting_anchor_ids}
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
                "research_context identifies the selected research question, one question_focus, answer_boundaries and verbatim source passages. "
                "Preserve that information need and the conditions that change its answer; do not turn it into a general mechanism overview. "
                "Focus and boundaries are model guidance, NOT evidence. Draft from original evidence_excerpts/source_passages; "
                "recheck candidate claims against the originals and decline or narrow unsupported interpretations. "
                "Default to domain knowledge questions with independently understandable premises. Study-specific queries require an explicit user/task request; do not invent that request from retrieved evidence. "
                "Report the relevant observations, then the bounded conclusion. Do not turn no significant effect into zero effect, "
                "or an observed endpoint change into a proven cellular mechanism, exclusive cause, or measured contribution proportion. "
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
        if task.goal.get("direct_sources"):
            payload["input_mode"] = "source_passages"
            payload["instruction"] = (
                "Draft directly from original evidence_excerpts. Return evidence_basis=source_passages and "
                "candidate_claim_ids=[] for every hypothesis; discovery_anchor_ids must reference the supplied excerpts. "
                "No fact-extraction stage is required. Treat all answers as unverified candidates. "
                "Use natural, self-contained questions; distinguish measured observations from suggested mechanisms. "
                "Keep experimental conditions and scope. Do not infer no effect from an unreported effect. "
                "Multiple sources must each contribute necessary information, not merely share an entity. "
                "Return few useful candidates or an insufficiency_reason; never invent supporting evidence. "
                "Default to domain questions, not questions about a retrieved paper's internal results. "
                "If the selected focus merely requests study metrics without an explicit user/task request for that study, "
                "return insufficiency and a better domain information target; do not rescue it by adding a trial name or generalizing its numbers. "
                "Follow the requested generation strategy and design_instruction within this boundary."
            )
        visible_claim_ids = set(claim_ids)
        claim_by_id = {claim.claim_id: claim for claim in claims}
        portfolio = dict(task.goal.get("question_portfolio", {}))
        attempted_hypothesis_ids = set(
            portfolio.get("attempted_hypothesis_ids", [])
        )
        previous = [q for q in portfolio.get("existing_questions", [])
                    if q.get("hypothesis_id") != task.goal.get("revision_of")]
        rejected_wordings = {item["question_intent"].strip().casefold() for item in previous if item.get("redesign_feedback")}
        existing_answers = {
            _answer_signature(item.get("answer_values", []))
            for item in previous
            if item.get("answer_values") and not item.get("redesign_feedback")
        }
        existing_claim_sets = [
            set(item.get("claim_ids", []))
            for item in previous
            if item.get("claim_ids") and not item.get("redesign_feedback")
        ]
        preferred_claim_ids = set(
            portfolio.get("preferred_claim_ids", [])
        )
        llm_owned = bool(task.goal.get("llm_owned_design"))
        accepted_hypotheses: list[QuestionHypothesis] = []
        answer_signatures: set[tuple[str, ...]] = set()
        accepted_claim_sets: list[set[str]] = []
        fact_selections: list[FactSelectionNote] = []

        def consume(batch: QuestionHypothesisBatch) -> bool:
            fact_selections.extend(batch.fact_selections)
            added_preferred = False
            for hypothesis in (batch.hypotheses if llm_owned else sorted(
                batch.hypotheses,
                key=lambda item: _hypothesis_rank(
                    item,
                    preferred_claim_ids=preferred_claim_ids,
                    claim_by_id=claim_by_id,
                ),
            )):
                if not llm_owned and hypothesis.question_intent.strip().casefold() in rejected_wordings:
                    continue
                limit = int(task.goal.get("question_count_upper_bound", 1))
                targets = set(task.goal.get("difficulty_targets", []))
                targeted = strategy.name != "free" and targets in ({"medium"}, {"hard"})
                supplementary = targeted and hypothesis.proposed_difficulty not in targets
                quota_indices = [i for i, item in enumerate(accepted_hypotheses)
                                 if not targeted or item.proposed_difficulty in targets]
                replaces_last = (
                    not llm_owned and not supplementary and limit > 0 and len(quota_indices) >= limit
                    and bool(preferred_claim_ids.intersection(hypothesis.candidate_claim_ids))
                    and not any(preferred_claim_ids.intersection(item.candidate_claim_ids)
                                for item in accepted_hypotheses)
                )
                if not llm_owned and not supplementary and len(quota_indices) >= limit and not replaces_last:
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
                if not llm_owned and answer_signature and (
                    answer_signature in existing_answers
                    or answer_signature in answer_signatures
                ):
                    continue
                if not llm_owned and any(
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
                if task.goal.get("direct_sources"):
                    if hypothesis.evidence_basis != "source_passages" or hypothesis.candidate_claim_ids:
                        raise RoleContractViolation("Direct design must use source_passages without invented claims")
                    supporting_anchor_ids = set(hypothesis.discovery_anchor_ids)
                    if not supporting_anchor_ids or not supporting_anchor_ids <= anchor_ids:
                        raise RoleContractViolation("Direct design references unavailable original excerpts")
                hypothesis = hypothesis.model_copy(
                    update={
                        "discovery_anchor_ids": sorted(
                            supporting_anchor_ids
                        )
                    }
                )
                semantic_payload = hypothesis.model_dump(
                    mode="json",
                    exclude={"hypothesis_id", "suggested_retrieval"},
                )
                accepted = hypothesis.model_copy(
                    update={
                        "hypothesis_id": (
                            "hypothesis."
                            + canonical_digest(semantic_payload)[:24]
                        ),
                        "suggested_retrieval": public_retrieval_suggestion(
                            hypothesis.question_intent, hypothesis.suggested_retrieval,
                        ),
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

        approved_question = (None if task.goal.get("revision_of") else
                             (task.goal.get("research_context") or {}).get("approved_question"))
        revision = task.goal.get("revision_context") or {}
        if revision.get("revision_target") == "answer":
            approved_question = revision["previous_candidate"]["question_intent"]
            # The fixed question and verification gaps define the repair. A
            # controller instruction can suggest the very answer under dispute.
            payload.pop("design_instruction")
            payload["research_context"] = {**(payload.get("research_context") or {}),
                                           "approved_question": approved_question}
        if approved_question:
            payload["instruction"] = (
                "Answer only research_context.approved_question using the supplied originals. "
                "Use its information_need and supported_observations to locate evidence, then confirm them against the originals. "
                "When primary_evidence is present, use it to locate the core finding for its actual subject and group. "
                "Cover every required part of the public question and preserve qualifiers from text and table legends. "
                "A supported comparison alone does not answer an additional mechanism question. "
                "Return exactly one hypothesis with that question_intent unchanged, or no hypotheses "
                "with the specific missing evidence. Do not transform or split the question. "
                + ("Use evidence_basis=source_passages, candidate_claim_ids=[] and supplied discovery_anchor_ids. "
                   if task.goal.get("direct_sources") else
                   "Use candidate_claim_ids from the supplied claims and their supporting anchors; retain claim-based evidence provenance. ") +
                "Record fact_selections as source-grounded observations, not new claims.")
        if revision:
            payload["instruction"] += (
                " Revise from the original excerpts. Use verification_feedback to locate contested statements. "
                "Previous answers, controller instructions and review suggestions are not evidence. "
                "Before drafting, use fact_selections to record the relevant original anchor IDs and what was "
                "actually shown for each contested part: the named entity or intervention, the tested conditions, "
                "and the observed relationship. Prefer direct evidence on the exact subject. "
                "Keep evidence about related entities or interventions separate; sharing a binding site or "
                "pathway does not transfer their measured effects or mechanism details. Keep the supported core "
                "and its necessary qualifiers. Omit unsupported additional detail when the core still answers "
                "the question. If an essential relationship is missing, name it in insufficiency_reason. "
                "For an answer-only revision keep the question unchanged. Change the question only to correct "
                "an identified question defect, and keep it a self-contained domain question. "
                "Return at most one corrected candidate for fresh verification."
            )
        response_schema = (SourceQuestionHypothesisBatch if task.goal.get("direct_sources")
                           else QuestionHypothesisBatch)
        source_reading, source_usage = None, {}
        if approved_question:
            response_schema = fixed_question_batch(
                approved_question, direct_sources=bool(task.goal.get("direct_sources")),
                source_anchor_ids=tuple(item["anchor_id"] for item in payload["evidence_excerpts"]),
            )
            if (task.goal.get("direct_sources") and payload["evidence_excerpts"]
                    and task.budget.get("max_model_calls", 1) >= 2):
                source_reading, source_usage = _read_answer_sources(
                    self.model, task, approved_question, payload["evidence_excerpts"],
                )
                if source_reading["status"] == "read":
                    payload["answer_source_reading"] = source_reading
                    payload["instruction"] += (
                        " Use answer_source_reading to locate direct observations and their boundaries. "
                        "Verify the extracted subject, conditions and result against its exact quote and original excerpt. "
                        "Base the core answer on findings for the asked subject. Use background and results about "
                        "other subjects only for context with distinct attribution."
                    )
                    if revision.get("revision_target") == "answer" and source_reading["observations"]:
                        # Reconstruct the fixed answer from the sources so past
                        # drafts and their reviews cannot prime disputed details.
                        payload = {key: payload[key] for key in (
                            "task", "input_mode", "evidence_excerpts", "answer_source_reading",
                        )}
                        payload["research_context"] = {"approved_question": approved_question}
                        payload["instruction"] = (
                            "Answer the fixed question from the original excerpts, using the source reading to find "
                            "direct findings about the asked subject and the conditions that limit those findings. "
                            "Use the smallest complete answer. Leave out optional details supported only for other "
                            "subjects or interventions. If a necessary part lacks support, state the specific gap. "
                            "Keep the question verbatim. Use evidence_basis=source_passages and cite the excerpts "
                            "that actually support the answer."
                        )
                    payload["instruction"] += (
                        " Answer the requested_result identified from the public question. Compare it with the "
                        "question itself. Include numeric results only when quantitative_result_needed is true "
                        "and the question calls for a magnitude, count or numeric threshold. A variant name, "
                        "exon number or other identifier containing digits is not a quantitative result. "
                        "When a figure is needed, keep its measured entity, denominator, condition and unit together."
                        " In an explanation, report supported effects and associations with their original strength. "
                        "Only link an intermediate effect to a downstream outcome when a verbatim source establishes "
                        "that link. If the same intervention changes two outcomes, describe both as its effects, "
                        "without making one the cause of the other. Use unestablished_links to check for such leaps."
                    )
                    if (source_reading["direct_observations_cover_question"] and source_reading["unestablished_links"]
                            and not source_reading["missing_relationships"]):
                        direct = [item for item in source_reading["observations"] if item["relevance"] == "direct"]
                        if direct and all(item["relationship"] in {"description", "observed_intervention_effect", "association"}
                                          for item in direct):
                            by_anchor = {item["anchor_id"]: item for item in payload["evidence_excerpts"]}
                            payload = {key: payload[key] for key in ("task", "input_mode", "instruction")}
                            payload["research_context"] = {"approved_question": approved_question}
                            payload["answer_source_reading"] = {**source_reading, "observations": direct}
                            payload["evidence_excerpts"] = [
                                {**by_anchor[item["anchor_id"]], "quoted_text": item["quote"],
                                 "start_in_anchor": item["start_in_anchor"], "end_in_anchor": item["end_in_anchor"]}
                                for item in direct
                            ]
                            source_reading["draft_evidence_scope"] = "direct_observation_sentences"
                            payload["instruction"] = (
                                "Answer the fixed public question with only the observed finding or comparison "
                                "in these exact source sentences. Preserve the model, groups and uncertainty. "
                                "Do not add an intermediate mechanism. Cite the provided anchor IDs. "
                                "If context such as a missing comparison group or table definition makes the result "
                                "ambiguous, report that gap instead of filling it in."
                            )
                            response_schema = fixed_question_batch(
                                approved_question, direct_sources=True,
                                source_anchor_ids=tuple(dict.fromkeys(item["anchor_id"] for item in direct)),
                            )
        if approved_question:
            payload["instruction"] += (
                " When the answer describes different measurements, use separate clauses with the applicable "
                "effect size and significance attached to the measured endpoint. A reported fold change in RNA "
                "cannot be extended to protein levels that were only reported as correlated with RNA. "
                "Report the direction and significance of a comparison without its exact P value unless "
                "the question asks for that value."
            )
        payload["instruction"] += (
            " For each hypothesis, supply suggested_retrieval alongside the question: a concise first search query "
            "using only the public question, suggested sources such as PubMed, the evidence to look for, "
            "and a question-specific stop condition covering every subquestion. Describe a proposed search; "
            "do not claim it was executed. In all four fields use only concepts available in the public "
            "question. Describe what relationship, value, or comparison the evidence must establish "
            "without naming its result, an answer-only search term, or the source that supplied the answer."
        )
        response = _generate_design_batch(self.model,
            StructuredGenerationRequest(
                invocation_id=task.initial_attempt_id,
                role="objective_grounded_answer" if approved_question else "objective_question_design",
                system_prompt_version="grounded-answer-v1" if approved_question else self.capability.prompt_version,
                payload=payload,
                idempotency_key=task.operation_key,
            ),
            response_schema,
        )
        usage = dict(response.usage)
        for key in ("input_tokens", "output_tokens", "attempts"):
            if key in source_usage:
                usage[key] = (usage.get(key) or 0) + (source_usage[key] or 0)
        initial_batch = QuestionHypothesisBatch.model_validate(response.parsed)
        if approved_question and (len(initial_batch.hypotheses) > 1 or any(
                h.question_intent != approved_question for h in initial_batch.hypotheses)):
            raise RoleContractViolation("Answer stage must preserve the approved question; return evidence gaps instead of changing it")
        consume(initial_batch)
        if not initial_batch.hypotheses:
            return QuestionDesignOutcome(
                result=SubagentResult(
                    subtask_id=task.subtask_id,
                    execution_status=ExecutionStatus.SUCCEEDED,
                    domain_outcome=DomainOutcome.INSUFFICIENT,
                    verdict=VerificationVerdict.NOT_EMITTED,
                    output_artifact_ids=[], evidence_need_ids=[],
                    usage=usage, error_category=None,
                    stop_reason=initial_batch.insufficiency_reason or "no_supported_hypothesis",
                ),
                hypotheses=[],
                fact_selections=fact_selections,
                answer_source_reading=source_reading,
            )

        has_preferred = any(
            preferred_claim_ids.intersection(
                hypothesis.candidate_claim_ids
            )
            for hypothesis in accepted_hypotheses
        )
        if not llm_owned and preferred_claim_ids and not has_preferred:
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
                    response_schema,
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
            usage=usage,
            error_category=None,
            stop_reason="hypotheses_proposed",
        )
        return QuestionDesignOutcome(
            result=result,
            hypotheses=accepted_hypotheses,
            fact_selections=fact_selections,
            answer_source_reading=source_reading,
        )
