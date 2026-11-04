import pytest
from medical_qa_data_factory.generation.candidate_review import review_candidates
from medical_qa_data_factory.generation.objective_question_contracts import QuestionHypothesis, AnswerCardinality
from medical_qa_data_factory.generation.subagent_contracts import RoleContractViolation
from medical_qa_data_factory.llm.fake import FakeStructuredModel
from test_objective_subagent_roles import _claim, _anchor


def hypothesis():
    return QuestionHypothesis(hypothesis_id="candidate", candidate_claim_ids=[_claim().claim_id],
        question_intent="BRCA1 位于哪里？", proposed_answer_type="chromosome",
        proposed_answer_values=["17"], proposed_cardinality=AnswerCardinality.EXACTLY_ONE,
        proposed_public_scope="human", discovery_anchor_ids=[_anchor().anchor_id])


def test_rewrite_is_rechecked_with_original_evidence_and_answer():
    def respond(request):
        if request.payload["stage"] == "initial":
            return {"reviews": [{"hypothesis_id": "candidate", "disposition": "REWRITE",
                "rationale": "Specify species and location type", "revised_question": "人类 BRCA1 位于哪条染色体？",
                "revised_scope": "human chromosome"}]}
        assert request.payload["hypotheses"][0]["question_intent"] == "人类 BRCA1 位于哪条染色体？"
        assert request.payload["hypotheses"][0]["proposed_answer_values"] == ["17"]
        assert request.payload["evidence_excerpts"][0]["quoted_text"] == _anchor().quoted_text
        return {"reviews": [{"hypothesis_id": "candidate", "disposition": "KEEP", "rationale": "Bounded simple lookup"}]}
    model = FakeStructuredModel(responses={"objective_candidate_review": respond})
    questions, reviews, history = review_candidates(model, [hypothesis()], [_claim()], [_anchor()], operation_id="test")
    assert reviews[0].disposition == "KEEP"
    assert len(history) == 2
    assert len(model.invocations) == 2
    assert "TTC33" in model.invocations[0].skill_instruction


def test_reject_is_terminal_for_review_and_not_rewritten():
    model = FakeStructuredModel(responses={"objective_candidate_review": {"reviews": [
        {"hypothesis_id": "candidate", "disposition": "REJECT", "rationale": "No bounded information need"}]}})
    _, reviews, _ = review_candidates(model, [hypothesis()], [_claim()], [_anchor()], operation_id="test")
    assert reviews[0].disposition == "REJECT"
    assert len(model.invocations) == 1


def test_repeated_rewrite_becomes_reject():
    model = FakeStructuredModel(responses={"objective_candidate_review": {"reviews": [
        {"hypothesis_id": "candidate", "disposition": "REWRITE", "rationale": "Still ambiguous",
         "revised_question": "哪里？", "revised_scope": "human"}]}})
    _, reviews, _ = review_candidates(model, [hypothesis()], [_claim()], [_anchor()], operation_id="test")
    assert reviews[0].disposition == "REJECT"
    assert len(model.invocations) == 2


@pytest.mark.parametrize("reviews", [[], [{"hypothesis_id": "candidate", "disposition": "NEEDS_EVIDENCE", "rationale": "missing"}]])
def test_missing_decision_or_vague_evidence_request_fails_closed(reviews):
    model = FakeStructuredModel(responses={"objective_candidate_review": {"reviews": reviews}})
    with pytest.raises(RoleContractViolation):
        review_candidates(model, [hypothesis()], [_claim()], [_anchor()], operation_id="test")
