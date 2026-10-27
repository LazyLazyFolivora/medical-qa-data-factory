"""Realize validated tasks and verify that wording preserves task semantics."""
from __future__ import annotations

import re

from pydantic import Field

from ..domain.common import ContractModel, UtcDateTime, utc_now
from .logical_query import LogicalDirection
from .task_contracts import ValidatedTask


class QuestionVariant(ContractModel):
    """One wording of a validated task; it carries no answer field."""

    variant_id: str = Field(min_length=1)
    qid: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    question_text: str = Field(min_length=1)
    language: str = "zh-CN"
    writer_version: str = Field(min_length=1)
    created_at: UtcDateTime = Field(default_factory=utc_now)


class TextSemanticReport(ContractModel):
    """Deterministic semantic gate for a question variant."""

    variant_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    passed: bool
    reason_codes: list[str] = Field(default_factory=list)
    reconstructed_query_digest: str | None = None
    verifier_version: str = Field(min_length=1)
    created_at: UtcDateTime = Field(default_factory=utc_now)


class TaskQuestionWriter:
    """Write the first supported constrained-entity question type.

    This writer intentionally supports only relation families with explicit,
    reviewed biomedical wording. Unknown relations are rejected rather than
    rendered through a vague "相关" fallback.
    """

    version = "task-question-writer-v1"

    def write(self, task: ValidatedTask, *, qid: str) -> QuestionVariant:
        if not isinstance(task, ValidatedTask):
            raise TypeError("TaskQuestionWriter only accepts ValidatedTask")
        spec = task.task
        if spec.question_type != "constrained_entity":
            raise ValueError("the first writer slice supports constrained entities")
        if len(spec.query.steps) != 1 or len(spec.query.constraints) != 1:
            raise ValueError("constrained writer requires one primary constraint")

        seed_name = self._public_name(spec, spec.seed_entity_id)
        constraint = spec.query.constraints[0]
        terminal_name = self._public_name(spec, constraint.terminal_entity_id)
        primary_relation = spec.query.steps[0].relation
        constraint_relation = constraint.steps[0].relation
        answer_noun = self._answer_noun(spec.query.answer_entity_type)

        relation_key = self._relation_key(spec)
        if relation_key == (
            "targets",
            LogicalDirection.FORWARD,
            "associated_with",
            LogicalDirection.FORWARD,
        ):
            question = (
                f"{seed_name}所靶向且与{terminal_name}相关的"
                f"{answer_noun}是哪一种？"
            )
        elif relation_key == (
            "disease_protein",
            LogicalDirection.FORWARD,
            "pathway_protein",
            LogicalDirection.REVERSE,
        ):
            question = (
                f"与{seed_name}相关且参与{terminal_name}的"
                f"{answer_noun}是哪一种？"
            )
        else:
            raise ValueError(f"unsupported relation composition: {relation_key}")
        return QuestionVariant(
            variant_id=f"variant-{qid}-{self.version}",
            qid=qid,
            task_id=spec.task_id,
            question_text=question,
            writer_version=self.version,
        )

    @staticmethod
    def _public_name(spec, entity_id: str) -> str:
        try:
            return spec.public_entity_names[entity_id]
        except KeyError as exc:
            raise ValueError(
                f"no public display name for entity {entity_id}"
            ) from exc

    @staticmethod
    def _answer_noun(entity_type: str | None) -> str:
        normalized = (entity_type or "").lower().replace("_", "/")
        if normalized in {"gene", "protein", "gene/protein"}:
            return "蛋白"
        if normalized == "disease":
            return "疾病"
        if normalized == "pathway":
            return "通路"
        raise ValueError(f"unsupported answer entity type: {entity_type}")

    @staticmethod
    def _relation_key(spec) -> tuple[str, LogicalDirection, str, LogicalDirection]:
        constraint = spec.query.constraints[0]
        return (
            spec.query.steps[0].relation,
            spec.query.steps[0].direction,
            constraint.steps[0].relation,
            constraint.steps[0].direction,
        )


class TaskTextSemanticVerifier:
    """Fail closed when a wording drops constraints or changes semantics."""

    version = "task-text-semantic-verifier-v1"

    def verify(
        self,
        variant: QuestionVariant,
        task: ValidatedTask,
    ) -> TextSemanticReport:
        if variant.task_id != task.task.task_id:
            raise ValueError("question variant belongs to a different task")
        text = variant.question_text
        lowered = text.casefold()
        reasons: set[str] = set()

        forbidden_answer_names = [
            *task.answer_contract.canonical_answer_ids,
            *task.answer_contract.accepted_aliases,
        ]
        for name in forbidden_answer_names:
            if name and name.casefold() in lowered:
                reasons.add("answer_leaked")

        spec = task.task
        seed_name = spec.public_entity_names.get(spec.seed_entity_id)
        if not seed_name or seed_name not in text:
            reasons.add("seed_anchor_missing")

        if len(spec.query.constraints) != 1:
            reasons.add("constraint_missing")
        else:
            constraint = spec.query.constraints[0]
            terminal_name = spec.public_entity_names.get(
                constraint.terminal_entity_id
            )
            if not terminal_name or terminal_name not in text:
                reasons.add("constraint_missing")
            if len(spec.query.steps) != 1 or len(constraint.steps) != 1:
                reasons.add("constraint_missing")
            else:
                expected_noun = TaskQuestionWriter._answer_noun(
                    spec.query.answer_entity_type
                )
                relation_key = TaskQuestionWriter._relation_key(spec)
                if relation_key == (
                    "targets",
                    LogicalDirection.FORWARD,
                    "associated_with",
                    LogicalDirection.FORWARD,
                ):
                    parsed = re.fullmatch(
                        r"(?P<seed>.+)所靶向且与(?P<terminal>.+)相关的"
                        r"(?P<noun>蛋白|疾病|通路)是哪一种[？?]",
                        text,
                    )
                elif relation_key == (
                    "disease_protein",
                    LogicalDirection.FORWARD,
                    "pathway_protein",
                    LogicalDirection.REVERSE,
                ):
                    parsed = re.fullmatch(
                        r"与(?P<seed>.+)相关且参与(?P<terminal>.+)的"
                        r"(?P<noun>蛋白|疾病|通路)是哪一种[？?]",
                        text,
                    )
                else:
                    parsed = None
                    reasons.add("unsupported_relation_semantics")
                if parsed is None:
                    reasons.add("round_trip_parse_failed")
                elif (
                    parsed.group("seed") != seed_name
                    or parsed.group("terminal") != terminal_name
                    or parsed.group("noun") != expected_noun
                ):
                    reasons.add("round_trip_semantics_mismatch")

            if any(word in text for word in ("导致", "治疗", "预防", "引起")):
                reasons.add("relation_semantics_violation")

        if any(word in text for word in ("PrimeKG", "head", "tail", "节点", "边")):
            reasons.add("implementation_vocabulary")
        if not text.endswith(("？", "?")):
            reasons.add("not_a_question")

        reason_codes = sorted(reasons)
        return TextSemanticReport(
            variant_id=variant.variant_id,
            task_id=spec.task_id,
            passed=not reason_codes,
            reason_codes=reason_codes,
            reconstructed_query_digest=(
                spec.query.dependency_digest if not reason_codes else None
            ),
            verifier_version=self.version,
        )


class CtdTaskQuestionWriter:
    """Deterministic first writer for validated CTD study-context tasks."""

    version = "ctd-task-question-writer-v1"

    def write(self, task: ValidatedTask, *, qid: str) -> QuestionVariant:
        if task.task.question_type != "study_context_qualified_disease":
            raise ValueError("CTD writer only accepts study-context tasks")
        question = _ctd_question_text(task)
        return QuestionVariant(
            variant_id=f"variant-{qid}-{self.version}",
            qid=qid,
            task_id=task.task.task_id,
            question_text=question,
            writer_version=self.version,
        )


class CtdTaskTextSemanticVerifier:
    """Verify that a CTD question retains every public task constraint."""

    version = "ctd-text-semantic-verifier-v1"
    _FORBIDDEN = (
        "ctd",
        "当前纳入的研究证据",
        "当前证据范围",
        "某疾病",
        "特定人群",
        "某药物",
        "某化学物",
        "claim",
        "evidence",
    )

    def verify(
        self,
        variant: QuestionVariant,
        task: ValidatedTask,
    ) -> TextSemanticReport:
        context = task.task.public_study_context
        if context is None:
            return self._failure(variant, task, "missing_public_context")
        chemical = task.task.public_entity_names.get(
            task.task.seed_entity_id
        )
        text = variant.question_text.casefold()
        required = [
            context.disease_category,
            chemical,
            context.population,
            context.exposure_context,
            context.dose_or_regimen,
            context.exposure_duration,
            context.study_type,
            context.sample_type,
            context.temporal_scope,
            *context.additional_qualifiers.values(),
        ]
        reasons: list[str] = []
        if any(
            value and value.casefold() not in text
            for value in required
        ):
            reasons.append("missing_public_disambiguator")
        if any(term.casefold() in text for term in self._FORBIDDEN):
            reasons.append("database_trivia_or_internal_term")
        if any(
            placeholder in variant.question_text
            for placeholder in (
                "某疾病",
                "特定人群",
                "某药物",
                "某化学物",
                "化学物 A",
                "药物 A",
                "剂量 D",
                "特征 S",
            )
        ):
            reasons.append("uninstantiated_placeholder")
        hidden_answers = [
            *task.answer_contract.accepted_aliases,
            *task.answer_contract.canonical_answer_ids,
        ]
        if any(
            answer.casefold() in text
            for answer in hidden_answers
        ):
            reasons.append("answer_leakage")
        if (
            context.relation_intent == "therapeutic_research"
            and any(
                term in text
                for term in (
                    "治疗有效",
                    "有效治疗",
                    "能够治疗",
                    "能够有效治疗",
                    "可治愈",
                )
            )
        ):
            reasons.append("claim_semantics_mismatch")
        if (
            variant.question_text != _ctd_question_text(task)
            and "question_semantics_mismatch" not in reasons
        ):
            reasons.append("question_semantics_mismatch")
        if reasons:
            return TextSemanticReport(
                variant_id=variant.variant_id,
                task_id=variant.task_id,
                passed=False,
                reason_codes=sorted(reasons),
                verifier_version=self.version,
            )
        return TextSemanticReport(
            variant_id=variant.variant_id,
            task_id=variant.task_id,
            passed=True,
            reconstructed_query_digest=task.task.query.dependency_digest,
            verifier_version=self.version,
        )

    @staticmethod
    def _failure(
        variant: QuestionVariant,
        task: ValidatedTask,
        reason: str,
    ) -> TextSemanticReport:
        return TextSemanticReport(
            variant_id=variant.variant_id,
            task_id=task.task.task_id,
            passed=False,
            reason_codes=[reason],
            verifier_version=CtdTaskTextSemanticVerifier.version,
        )


def _ctd_context_text(context) -> str:
    parts: list[str] = []
    if context.population:
        parts.append(context.population)
    if context.exposure_context:
        parts.append(context.exposure_context)
    if context.dose_or_regimen:
        parts.append(f"剂量或方案为{context.dose_or_regimen}")
    if context.exposure_duration:
        parts.append(f"持续{context.exposure_duration}")
    if context.study_type:
        parts.append(context.study_type)
    if context.sample_type:
        parts.append(context.sample_type)
    if context.temporal_scope:
        parts.append(context.temporal_scope)
    parts.extend(
        f"{key}为{value}"
        for key, value in sorted(context.additional_qualifiers.items())
    )
    return "、".join(parts)


def _ctd_question_text(task: ValidatedTask) -> str:
    context = task.task.public_study_context
    if context is None:
        raise ValueError("CTD task is missing its public study context")
    chemical = task.task.public_entity_names.get(task.task.seed_entity_id)
    if not chemical:
        raise ValueError("CTD task is missing the public chemical name")
    qualifier_text = _ctd_context_text(context)
    disease_category = context.disease_category
    intent = context.relation_intent
    if intent == "risk_decrease":
        ending = f"哪种{disease_category}的发生风险被报告降低？"
    elif intent == "risk_increase":
        ending = f"哪种{disease_category}的发生风险被报告升高？"
    elif intent == "association_research":
        ending = (
            f"哪种{disease_category}被研究与{chemical}暴露存在关联？"
        )
    elif intent == "biomarker_research":
        ending = (
            f"哪种{disease_category}被研究作为相关候选生物标志物？"
        )
    elif intent == "therapeutic_research":
        ending = (
            f"哪种{disease_category}是{chemical}被研究用于改善的疾病？"
        )
    else:
        ending = (
            f"哪种{disease_category}被报告为{chemical}相关的不良事件？"
        )
    return f"在{qualifier_text}中，{ending}"
