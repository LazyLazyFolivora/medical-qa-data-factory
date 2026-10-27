from medical_qa_data_factory.generation.prompt_writing import (
    PromptQuestionSemanticVerifier,
    PromptQuestionWriter,
)
from medical_qa_data_factory.generation.task_contracts import (
    promote_validated_task,
)
from medical_qa_data_factory.llm import FakeStructuredModel

from test_ctd_task_contracts import _report, _spec


def _validated():
    spec = _spec()
    return promote_validated_task(spec, _report(spec))


def _semantic_view(**updates):
    payload = {
        "chemical_name": "Drug A",
        "relation_intent": "risk_decrease",
        "polarity": "decrease",
        "population": "adults carrying genetic feature S",
        "exposure_context": "drug A exposure",
        "dose_or_regimen": "dose D",
        "exposure_duration": "at least two years",
        "study_type": "long-term follow-up",
        "sample_type": None,
        "disease_category": "digestive system cancer",
        "temporal_scope": None,
        "additional_qualifiers": {},
        "cardinality": "exactly_one",
        "uncertain": False,
    }
    payload.update(updates)
    return payload


def test_prompt_writer_generates_a_natural_variant_without_program_template() -> None:
    validated = _validated()
    model = FakeStructuredModel(
        responses={
            "question_writer": {
                "question_text": (
                    "对于携带遗传特征 S 并持续接受剂量 D 药物 A "
                    "至少两年的成年人长期随访研究，"
                    "哪种消化系统癌症的发生风险被报告降低？"
                ),
                "language": "zh-CN",
            }
        }
    )

    variant = PromptQuestionWriter(model).write(
        validated,
        qid="question-prompted",
    )

    assert variant.question_text.startswith("对于携带遗传特征 S")
    payload = model.payloads_for("question_writer")[0]
    assert payload["task"]["public_study_context"]["population"]
    assert any(
        "state the answer" in item for item in payload["constraints"]
    )


def test_semantic_verifier_accepts_equivalent_rewording() -> None:
    validated = _validated()
    model = FakeStructuredModel(
        responses={"answer_verifier": _semantic_view()}
    )

    report = PromptQuestionSemanticVerifier(model).verify(
        "对于携带遗传特征 S 并持续接受剂量 D 药物 A 至少两年的成年人长期随访研究，"
        "哪种消化系统癌症的发生风险被报告降低？",
        validated,
    )

    assert report.passed is True
    assert report.reconstructed_query_digest == (
        validated.task.query.dependency_digest
    )


def test_semantic_verifier_rejects_changed_polarity() -> None:
    validated = _validated()
    model = FakeStructuredModel(
        responses={
            "answer_verifier": _semantic_view(
                polarity="increase",
                relation_intent="risk_increase",
            )
        }
    )

    report = PromptQuestionSemanticVerifier(model).verify(
        "哪种消化系统癌症的发生风险被报告升高？",
        validated,
    )

    assert report.passed is False
    assert "intent_polarity_mismatch" in report.reason_codes


def test_deterministic_leakage_gate_rejects_answer_alias() -> None:
    validated = _validated()
    model = FakeStructuredModel(
        responses={"answer_verifier": _semantic_view()}
    )

    report = PromptQuestionSemanticVerifier(model).verify(
        "Answer disease 是否是与药物 A 相关且风险降低的疾病？",
        validated,
    )

    assert report.passed is False
    assert "answer_leakage" in report.reason_codes
    assert model.invocations == []


def test_semantic_verifier_rejects_missing_public_qualifier() -> None:
    validated = _validated()
    model = FakeStructuredModel(
        responses={
            "answer_verifier": _semantic_view(population=None)
        }
    )

    report = PromptQuestionSemanticVerifier(model).verify(
        "在长期接受药物 A 的成年人中，哪种消化系统癌症风险降低？",
        validated,
    )

    assert report.passed is False
    assert "qualifier_mismatch" in report.reason_codes
