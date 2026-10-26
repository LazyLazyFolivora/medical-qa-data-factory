"""Executable automated validators.

The registry in :mod:`medical_qa_data_factory.validation` only described which
checks depend on which fields. This module actually runs them, so a question
carries evidence of what was verified, by which validator version, and why it
failed instead of an empty ``not_run`` list forever.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable

from .domain import (
    Answerability,
    EvidenceBundle,
    QuestionSpec,
    Route,
    ValidationCheck,
    ValidationSeverity,
    ValidationStatus,
)
from .domain.common import new_id, utc_now
from .validation import BLOCKING_AUTOMATED
from .validation_applicability import question_dependency_digest


#: Bumped when the semantics of a check type move. A check recorded under an
#: older policy may not be read as if it were the new semantics.
VALIDATOR_VERSION = "validators.v2"

SCHEMA_REFERENCES = "schema_references"
EVIDENCE_SUPPORT = "evidence_support"
ANSWER_CONSISTENCY = "answer_evidence_consistency"
EVIDENCE_SUFFICIENCY = "evidence_sufficiency"
QUESTION_LEAKAGE = "question_leakage"
DUPLICATE_DETECTION = "duplicate_detection"
QUESTION_NATURALNESS = "question_naturalness"
SEMANTIC_EQUIVALENCE = "semantic_equivalence"
TAXONOMY_CONSISTENCY = "taxonomy_consistency"
ROUTE_PLAUSIBILITY = "route_plausibility"
SOURCE_CITABILITY = "source_citability"
#: Checks that the stored group key can be recomputed from the structure. It
#: does not judge whether two candidates are semantically the same.
REVIEW_GROUP_INTEGRITY = "review_group_integrity"

#: Checks that must be recorded before a question may leave ``draft``.
#:
#: ``duplicate_detection`` is deliberately absent. A candidate that looks like
#: another one is a signal for the reviewer, not a machine verdict: the
#: reviewer may keep both. The only blocking part is that the stored group
#: key must be recomputable.
BLOCKING_FOR_MACHINE_VALIDATION: tuple[str, ...] = (
    SCHEMA_REFERENCES,
    EVIDENCE_SUPPORT,
    ANSWER_CONSISTENCY,
    EVIDENCE_SUFFICIENCY,
    QUESTION_LEAKAGE,
    TAXONOMY_CONSISTENCY,
    ROUTE_PLAUSIBILITY,
    SOURCE_CITABILITY,
    REVIEW_GROUP_INTEGRITY,
)

#: Checks that only warn; a human decides naturalness, variant equivalence and
#: whether two candidates are really the same question.
ADVISORY_CHECKS: tuple[str, ...] = (
    QUESTION_NATURALNESS,
    SEMANTIC_EQUIVALENCE,
    DUPLICATE_DETECTION,
)

ALL_CHECK_TYPES: tuple[str, ...] = BLOCKING_FOR_MACHINE_VALIDATION + ADVISORY_CHECKS

#: Wording that betrays a template rather than a human-written question.
TEMPLATE_MARKERS: tuple[str, ...] = (
    "根据现有证据",
    "根据上述证据",
    "请回答",
    "填空",
    "TODO",
    "占位",
    "lorem ipsum",
)

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_PUNCTUATION = re.compile(r"[\s　]+")


def normalize_text(value: str) -> str:
    """Fold width, case and whitespace so comparisons are stable."""
    folded = unicodedata.normalize("NFKC", value).strip().lower()
    return _PUNCTUATION.sub(" ", folded)


def tokenize(value: str) -> set[str]:
    """Token set for English text plus per-character CJK characters."""
    folded = normalize_text(value)
    tokens = set(_TOKEN_PATTERN.findall(folded))
    for char in folded:
        if "\u4e00" <= char <= "\u9fff":
            tokens.add(char)
    return tokens


def jaccard(left: str, right: str) -> float:
    left_tokens = tokenize(left)
    right_tokens = tokenize(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


@dataclass(frozen=True)
class ValidationContext:
    """Everything a validator may read for one question."""

    question: QuestionSpec
    bundles: tuple[EvidenceBundle, ...]
    peers: tuple[QuestionSpec, ...] = ()

    @property
    def kg_evidence(self) -> dict[str, object]:
        return {
            item.evidence_id: item
            for bundle in self.bundles
            for item in bundle.kg_evidence
        }

    @property
    def literature_evidence(self) -> dict[str, object]:
        return {
            item.evidence_id: item
            for bundle in self.bundles
            for item in bundle.literature_evidence
        }

    @property
    def claims(self) -> dict[str, object]:
        return {
            claim.claim_id: claim
            for bundle in self.bundles
            for claim in bundle.claims
        }

    def question_texts(self) -> list[str]:
        return [
            self.question.canonical_question,
            *(variant.text for variant in self.question.question_variants),
        ]


@dataclass(frozen=True)
class Outcome:
    """Result of a single validator run."""

    check_type: str
    status: ValidationStatus
    message: str
    severity: ValidationSeverity | None = None
    artifact_ids: tuple[str, ...] = ()

    def severity_or_default(self) -> ValidationSeverity:
        if self.severity is not None:
            return self.severity
        if self.check_type in BLOCKING_AUTOMATED:
            return ValidationSeverity.BLOCKING
        if self.status is ValidationStatus.FAILED:
            return ValidationSeverity.BLOCKING
        return ValidationSeverity.WARNING


def _outcome(
    check_type: str,
    status: ValidationStatus,
    message: str,
    artifact_ids: Iterable[str] = (),
) -> Outcome:
    return Outcome(
        check_type=check_type,
        status=status,
        message=message,
        artifact_ids=tuple(artifact_ids),
    )


def _fail(check_type: str, message: str, artifact_ids: Iterable[str] = ()) -> Outcome:
    return _outcome(check_type, ValidationStatus.FAILED, message, artifact_ids)


def _warn(check_type: str, message: str, artifact_ids: Iterable[str] = ()) -> Outcome:
    return _outcome(check_type, ValidationStatus.WARNING, message, artifact_ids)


def _pass(check_type: str, message: str, artifact_ids: Iterable[str] = ()) -> Outcome:
    return _outcome(check_type, ValidationStatus.PASSED, message, artifact_ids)


def validate_schema_references(context: ValidationContext) -> Outcome:
    """Every referenced claim and evidence id must exist in a linked bundle."""
    requirements = context.question.evidence_requirements
    known_claims = set(context.claims)
    known_evidence = set(context.kg_evidence) | set(context.literature_evidence)
    if not context.bundles:
        return _fail(SCHEMA_REFERENCES, "问题关联的 evidence bundle 不存在")
    missing_claims = [
        claim_id
        for claim_id in requirements.required_claim_ids
        if claim_id not in known_claims
    ]
    required_evidence = (
        requirements.required_kg_evidence_ids
        + requirements.required_literature_evidence_ids
    )
    missing_evidence = [
        evidence_id for evidence_id in required_evidence if evidence_id not in known_evidence
    ]
    if missing_claims:
        return _fail(
            SCHEMA_REFERENCES,
            f"引用了不存在的 claim: {', '.join(sorted(missing_claims))}",
            missing_claims,
        )
    if missing_evidence:
        return _fail(
            SCHEMA_REFERENCES,
            f"引用了不存在的 evidence: {', '.join(sorted(missing_evidence))}",
            missing_evidence,
        )
    return _pass(SCHEMA_REFERENCES, "claim 与 evidence 引用完整")


def validate_evidence_support(context: ValidationContext) -> Outcome:
    """Each required claim needs at least one supporting, existing evidence."""
    requirements = context.question.evidence_requirements
    claims = context.claims
    known_evidence = set(context.kg_evidence) | set(context.literature_evidence)
    if not requirements.required_claim_ids:
        return _fail(EVIDENCE_SUPPORT, "evidence requirements 未声明 required_claim_ids")
    unsupported: list[str] = []
    for claim_id in requirements.required_claim_ids:
        claim = claims.get(claim_id)
        if claim is None:
            unsupported.append(claim_id)
            continue
        if not [eid for eid in claim.supporting_evidence_ids if eid in known_evidence]:
            unsupported.append(claim_id)
    if unsupported:
        return _fail(
            EVIDENCE_SUPPORT,
            "以下 required claim 没有可用的支持证据: " + ", ".join(sorted(unsupported)),
            unsupported,
        )
    return _pass(
        EVIDENCE_SUPPORT,
        f"{len(requirements.required_claim_ids)} 个 required claim 均有支持证据",
        requirements.required_claim_ids,
    )


def validate_answer_consistency(context: ValidationContext) -> Outcome:
    """Gold answer, answerability and route contract must agree."""
    question = context.question
    requirements = question.evidence_requirements
    if question.answerability is Answerability.UNANSWERABLE:
        if question.gold_answer.short_answer.strip():
            return _fail(
                ANSWER_CONSISTENCY,
                "unanswerable 问题不应给出非空 gold short answer",
            )
        return _pass(ANSWER_CONSISTENCY, "unanswerable 问题没有泄漏答案")

    if question.answerability is Answerability.UNCERTAIN:
        return _warn(ANSWER_CONSISTENCY, "answerability 为 uncertain，需要人工判断")

    if not question.gold_answer.short_answer.strip():
        return _fail(ANSWER_CONSISTENCY, "gold short answer 为空")
    if question.gold_answer.answer_mode is not question.taxonomy.answer_form:
        return _fail(
            ANSWER_CONSISTENCY,
            "taxonomy.answer_form 与 gold_answer.answer_mode 不一致",
        )
    route = question.expected_route
    has_kg = bool(requirements.required_kg_evidence_ids)
    has_lit = bool(requirements.required_literature_evidence_ids)
    if route is Route.KG and not has_kg:
        return _fail(ANSWER_CONSISTENCY, "expected_route=kg 但未要求任何 KG 证据")
    if route is Route.LITERATURE and not has_lit:
        return _fail(ANSWER_CONSISTENCY, "expected_route=literature 但未要求任何文献证据")
    if route is Route.HYBRID and not (has_kg and has_lit):
        return _fail(
            ANSWER_CONSISTENCY,
            "expected_route=hybrid 但未同时要求 KG 与文献证据",
        )
    if route is Route.UNANSWERABLE:
        return _fail(ANSWER_CONSISTENCY, "expected_route=unanswerable 与 answerability 冲突")
    return _pass(ANSWER_CONSISTENCY, "gold answer 与 evidence 要求一致")


def validate_evidence_sufficiency(context: ValidationContext) -> Outcome:
    """Required evidence must cover the claims needed to answer."""
    requirements = context.question.evidence_requirements
    claims = context.claims
    required_evidence = set(requirements.required_kg_evidence_ids) | set(
        requirements.required_literature_evidence_ids
    )
    if not required_evidence:
        return _fail(EVIDENCE_SUFFICIENCY, "没有声明任何 required evidence")
    for claim_id in requirements.required_claim_ids:
        claim = claims.get(claim_id)
        if claim is None:
            continue
        if not required_evidence & set(claim.supporting_evidence_ids):
            return _fail(
                EVIDENCE_SUFFICIENCY,
                f"claim {claim_id} 的支持证据未进入 required evidence 集合",
                [claim_id],
            )
    kg_ids = set(context.kg_evidence)
    lit_ids = set(context.literature_evidence)
    if set(requirements.required_kg_evidence_ids) - kg_ids:
        return _fail(EVIDENCE_SUFFICIENCY, "required_kg_evidence_ids 的来源类型标注错误")
    if set(requirements.required_literature_evidence_ids) - lit_ids:
        return _fail(EVIDENCE_SUFFICIENCY, "required_literature_evidence_ids 的来源类型标注错误")
    return _pass(
        EVIDENCE_SUFFICIENCY,
        f"sufficiency_rule_version={requirements.sufficiency_rule_version}，"
        f"required evidence {len(required_evidence)} 条",
        sorted(required_evidence),
    )


def validate_question_leakage(context: ValidationContext) -> Outcome:
    """The question text must not already contain the answer."""
    question = context.question
    if question.answerability is Answerability.UNANSWERABLE:
        return _pass(QUESTION_LEAKAGE, "unanswerable 问题无答案泄漏")
    candidates = [question.gold_answer.short_answer]
    for value in question.gold_answer.values:
        for key in ("name", "value", "text", "label"):
            if isinstance(value.get(key), str):
                candidates.append(value[key])
    texts = [normalize_text(text) for text in context.question_texts()]
    leaks = sorted(
        {
            candidate
            for candidate in candidates
            if len(candidate.strip()) >= 2
            and any(normalize_text(candidate) in text for text in texts)
        }
    )
    if leaks:
        return _fail(QUESTION_LEAKAGE, "问题文本中直接出现答案: " + ", ".join(leaks))
    return _pass(QUESTION_LEAKAGE, "问题文本未泄漏 gold answer")


def validate_duplicate_detection(context: ValidationContext) -> Outcome:
    """Flag candidates that look alike, without judging them.

    This check is advisory. Two candidates that read alike may still be
    different questions, and the reviewer may keep both. The machine's job
    is to point at the comparison, not to make the call.
    """
    question = context.question
    target = normalize_text(question.canonical_question)
    near: list[tuple[str, float]] = []
    exact = False
    for peer in context.peers:
        if peer.question_id == question.question_id:
            continue
        if peer.split_group_id != question.split_group_id:
            continue
        if normalize_text(peer.canonical_question) == target:
            exact = True
            continue
        score = jaccard(question.canonical_question, peer.canonical_question)
        if score >= 0.9:
            near.append((peer.question_id, score))
    if exact:
        return _warn(
            DUPLICATE_DETECTION,
            "同一 split group 内存在完全重复的标准问题，请人工比较",
        )
    if near:
        worst = max(near, key=lambda item: item[1])
        return _warn(
            DUPLICATE_DETECTION,
            f"同一 split group 内存在近重复问题（相似度 {worst[1]:.2f}），"
            "是否重复由人工判断",
            [item[0] for item in near],
        )
    return _pass(DUPLICATE_DETECTION, "同组内无完全重复或近重复问题")


def validate_review_group_integrity(context: ValidationContext) -> Outcome:
    """Check that the stored review group key is recomputable.

    This is a structural check, not a semantic one. It says nothing about
    whether two candidates in a group are really the same question; it says
    the key was derived from the structure rather than asserted.
    """
    question = context.question
    from .generation.grouping import GroupPayload, is_recomputable

    payload = group_payload_for_question(question)
    if payload is None:
        # The question carries no grouping structure at all. There is nothing
        # to recompute, so the check has no opinion: a question that was
        # never grouped is not a broken group key.
        return _pass(
            REVIEW_GROUP_INTEGRITY,
            "该问题不携带分组结构，无需复算分组键",
        )
    if not question.review_group_key:
        # The question has the structure a group key is built from, but no
        # key. That is a missing key, and a missing key is a defect.
        return _fail(
            REVIEW_GROUP_INTEGRITY,
            "问题缺少 review_group_key，无法复算分组",
        )
    if not is_recomputable(payload, question.review_group_key):
        return _fail(
            REVIEW_GROUP_INTEGRITY,
            "review_group_key 与问题的结构不一致，无法复算",
        )
    return _pass(REVIEW_GROUP_INTEGRITY, "review_group_key 可由结构复算")


def group_payload_for_question(question: Any) -> Any:
    """The grouping payload of a question, from its structured fields.

    Returns ``None`` when the question does not carry the structure the
    grouping needs.
    """
    intent = question.query_intent or {}
    seed_provider_id = str(intent.get("provider_id") or "")
    seed_entity_id = str(intent.get("seed_entity_id") or "")
    if not seed_provider_id or not seed_entity_id:
        return None
    relations = intent.get("relations")
    directions = intent.get("directions")
    if not isinstance(relations, list) or not relations:
        return None
    if not isinstance(directions, list) or len(directions) != len(relations):
        return None
    from .generation.grouping import GroupPayload, GroupStep

    steps: list[GroupStep] = []
    for index, (relation, direction) in enumerate(zip(relations, directions)):
        steps.append(
            GroupStep(
                position=index,
                relation=str(relation),
                direction=str(direction),
                target_entity_type=None,
            )
        )
    return GroupPayload(
        seed_provider_id=seed_provider_id,
        seed_entity_id=seed_entity_id,
        seed_entity_type=intent.get("seed_entity_type"),
        steps=steps,
        answer_entity_type=intent.get("answer_entity_type"),
    )


def validate_question_naturalness(context: ValidationContext) -> Outcome:
    """Flag templated phrasing so a human reviews it."""
    hits: list[str] = []
    for index, text in enumerate(context.question_texts()):
        lowered = normalize_text(text)
        for marker in TEMPLATE_MARKERS:
            if normalize_text(marker) in lowered:
                label = "标准问题" if index == 0 else f"变体{index}"
                hits.append(f"{label}:{marker}")
    if hits:
        return _warn(QUESTION_NATURALNESS, "检测到模板化措辞: " + ", ".join(hits))
    return _pass(QUESTION_NATURALNESS, "未检测到明显模板措辞")


def validate_semantic_equivalence(context: ValidationContext) -> Outcome:
    """Variants must stay close to the canonical question and hide the answer."""
    question = context.question
    answer = normalize_text(question.gold_answer.short_answer)
    drifted: list[str] = []
    leaked: list[str] = []
    for variant in question.question_variants:
        score = jaccard(question.canonical_question, variant.text)
        if score < 0.2:
            drifted.append(f"{variant.variant_id}({score:.2f})")
        if answer and len(answer) >= 2 and answer in normalize_text(variant.text):
            leaked.append(variant.variant_id)
    if leaked:
        return _fail(
            SEMANTIC_EQUIVALENCE,
            "问题变体泄漏答案: " + ", ".join(sorted(leaked)),
            leaked,
        )
    if drifted:
        return _warn(
            SEMANTIC_EQUIVALENCE,
            "问题变体与标准问题语义偏离较大: " + ", ".join(drifted),
            drifted,
        )
    return _pass(SEMANTIC_EQUIVALENCE, f"{len(question.question_variants)} 个变体语义一致")


def validate_taxonomy_consistency(context: ValidationContext) -> Outcome:
    """Taxonomy must be filled in and consistent with the question shape."""
    taxonomy = context.question.taxonomy
    problems: list[str] = []
    if not taxonomy.domain_intent.strip():
        problems.append("domain_intent 为空")
    if not taxonomy.reasoning_pattern:
        problems.append("reasoning_pattern 为空")
    if taxonomy.hop_complexity is None:
        problems.append("hop_complexity 未标注")
    if problems:
        return _fail(TAXONOMY_CONSISTENCY, "；".join(problems))
    return _pass(TAXONOMY_CONSISTENCY, f"taxonomy 完整（{taxonomy.domain_intent}）")


def validate_route_plausibility(context: ValidationContext) -> Outcome:
    """The declared route must be compatible with collected evidence."""
    question = context.question
    requirements = question.evidence_requirements
    has_kg = bool(requirements.required_kg_evidence_ids)
    has_lit = bool(requirements.required_literature_evidence_ids)
    route = question.expected_route
    if route is Route.KG and not has_kg:
        return _fail(ROUTE_PLAUSIBILITY, "expected_route=kg 与所需证据不一致")
    if route is Route.LITERATURE and not has_lit:
        return _fail(ROUTE_PLAUSIBILITY, "expected_route=literature 与所需证据不一致")
    if route in {Route.HYBRID, Route.REDUNDANT} and not (has_kg and has_lit):
        return _fail(ROUTE_PLAUSIBILITY, "expected_route 需要 KG 与文献两条来源")
    if route is Route.UNANSWERABLE and question.answerability is not Answerability.UNANSWERABLE:
        return _fail(ROUTE_PLAUSIBILITY, "expected_route=unanswerable 但问题标记为可回答")
    return _pass(
        ROUTE_PLAUSIBILITY,
        f"expected_route={route.value}，KG={has_kg}，文献={has_lit}",
    )


def validate_source_citability(context: ValidationContext) -> Outcome:
    """Required evidence must carry a stable, citable source identity."""
    requirements = context.question.evidence_requirements
    problems: list[str] = []
    for evidence_id in requirements.required_literature_evidence_ids:
        item = context.literature_evidence.get(evidence_id)
        if item is None:
            problems.append(f"{evidence_id} 不是文献证据")
            continue
        if not (item.pmid or item.doi):
            problems.append(f"{evidence_id} 缺少 PMID/DOI")
        if not item.license_note:
            problems.append(f"{evidence_id} 缺少许可说明")
        if not item.retrieval_query.strip():
            problems.append(f"{evidence_id} 缺少检索查询")
    for evidence_id in requirements.required_kg_evidence_ids:
        item = context.kg_evidence.get(evidence_id)
        if item is None:
            problems.append(f"{evidence_id} 不是 KG 证据")
            continue
        if not item.source_database.strip() or not item.source_record_id:
            problems.append(f"{evidence_id} 缺少稳定的来源数据库或记录标识")
    if problems:
        return _fail(SOURCE_CITABILITY, "；".join(problems))
    return _pass(SOURCE_CITABILITY, "所有必需证据均可引用")


VALIDATORS: dict[str, Callable[[ValidationContext], Outcome]] = {
    SCHEMA_REFERENCES: validate_schema_references,
    EVIDENCE_SUPPORT: validate_evidence_support,
    ANSWER_CONSISTENCY: validate_answer_consistency,
    EVIDENCE_SUFFICIENCY: validate_evidence_sufficiency,
    QUESTION_LEAKAGE: validate_question_leakage,
    DUPLICATE_DETECTION: validate_duplicate_detection,
    QUESTION_NATURALNESS: validate_question_naturalness,
    SEMANTIC_EQUIVALENCE: validate_semantic_equivalence,
    TAXONOMY_CONSISTENCY: validate_taxonomy_consistency,
    ROUTE_PLAUSIBILITY: validate_route_plausibility,
    SOURCE_CITABILITY: validate_source_citability,
    REVIEW_GROUP_INTEGRITY: validate_review_group_integrity,
}


def run_check(
    check_type: str,
    context: ValidationContext,
    *,
    validator_version: str = VALIDATOR_VERSION,
) -> ValidationCheck:
    """Run one validator and return a persisted check record."""
    try:
        outcome = VALIDATORS[check_type](context)
    except Exception as exc:  # a broken validator must fail loudly, not pass silently
        outcome = _fail(check_type, f"校验器异常: {type(exc).__name__}: {exc}")
    return ValidationCheck(
        check_id=new_id("check"),
        check_type=check_type,
        status=outcome.status,
        severity=outcome.severity_or_default(),
        message=outcome.message,
        validator_version=validator_version,
        artifact_ids=list(outcome.artifact_ids),
        # Applicability provenance. A result may only be reused for this
        # question version while the inputs it depended on are unchanged.
        question_id=context.question.question_id,
        evaluated_question_version=context.question.question_version,
        input_digest=question_dependency_digest(context.question),
        evaluated_at=utc_now(),
    )


def run_all_validators(
    question: QuestionSpec,
    bundles: Iterable[EvidenceBundle],
    *,
    peers: Iterable[QuestionSpec] = (),
    check_types: Iterable[str] = ALL_CHECK_TYPES,
    validator_version: str = VALIDATOR_VERSION,
) -> list[ValidationCheck]:
    """Run the full validator suite for one question."""
    context = ValidationContext(
        question=question,
        bundles=tuple(bundles),
        peers=tuple(peers),
    )
    return [
        run_check(check_type, context, validator_version=validator_version)
        for check_type in check_types
    ]


def summarize_checks(checks: list[ValidationCheck]) -> dict[str, int]:
    """Count check statuses for reports and the workbench."""
    summary = {status.value: 0 for status in ValidationStatus}
    for check in checks:
        summary[check.status.value] += 1
    return summary
