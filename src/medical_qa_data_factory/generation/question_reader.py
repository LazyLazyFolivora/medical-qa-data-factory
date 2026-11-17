"""Shared, answer-blind question review for discovery and candidate revisions."""
from typing import Literal

from pydantic import Field, model_validator

from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from ..llm.prompts import TEMPLATES
from .source_question_inputs import SELECTED_EVIDENCE_CHAR_BUDGET
from .subagent_contracts import RoleContractViolation
from .verification_evidence_view import generate_with_evidence

QUESTION_REVIEW_VERSION = (
    "question-first-v8:"
    + TEMPLATES["objective_question_form_reader"].hash()[:16]
    + ":" + TEMPLATES["objective_question_reader"].hash()[:16]
)


class ReaderJudgment(ContractModel):
    index: int = Field(ge=0)
    verdict: Literal["ACCEPT", "REVISE", "RESELECT", "NEEDS_EVIDENCE"]
    reason: str = Field(max_length=1000)


class ReaderReview(ContractModel):
    judgments: list[ReaderJudgment] = Field(max_length=4)


class FormJudgment(ContractModel):
    index: int = Field(ge=0)
    task_in_question: str = Field(max_length=400,
        description="Describe what the literal question asks the reader to do; do not silently rewrite it.")
    missing_required_context: list[str] = Field(max_length=4,
        description="Required but missing options, material, figures, referents or defining conditions. "
                    "Empty if none. Unknown answers are not missing context.")
    task_kind: Literal["domain_knowledge", "source_specific_reading", "incomplete_question"]
    verdict: Literal["ACCEPT", "REVISE", "RESELECT"]
    reason: str = Field(max_length=700)


class FormReview(ContractModel):
    judgments: list[FormJudgment] = Field(max_length=4)


class PremiseCheck(ContractModel):
    question_words: str = Field(max_length=300)
    asserted_fact: str = Field(max_length=400)
    source_words: str = Field(max_length=900)
    source_population_and_comparison: str = Field(max_length=400)
    observation_or_explanation: str = Field(max_length=500)
    scope_or_certainty_difference: str = Field(max_length=500)
    supported: bool


class PremiseJudgment(ContractModel):
    index: int = Field(ge=0)
    premises: list[PremiseCheck] = Field(max_length=4)
    verdict: Literal["ACCEPT", "REVISE", "RESELECT", "NEEDS_EVIDENCE"]
    reason: str = Field(max_length=1000)

    @model_validator(mode="after")
    def acceptance_matches_checks(self):
        if self.verdict == "ACCEPT" and any(not premise.supported for premise in self.premises):
            raise ValueError("ACCEPT contradicts an unsupported asserted premise; resolve the judgment against the source")
        return self


class PremiseReview(ContractModel):
    judgments: list[PremiseJudgment] = Field(max_length=4)


def review_questions(model, *, questions, materials, goal, operation_id, navigate_materials=False,
                     question_source_ids=None, question_required_scope=None):
    """Judge the public task and premises without proposed answers or writer notes."""
    usage = {}

    def call(role, version, selected, payload, suffix):
        schema = PremiseReview if suffix == "premises" else FormReview
        request = StructuredGenerationRequest(
            role=role, system_prompt_version=version,
            invocation_id=f"{operation_id}.{suffix}", idempotency_key=f"{operation_id}|{suffix}",
            payload={"questions": selected,
                     "review_context": "Evaluate the public question as written.",
                     **payload})
        response = (generate_with_evidence(model, request, schema, "materials",
                    verbatim_text_budget=SELECTED_EVIDENCE_CHAR_BUDGET)
                    if navigate_materials and suffix == "premises" else model.generate_structured(request, schema))
        for key, value in response.usage.items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
        review = schema.model_validate(response.parsed)
        expected = {q["index"] for q in selected}
        if len(review.judgments) != len(expected) or {j.index for j in review.judgments} != expected:
            raise RoleContractViolation("Reader must review each supplied question exactly once")
        return review

    # The form reader must not infer missing public context from private evidence
    # or even a generation goal containing the seed entity or planner hints.
    form_audit = call("objective_question_form_reader",
                      TEMPLATES["objective_question_form_reader"].version, questions, {}, "form")
    form = ReaderReview(judgments=[ReaderJudgment(index=j.index, verdict=j.verdict, reason=j.reason)
                                  for j in form_audit.judgments])
    accepted = {j.index for j in form.judgments if j.verdict == "ACCEPT"}
    selected = [q for q in questions if q["index"] in accepted]
    views = {"form_review": form_audit.model_dump(mode="json")}
    if not selected:
        return form, views, usage
    evidence_payload = {"task": goal, "materials": materials}
    if question_source_ids is not None:
        source_ids = {q["index"]: question_source_ids[q["index"]] for q in selected}
        visible_ids = {source_id for ids in source_ids.values() for source_id in ids}
        evidence_payload.update({
            "question_source_ids": source_ids,
            "materials": [m for m in materials if m["material_id"] in visible_ids],
            "source_scope_instruction": (
                "Use only the selected sources for each index, as only those sources will be handed to answer design. "
                "Check only factual premises asserted by the question. A narrower source or missing answer "
                "does not make the public question defective. Name unsupported premises as evidence gaps; "
                "request a correction only for a concrete error or confirmed missing condition."
            ),
        })
        views["question_source_ids"] = source_ids
    if question_required_scope is not None:
        planned_scope = {q["index"]: question_required_scope.get(q["index"], []) for q in selected}
        evidence_payload["question_required_scope"] = planned_scope
        evidence_payload["planned_scope_instruction"] = (
            "The planned scope is a hint from the writer, not established evidence. Compare it to the selected "
            "original sources and the exact public wording. If the public question drops an answer-changing "
            "genotype, experimental model or comparator confirmed by the evidence, request REVISE and name "
            "the precise missing condition so the writer can restore it. Ignore planned scope entries not "
            "supported by the source or not necessary for the information need."
        )
        views["question_required_scope"] = planned_scope
    premises = call("objective_question_reader", TEMPLATES["objective_question_reader"].version,
                    selected, evidence_payload, "premises")
    views["premise_review"] = premises.model_dump(mode="json")
    by_index = {j.index: j for j in form.judgments}
    for judgment in premises.judgments:
        # Older or overridden prompts can still emit RESELECT. Keep the form decision
        # and surface any actual unsupported premise as an evidence gap.
        if judgment.verdict == "RESELECT":
            if not any(not premise.supported for premise in judgment.premises):
                continue
            verdict = "NEEDS_EVIDENCE"
        else:
            verdict = judgment.verdict
        by_index[judgment.index] = ReaderJudgment(index=judgment.index, verdict=verdict, reason=judgment.reason)
    review = ReaderReview(judgments=[by_index[q["index"]] for q in questions])
    return review, views, usage
