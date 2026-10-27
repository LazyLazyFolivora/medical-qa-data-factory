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
        "population": "adults carrying a BRCA1 pathogenic variant",
        "exposure_context": "drug A exposure",
        "dose_or_regimen": "aspirin 81 mg daily",
        "exposure_duration": "at least two years",
        "study_type": "long-term follow-up",
        "sample_type": None,
        "disease_category": "digestive system cancer",
        "temporal_scope": None,
        "additional_qualifiers": {},
        "cardinality": "exactly_one",
        "relation_strength": "risk",
        "answer_disclosed": False,
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
                    "对于携带 BRCA1 致病性变异并持续接受每日 81 mg 阿司匹林的成年人"
                    "长期随访研究，"
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

    assert variant.question_text.startswith("对于携带 BRCA1")
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
        "对于携带 BRCA1 致病性变异并持续接受每日 81 mg 阿司匹林的成年人长期随访研究，"
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
        "在长期接受 Drug A 的成年人中，哪种消化系统癌症风险降低？",
        validated,
    )

    assert report.passed is False
    assert "qualifier_mismatch" in report.reason_codes


def test_semantic_verifier_requires_the_public_entity_to_be_recovered() -> None:
    validated = _validated()
    model = FakeStructuredModel(
        responses={
            "answer_verifier": _semantic_view(chemical_name=None)
        }
    )

    report = PromptQuestionSemanticVerifier(model).verify(
        "哪种消化系统癌症的发生风险被报告降低？",
        validated,
    )

    assert report.passed is False
    assert "entity_mismatch" in report.reason_codes


def test_semantic_verifier_rejects_question_strength_upgrade() -> None:
    validated = _validated()
    model = FakeStructuredModel(
        responses={
            "answer_verifier": _semantic_view(relation_strength="efficacy")
        }
    )

    report = PromptQuestionSemanticVerifier(model).verify(
        "在长期随访中，Drug A 是否能够有效治疗哪种消化系统癌症？",
        validated,
    )

    assert report.passed is False
    assert "claim_strength_exaggerated" in report.reason_codes


def test_semantic_verifier_can_reject_disclosed_answer_synonyms() -> None:
    validated = _validated()
    model = FakeStructuredModel(
        responses={
            "answer_verifier": _semantic_view(answer_disclosed=True)
        }
    )

    report = PromptQuestionSemanticVerifier(model).verify(
        "Drug A 是否降低了某消化系统肿瘤的风险？",
        validated,
    )

    assert report.passed is False
    assert "answer_leakage" in report.reason_codes


def test_deterministic_gate_rejects_uninstantiated_placeholder() -> None:
    validated = _validated()
    report = PromptQuestionSemanticVerifier(
        FakeStructuredModel(responses={"answer_verifier": _semantic_view()})
    ).verify(
        "药物 A 与药物 B 是否在某疾病中相关？",
        validated,
    )

    assert report.passed is False
    assert "uninstantiated_placeholder" in report.reason_codes


def test_deterministic_gate_rejects_database_metadata_question() -> None:
    validated = _validated()
    report = PromptQuestionSemanticVerifier(
        FakeStructuredModel(responses={"answer_verifier": _semantic_view()})
    ).verify(
        "在数据库记录中，Drug A 对应哪种消化系统癌症？",
        validated,
    )

    assert report.passed is False
    assert "database_metadata_question" in report.reason_codes
