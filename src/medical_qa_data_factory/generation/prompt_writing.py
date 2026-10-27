"""Prompt-constrained question writing with independent semantic read-back."""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field

from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from .task_contracts import (
    StudyRelationIntent,
    ValidatedTask,
)
from .task_realization import QuestionVariant, TextSemanticReport


class GeneratedQuestionText(ContractModel):
    question_text: str = Field(min_length=1)
    language: str = Field(default="zh-CN", min_length=2)


class QuestionSemanticView(ContractModel):
    chemical_name: str | None = None
    relation_intent: StudyRelationIntent
    polarity: Literal["increase", "decrease", "neutral", "unknown"]
    population: str | None = None
    exposure_context: str | None = None
    dose_or_regimen: str | None = None
    exposure_duration: str | None = None
    study_type: str | None = None
    sample_type: str | None = None
    disease_category: str | None = None
    temporal_scope: str | None = None
    additional_qualifiers: dict[str, str] = Field(default_factory=dict)
    cardinality: Literal["exactly_one", "one_or_more", "exact_set"]
    uncertain: bool = False


class PromptQuestionWriter:
    """Generate a question from a Task without a program-fixed sentence."""

    version = "prompt-question-writer-v1"

    def __init__(self, model) -> None:
        self.model = model

    def write(self, task: ValidatedTask, *, qid: str) -> QuestionVariant:
        if task.task.public_study_context is None:
            raise ValueError("prompt writer currently needs a public study context")
        request = StructuredGenerationRequest(
            invocation_id=f"prompt-writer.{qid}",
            role="question_writer",
            system_prompt_version="writer-v2",
            payload={
                "task": task.task.model_dump(mode="json"),
                "answer_contract": task.answer_contract.model_dump(mode="json"),
                "constraints": [
                    "write one standalone natural-language question",
                    "preserve all public study qualifiers",
                    "preserve intent, polarity and cardinality",
                    "must not state the answer, aliases or internal IDs",
                    "must not mention claim, evidence, PMID, CTD or PubMed",
                    "therapeutic research must not be upgraded to efficacy",
                    "association must not be upgraded to causation",
                ],
            },
            idempotency_key=f"{task.task.task_id}:{self.version}:{qid}",
        )
        response = self.model.generate_structured(
            request,
            GeneratedQuestionText,
        )
        generated = GeneratedQuestionText.model_validate(response.parsed)
        return QuestionVariant(
            variant_id=f"variant-{qid}-{self.version}",
            qid=qid,
            task_id=task.task.task_id,
            question_text=generated.question_text,
            language=generated.language,
            writer_version=self.version,
        )


class PromptQuestionSemanticVerifier:
    """Reconstruct question semantics and compare them with ValidatedTask."""

    version = "prompt-semantic-verifier-v1"

    def __init__(self, model) -> None:
        self.model = model

    def verify(
        self,
        question_text: str,
        task: ValidatedTask,
    ) -> TextSemanticReport:
        deterministic = self._deterministic_reasons(question_text, task)
        if deterministic:
            return self._report(
                task,
                variant_id=f"prompt-verification-{task.task.task_id}",
                reasons=deterministic,
            )
        request = StructuredGenerationRequest(
            invocation_id=f"prompt-verifier.{task.task.task_id}",
            role="answer_verifier",
            system_prompt_version="verifier-v2",
            payload={
                "question": question_text,
                "validated_task": task.task.model_dump(mode="json"),
                "constraints": [
                    "reconstruct the public semantics without changing them",
                    "report uncertainty instead of guessing",
                    "do not answer the question",
                ],
            },
            idempotency_key=(
                f"{task.task.task_id}:{self.version}:"
                f"{hashlib.sha256(question_text.encode('utf-8')).hexdigest()[:16]}"
            ),
        )
        response = self.model.generate_structured(
            request,
            QuestionSemanticView,
        )
        view = QuestionSemanticView.model_validate(response.parsed)
        reasons = self._semantic_reasons(view, task)
        return self._report(
            task,
            variant_id=f"prompt-verification-{task.task.task_id}",
            reasons=reasons,
        )

    @staticmethod
    def _deterministic_reasons(
        question_text: str,
        task: ValidatedTask,
    ) -> list[str]:
        reasons: list[str] = []
        lowered = question_text.casefold()
        hidden = [
            *task.answer_contract.accepted_aliases,
            *task.answer_contract.canonical_answer_ids,
        ]
        if any(
            value and value.casefold() in lowered
            for value in hidden
        ):
            reasons.append("answer_leakage")
        internal_terms = (
            "claim",
            "evidence",
            "pmid",
            "ctd",
            "pubmed",
            "task_id",
            "qid",
        )
        if any(term in lowered for term in internal_terms):
            reasons.append("internal_terminology")
        if not question_text.rstrip().endswith(("？", "?")):
            reasons.append("not_a_question")
        return sorted(set(reasons))

    @staticmethod
    def _semantic_reasons(
        view: QuestionSemanticView,
        task: ValidatedTask,
    ) -> list[str]:
        reasons: list[str] = []
        context = task.task.public_study_context
        if context is None:
            return ["missing_public_context"]
        if view.uncertain:
            reasons.append("semantic_reconstruction_uncertain")
        expected_chemical = task.task.public_entity_names.get(
            task.task.seed_entity_id
        )
        if (
            expected_chemical
            and view.chemical_name
            and view.chemical_name.casefold()
            != expected_chemical.casefold()
        ):
            reasons.append("entity_mismatch")
        if (
            view.relation_intent != context.relation_intent
            or view.polarity != context.polarity
        ):
            reasons.append("intent_polarity_mismatch")
        if view.cardinality != task.answer_contract.cardinality:
            reasons.append("cardinality_mismatch")
        expected_qualifiers = {
            "population": context.population,
            "exposure_context": context.exposure_context,
            "dose_or_regimen": context.dose_or_regimen,
            "exposure_duration": context.exposure_duration,
            "study_type": context.study_type,
            "sample_type": context.sample_type,
            "disease_category": context.disease_category,
            "temporal_scope": context.temporal_scope,
        }
        for key, expected in expected_qualifiers.items():
            if expected is None:
                continue
            actual = getattr(view, key)
            if (
                actual is None
                or actual.casefold() != expected.casefold()
            ):
                reasons.append("qualifier_mismatch")
                break
        if view.additional_qualifiers != context.additional_qualifiers:
            reasons.append("qualifier_mismatch")
        return sorted(set(reasons))

    @staticmethod
    def _report(
        task: ValidatedTask,
        *,
        variant_id: str,
        reasons: list[str],
    ) -> TextSemanticReport:
        return TextSemanticReport(
            variant_id=variant_id,
            task_id=task.task.task_id,
            passed=not reasons,
            reason_codes=reasons,
            reconstructed_query_digest=(
                task.task.query.dependency_digest if not reasons else None
            ),
            verifier_version=PromptQuestionSemanticVerifier.version,
        )
