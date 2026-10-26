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
