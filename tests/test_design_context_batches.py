import pytest
from medical_qa_data_factory.generation.question_design import _generate_design_batch, QuestionHypothesisBatch
from medical_qa_data_factory.llm.contracts import StructuredGenerationRequest, StructuredGenerationResult
from medical_qa_data_factory.llm.errors import ModelError, ModelErrorCode


def test_large_batch_is_split_with_whole_evidence_and_unique_requests():
    requests = []
    class Model:
        def generate_structured(self, request, response_model):
            requests.append(request)
            if len(request.payload["candidate_claims"]) > 1:
                raise ModelError(ModelErrorCode.BAD_REQUEST, "context budget exceeded", retryable=False)
            claim = request.payload["candidate_claims"][0]
            assert request.payload["evidence_excerpts"] == [{"anchor_id": claim["supporting_anchor_ids"][0], "quoted_text": "complete condition " * 1000}]
            assert request.payload["claim_evidence_cards"][0]["claim_id"] == claim["claim_id"]
            return StructuredGenerationResult(invocation_id=request.invocation_id, role=request.role,
                parsed={"hypotheses": [], "insufficiency_reason": "Not enough for a useful question"},
                usage={"input_tokens": 100, "output_tokens": 10}, model_config_id="test")
    payload = {"task": {}, "candidate_claims": [{"claim_id": str(i), "supporting_anchor_ids": [f"a{i}"]} for i in range(4)],
               "claim_evidence_cards": [{"claim_id": str(i), "source_anchor_ids": [f"a{i}"]} for i in range(4)],
               "evidence_excerpts": [{"anchor_id": f"a{i}", "quoted_text": "complete condition " * 1000} for i in range(4)]}
    request = StructuredGenerationRequest(invocation_id="design", role="objective_question_design", system_prompt_version="v1",
        payload=payload, idempotency_key="design")
    result = _generate_design_batch(Model(), request, QuestionHypothesisBatch)
    assert result.usage["design_batches"] == 4
    assert result.usage["input_tokens"] == 400
    assert len({r.idempotency_key for r in requests}) == len(requests)
    assert len(payload["candidate_claims"]) == 4


def test_non_context_error_is_not_retried_as_smaller_batches():
    class Model:
        def generate_structured(self, *args):
            raise ModelError(ModelErrorCode.AUTH_ERROR, "unauthorized")
    request = StructuredGenerationRequest(invocation_id="design", role="objective_question_design", system_prompt_version="v1",
        payload={"task": {}, "candidate_claims": [{}, {}]}, idempotency_key="design")
    with pytest.raises(ModelError, match="unauthorized"):
        _generate_design_batch(Model(), request, QuestionHypothesisBatch)
