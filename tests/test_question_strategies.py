import pytest

from medical_qa_data_factory.generation.question_strategies import STRATEGIES, question_strategy
from medical_qa_data_factory.generation.difficulty_assessment import assess_difficulty
from medical_qa_data_factory.llm.fake import FakeStructuredModel
from medical_qa_data_factory.llm.objective_skills import load_objective_skill


@pytest.mark.parametrize("level", ["easy", "medium", "hard"])
def test_target_routes_retrieval_and_design_to_same_policy(level):
    policy = question_strategy({"difficulty_targets": [level]})
    assert policy.name == level
    assert policy.retrieval_goal and policy.design_steps and policy.missing_material_action
    _, _, instructions = load_objective_skill("objective_question_design", {"task": {
        "difficulty_targets": [level], "generation_strategy": policy.name,
    }})
    assert f"references/{level}.md" in instructions


def test_explicit_free_strategy_does_not_inherit_hard_composition_rules():
    free = question_strategy({"generation_strategy": "free", "difficulty_targets": ["hard"]})
    assert free == STRATEGIES["free"]
    assert free.design_steps != STRATEGIES["hard"].design_steps
    assert free.retrieval_goal != STRATEGIES["hard"].retrieval_goal
    assert question_strategy({"difficulty_targets": ["easy", "medium"]}).name == "free"


def test_independent_assessor_receives_no_requested_or_proposed_difficulty():
    model = FakeStructuredModel(responses={"objective_difficulty_assessment": {
        "assessed_difficulty": "easy", "shortest_solution_steps": ["Read one authoritative record"],
        "rationale": "One direct lookup is sufficient",
    }})
    result, usage = assess_difficulty(model, question="Where is the named gene located?", answer=["17"],
        anchors=[], planner_environment={}, operation_key="verified-question")
    request = model.calls_for("objective_difficulty_assessment")[0]
    assert not {"difficulty_targets", "generation_strategy", "proposed_difficulty"} & request.payload.keys()
    assert result.assessed_difficulty == "easy"
    assert usage["model_calls"] == 1


def test_all_four_policies_have_distinct_retrieval_and_composition_methods():
    assert len({p.retrieval_goal for p in STRATEGIES.values()}) == 4
    assert len({p.design_steps for p in STRATEGIES.values()}) == 4


def test_malformed_grade_is_a_recoverable_assessment_error():
    from medical_qa_data_factory.generation.difficulty_assessment import DifficultyAssessmentError
    model = FakeStructuredModel(responses={"objective_difficulty_assessment": {"assessed_difficulty": "hard"}})
    with pytest.raises(DifficultyAssessmentError, match="等待恢复"):
        assess_difficulty(model, question="Question", answer=["Answer"], anchors=[],
                          planner_environment={}, operation_key="bad-grade")
