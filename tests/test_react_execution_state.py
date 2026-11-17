"""Execution facts override notebook interpretations; missing evidence is repairable."""
from test_react_main_agent import make_loop, scripted_choice
from medical_qa_data_factory.generation.react_execution_state import verification_readiness, execution_state


def test_missing_evidence_feedback_guides_retrieval_then_allows_verification(tmp_path):
    def choose(request):
        p = request.payload
        n = len(p["prior_searches"])
        if n == 3:
            hid = next(iter(p["hypotheses"]))
            ready = p["verification_readiness"][hid]
            assert not ready["entry_ready"]
            assert ready["repair"]["tool"] == "request_more_evidence"
            assert "proposed_answer_values" not in ready["repair"]
            return {"action": "verify_answer", "rationale": "Exercise prerequisite feedback",
                    "parameters": {"hypothesis_id": hid}}
        if n == 4:
            hid = next(iter(p["hypotheses"]))
            actual = p["execution_state"]["last_completed_step"]
            assert actual["attempted_tool_executed"] is False
            assert actual["feedback"]["remediation"]["repair"]["hypothesis_id"] == hid
            assert "NOT EXECUTED" in actual["feedback"]["error"]
            return {"action": "request_more_evidence", "rationale": "Supply missing evidence",
                    "parameters": {"hypothesis_id": hid, "provider_id": "pubmed",
                                   "query": {"term": "BRCA1 location"}, "evidence_need": "Answer public question"}}
        if n == 5:
            hid = next(iter(p["hypotheses"]))
            assert p["verification_readiness"][hid]["entry_ready"]
            assert p["verification_readiness"][hid]["eligible_provider_result_ids"]
            return {"action": "verify_answer", "rationale": "Prerequisite repaired", "parameters": {"hypothesis_id": hid}}
        return scripted_choice(request)
    loop, model = make_loop(tmp_path, choose, max_actions=6)
    result = loop.run()
    assert loop.runtime.ordered_decisions()[-1].action == "verify_answer"
    assert any(a.artifact_type == "react_publication_proposal" for a in result.runtime.artifacts.values())


def test_execution_projection_ignores_false_model_progress(tmp_path):
    def choose(request):
        return {"action": "verify_answer", "rationale": "Bad call", "research_note": "Verification succeeded"}
    loop, _ = make_loop(tmp_path, choose, max_actions=1)
    snapshot = loop.run().runtime
    steps = [a.payload for a in snapshot.artifacts.values() if a.artifact_type == "react_step"]
    actual = execution_state(snapshot, steps)
    assert actual["last_completed_step"]["attempted_tool_executed"] is False
    assert actual["last_completed_step"]["output_counts"] == {"react_observation": 1}
    assert "Verification succeeded" not in str(actual)
