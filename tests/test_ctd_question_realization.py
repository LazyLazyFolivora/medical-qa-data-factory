from __future__ import annotations

from pathlib import Path

import pytest

from medical_qa_data_factory.generation.task_candidates import (
    CtdStudyContextCandidateSource,
)
from medical_qa_data_factory.generation.task_compiler import TaskCompiler
from medical_qa_data_factory.generation.task_realization import (
    CtdTaskQuestionWriter,
    CtdTaskTextSemanticVerifier,
)
from medical_qa_data_factory.generation.task_service import (
    CtdTaskGenerationService,
)
from medical_qa_data_factory.generation.task_validation import CtdTaskValidator

from test_ctd_candidate_source import (
    SEED_ID,
    _ContextAssessor,
    _context,
    _environment,
    _evaluator,
    _index,
)


def _published(tmp_path: Path, **context_updates):
    index = _index(tmp_path)
    evaluator = _evaluator(
        index,
        _ContextAssessor(
            lambda _record, context, answer_id: (
                "matched"
                if context.population == "adults"
                and answer_id.endswith(":MESH:A")
                else "matched"
                if context.population != "adults"
                else "not_matched"
            )
        ),
    )
    service = CtdTaskGenerationService(
        candidate_source=CtdStudyContextCandidateSource(evaluator),
        compiler=TaskCompiler(),
        validator=CtdTaskValidator(evaluator),
        evidence_by_record={},
    )
    result = service.generate(
        seed_entity_id=SEED_ID,
        chemical_id="D001241",
        snapshot_id=index.metadata()["snapshot_id"],
        context=_context(**context_updates),
        environment=_environment(index),
    )
    assert result.published
    return result.published[0]


@pytest.mark.parametrize(
    ("intent", "polarity"),
    [
        ("association_research", "unknown"),
        ("biomarker_research", "neutral"),
        ("therapeutic_research", "neutral"),
        ("risk_increase", "increase"),
        ("risk_decrease", "decrease"),
        ("safety", "unknown"),
    ],
)
def test_writer_and_verifier_support_every_allowed_intent(
    tmp_path: Path,
    intent,
    polarity,
) -> None:
    published = _published(
        tmp_path,
        relation_intent=intent,
        polarity=polarity,
    )
    writer = CtdTaskQuestionWriter()
    verifier = CtdTaskTextSemanticVerifier()
    variant = writer.write(
        published.validated_task,
        qid="question-intent",
    )

    report = verifier.verify(variant, published.validated_task)

    assert report.passed is True
    assert report.reconstructed_query_digest == (
        published.validated_task.task.query.dependency_digest
    )
    for value in (
        "adults",
        "aspirin exposure",
        "low dose",
        "at least two years",
        "observational cohort",
        "serum",
        "digestive cancer",
        "long-term follow-up",
    ):
        assert value in variant.question_text


def test_verifier_rejects_changed_risk_direction(tmp_path: Path) -> None:
    published = _published(tmp_path)
    variant = CtdTaskQuestionWriter().write(
        published.validated_task,
        qid="question-risk",
    ).model_copy(
        update={
            "question_text": published.question_variant.question_text.replace(
                "降低",
                "升高",
            )
        }
    )

    report = CtdTaskTextSemanticVerifier().verify(
        variant,
        published.validated_task,
    )

    assert report.passed is False
    assert "question_semantics_mismatch" in report.reason_codes


def test_verifier_rejects_missing_public_disambiguator(
    tmp_path: Path,
) -> None:
    published = _published(tmp_path)
    text = published.question_variant.question_text.replace(
        "adults、",
        "",
        1,
    )
    variant = published.question_variant.model_copy(
        update={"question_text": text}
    )

    report = CtdTaskTextSemanticVerifier().verify(
        variant,
        published.validated_task,
    )

    assert report.passed is False
    assert "missing_public_disambiguator" in report.reason_codes


def test_verifier_rejects_uninstantiated_placeholders(
    tmp_path: Path,
) -> None:
    published = _published(tmp_path)
    variant = published.question_variant.model_copy(
        update={
            "question_text": published.question_variant.question_text.replace(
                "digestive cancer",
                "某疾病",
            )
        }
    )

    report = CtdTaskTextSemanticVerifier().verify(
        variant,
        published.validated_task,
    )

    assert report.passed is False
    assert "uninstantiated_placeholder" in report.reason_codes


def test_association_cannot_be_rewritten_as_causality(
    tmp_path: Path,
) -> None:
    published = _published(
        tmp_path,
        relation_intent="association_research",
        polarity="unknown",
    )
    variant = published.question_variant.model_copy(
        update={
            "question_text": published.question_variant.question_text.replace(
                "存在关联",
                "导致",
            )
        }
    )

    report = CtdTaskTextSemanticVerifier().verify(
        variant,
        published.validated_task,
    )

    assert report.passed is False
    assert "question_semantics_mismatch" in report.reason_codes


def test_therapeutic_research_cannot_become_demonstrated_efficacy(
    tmp_path: Path,
) -> None:
    published = _published(
        tmp_path,
        relation_intent="therapeutic_research",
        polarity="neutral",
    )
    variant = published.question_variant.model_copy(
        update={
            "question_text": published.question_variant.question_text.replace(
                "被研究用于改善",
                "能够有效治疗",
            )
        }
    )

    report = CtdTaskTextSemanticVerifier().verify(
        variant,
        published.validated_task,
    )

    assert report.passed is False
    assert "claim_semantics_mismatch" in report.reason_codes


def test_answer_alias_and_internal_id_leak_are_rejected(
    tmp_path: Path,
) -> None:
    published = _published(tmp_path)
    leaked = published.question_variant.model_copy(
        update={
            "question_text": (
                published.question_variant.question_text
                + " Cancer A"
            )
        }
    )
    internal = published.question_variant.model_copy(
        update={
            "question_text": (
                published.question_variant.question_text
                + " evidence-catalog:1"
            )
        }
    )

    leaked_report = CtdTaskTextSemanticVerifier().verify(
        leaked,
        published.validated_task,
    )
    internal_report = CtdTaskTextSemanticVerifier().verify(
        internal,
        published.validated_task,
    )

    assert "answer_leakage" in leaked_report.reason_codes
    assert (
        "database_trivia_or_internal_term"
        in internal_report.reason_codes
    )
