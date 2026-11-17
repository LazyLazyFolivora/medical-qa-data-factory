import json

import pytest
from pydantic import ValidationError

from types import SimpleNamespace

from medical_qa_data_factory.generation.react_main_agent import ReactChoice, _question_already_has_candidate
from medical_qa_data_factory.llm.contracts import StructuredGenerationRequest
from medical_qa_data_factory.llm.providers.openai_compatible import OpenAICompatibleModel
from test_llm_openai_adapter import _config, _envelope


def request():
    return StructuredGenerationRequest(role="objective_react_agent", system_prompt_version="objective-react-v1",
        invocation_id="contract-test", idempotency_key="contract-test", payload={"stage": "exploration"})


@pytest.mark.parametrize("parameters", [None, {}, {"provider_id": "pubmed"},
    {"provider_id": "pubmed", "query": {"term": "EGFR"}}, {"searches": []}])
def test_missing_search_arguments_fail_before_execution(parameters):
    response = {"action": "retrieve_materials", "rationale": "Search EGFR"}
    if parameters is not None:
        response["parameters"] = parameters
    with pytest.raises(ValidationError):
        ReactChoice.model_validate(response)


def test_action_branches_require_parameters_and_keep_rationale():
    schema = ReactChoice.model_json_schema()
    branches = {b["properties"]["action"]["const"]: b for b in schema["anyOf"]}
    assert all({"action", "rationale", "parameters"} <= set(b["required"]) for b in branches.values())
    read_ids = branches["read_artifacts"]["properties"]["input_artifact_ids"]
    assert read_ids["maxItems"] == 8
    assert "uniqueItems" not in read_ids  # Current self-hosted grammar rejects this keyword.
    assert set(schema["$defs"]["Search"]["required"]) == {"provider_id", "query", "evidence_need"}
    batch = {"searches": [{"provider_id": "pubmed", "query": {"term": "EGFR"}, "evidence_need": "Mechanism"}]}
    choice = ReactChoice.model_validate({"action": "retrieve_materials", "rationale": "Search", "parameters": batch})
    assert choice.parameters == batch  # No defaults or query content silently invented.


def test_missing_parameters_are_repaired_inside_model_call(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "test")
    malformed = {"action": "retrieve_materials", "rationale": "Search EGFR"}
    valid = {**malformed, "parameters": {"provider_id": "pubmed", "query": {"term": "EGFR"},
                                        "evidence_need": "Find evidence"}}
    bodies = []
    def transport(url, headers, body, timeout):
        bodies.append(body)
        return 200, {}, json.dumps(_envelope(json.dumps(malformed if len(bodies) == 1 else valid)))
    model = OpenAICompatibleModel(config=_config("http://unused", structured_output_mode="json_object"), transport=transport)
    result = model.generate_structured(request(), ReactChoice)
    assert result.parsed["parameters"] == valid["parameters"]
    assert len(bodies) == 2 and model.calls[-1].repaired
    assert "Validation error to correct" in bodies[1]["messages"][0]["content"]
    assert "provider_id" in bodies[1]["messages"][0]["content"]


def test_strict_role_override_keeps_other_roles_and_fallback_schema():
    model = OpenAICompatibleModel(config=_config("http://unused", structured_output_mode="json_object",
        structured_output_modes_by_role={"objective_react_agent": "json_schema"}))
    schema = ReactChoice.model_json_schema()
    strict = model._build_body(request(), schema)
    assert strict["response_format"]["type"] == "json_schema"
    assert "json_schema" not in json.loads(strict["messages"][1]["content"])
    fallback = model._build_body(request(), schema, structured_output_mode="json_object")
    assert json.loads(fallback["messages"][1]["content"])["json_schema"] == schema
    other = request().model_copy(update={"role": "objective_history_summary"})
    assert model._build_body(other, schema)["response_format"]["type"] == "json_object"


@pytest.mark.parametrize("selection", [{"source_sections": {"material.a": ["p0"]}}, {"claim_ids": ["claim.a"]}])
def test_design_requires_explicit_evidence_readiness(selection):
    choice = {"action": "design_questions", "rationale": "Enough evidence", "parameters": selection}
    with pytest.raises(ValidationError, match="evidence_sufficiency_reason"):
        ReactChoice.model_validate(choice)
    reason = "Source A p0 names both classes and examples; a classification question needs no clinical efficacy comparison."
    accepted = ReactChoice.model_validate({**choice, "parameters": {**selection, "evidence_sufficiency_reason": reason}})
    assert accepted.parameters["evidence_sufficiency_reason"] == reason
    schema = ReactChoice.model_json_schema()
    for name in ("SourceDesign", "ClaimDesign"):
        assert "evidence_sufficiency_reason" in schema["$defs"][name]["required"]


def test_same_approved_question_cannot_create_another_independent_candidate():
    artifacts = [
        SimpleNamespace(artifact_type="question_hypothesis", payload={
            "question_intent": " Why is EGFR mutation testing important? "}),
        SimpleNamespace(artifact_type="react_observation", payload={
            "question_intent": "Unrelated observation"}),
    ]

    assert _question_already_has_candidate(artifacts, "why is egfr mutation testing important?")
    assert not _question_already_has_candidate(artifacts, "Which mutations confer resistance?")


def test_stop_cannot_claim_exhaustion_with_an_unused_approved_question():
    from medical_qa_data_factory.generation.question_reader import QUESTION_REVIEW_VERSION
    from medical_qa_data_factory.generation.react_main_agent import validate_react_decision
    from medical_qa_data_factory.generation.subagent_contracts import RoleContractViolation

    opportunity = SimpleNamespace(
        artifact_id="opportunity.a", artifact_type="react_observation", parent_decision_id="decision.a",
        payload={"status": "model_assessment_not_verified_fact", "question_review": "ACCEPT",
                 "question_review_version": QUESTION_REVIEW_VERSION,
                 "approved_question": "What outcome did Trial A report?",
                 "assessment": {"ready_for_design": True}, "source_passages": [{"text": "Evidence"}]},
    )
    snapshot = SimpleNamespace(
        goal=SimpleNamespace(difficulty_targets=["easy"], question_count_upper_bound=1),
        hypotheses={}, artifacts={opportunity.artifact_id: opportunity}, input_artifact_ids=[],
    )
    decision = SimpleNamespace(
        action="stop", action_parameters={"stop_basis": "no_viable_directions"}, input_artifact_ids=[],
    )
    with pytest.raises(RoleContractViolation, match="opportunity.a"):
        validate_react_decision(snapshot, decision)
    decision.action_parameters["stop_basis"] = "resource_limit"
    validate_react_decision(snapshot, decision)
    decision.action_parameters["stop_basis"] = "no_viable_directions"
    opportunity.payload["question_review_version"] = "stale"
    validate_react_decision(snapshot, decision)


def test_source_review_does_not_discard_a_publicly_identified_trial_question():
    from medical_qa_data_factory.generation.question_reader import review_questions

    class Model:
        def generate_structured(self, request, schema):
            if request.role == "objective_question_form_reader":
                assert "materials" not in request.payload
                return SimpleNamespace(parsed={"judgments": [
                    {"index": 0, "task_in_question": "Find Trial A's outcome.", "missing_required_context": [],
                     "task_kind": "source_specific_reading", "verdict": "ACCEPT", "reason": "Trial named."},
                    {"index": 1, "task_in_question": "Find an unnamed disease's incidence.",
                     "missing_required_context": ["disease"], "task_kind": "incomplete_question",
                     "verdict": "REVISE", "reason": "Disease missing."},
                ]}, usage={})
            assert [q["index"] for q in request.payload["questions"]] == [0]
            return SimpleNamespace(parsed={"judgments": [
                {"index": 0, "premises": [], "verdict": "RESELECT", "reason": "Report outcome unavailable."},
            ]}, usage={})

    result, audit, _ = review_questions(
        Model(), questions=[{"index": 0, "question": "What outcome did Trial A report?"},
                            {"index": 1, "question": "Does incidence differ by sex?"}],
        materials=[], goal={}, operation_id="light-screen",
    )
    assert [judgment.verdict for judgment in result.judgments] == ["ACCEPT", "REVISE"]
    assert audit["premise_review"]["judgments"][0]["verdict"] == "RESELECT"
