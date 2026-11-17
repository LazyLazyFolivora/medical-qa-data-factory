"""Exercise dependency continuity through the real controller and persistence."""
from test_react_main_agent import make_loop
from test_direct_source_generation import choose_direct, configure_direct
from test_objective_agent_loop import FakeMaterialProvider


def test_research_only_design_review_and_verification_survive_resume(tmp_path):
    def choose(request):
        p = request.payload
        done = [s["action"] for s in p["prior_searches"]]
        if not done:
            return choose_direct(request)
        if "synthesize_evidence" not in done:
            return {"action": "synthesize_evidence", "rationale": "Bound the source",
                    "input_artifact_ids": ["material.primekg"],
                    "parameters": {"research_question": "Where is human BRCA1 located?"}}
        if not p["hypotheses"]:
            return {"action": "design_questions", "rationale": "Use this research",
                    "parameters": {"research_id": p["design_inputs"]["research_briefs"][0]["research_id"],
                                   "evidence_sufficiency_reason": "Source directly states chromosome 17."}}
        return choose_direct(request)

    loop, model = make_loop(tmp_path, choose, max_actions=3)
    configure_direct(model)
    model.set_response("objective_evidence_synthesis", lambda r: {
        "sources": [{"material_id": "material.primekg", "information_value": "Location",
                     "limitations": "Human only", "further_reading_needed": False, "missing_detail": ""}],
        "question_focus": "Human chromosome location", "answer_boundaries": ["Human only"],
        "remaining_gaps": ["No mechanism evidence"], "ready_for_design": True})
    first = loop.run()
    assert len(first.runtime.hypotheses) == 1
    assert not model.calls_for("fact_understanding")
    hid = next(iter(first.runtime.hypotheses))
    context = first.runtime.artifacts[f"candidate_context.{hid}"].payload["research_context"]
    assert context["answer_boundaries"] == ["Human only"]
    assert context["remaining_gaps"] == ["No mechanism evidence"]
    assert model.calls_for("objective_question_design")[0].payload["research_context"] == context
    # Direct and synthesis routes must persist identical content for an identical anchor ID.
    from medical_qa_data_factory.generation.source_question_inputs import design_source_anchors, research_source_anchors
    direct = design_source_anchors(loop, first.runtime, {"material.primekg": None})
    research = research_source_anchors(loop, first.runtime, context)
    assert direct == research
    for anchor in direct:
        loop.persistence.save_anchor(anchor)
    loop.runtime.resume_from_recovery()
    loop.max_actions = 5
    result = loop.run()
    assert len(result.published_questions) == 1
    assert model.calls_for("objective_candidate_review")[0].payload["research_context"] == context
    verification = model.calls_for("objective_answer_verification")
    blind = next(r for r in verification if r.payload["stage"] == "final_blind")
    assert "research_context" not in str(blind.payload)
    review = next(r for r in verification if r.payload["stage"] == "final_review")
    assert review.payload["design_requirements"]["research_context"] == context


def test_index_only_synthesis_recovers_by_selecting_original_paragraph(tmp_path):
    def choose(request):
        p = request.payload
        done = [s["action"] for s in p["prior_searches"]]
        if not done:
            return choose_direct(request)
        if done.count("synthesize_evidence") == 0:
            return {"action": "synthesize_evidence", "rationale": "Try synthesis",
                    "input_artifact_ids": ["material.primekg"], "parameters": {"research_question": "Location?"}}
        if "read_artifacts" not in done:
            return {"action": "read_artifacts", "rationale": "Locate source paragraphs",
                    "input_artifact_ids": ["material.primekg"], "parameters": {}}
        view = p["latest_observations"][0]["payload"]["source_views"][0]
        pid = next(row["paragraph_id"] for row in view["paragraph_index"] if row["section"] == "Results")
        return {"action": "synthesize_evidence", "rationale": "Use original results",
                "input_artifact_ids": ["material.primekg"], "parameters": {
                    "research_question": "Location?", "source_sections": {"material.primekg": [pid]}}}

    loop, model = make_loop(tmp_path, choose, max_actions=4)
    loop.providers["primekg"] = FakeMaterialProvider(
        "primekg", "[Introduction]\n" + "X" * 13000 + "\n\n[Results]\nBRCA1 is on chromosome 17.")
    model.set_response("objective_evidence_synthesis", lambda r: {"sources": [{
        "material_id": "material.primekg", "information_value": "Location", "limitations": "Human",
        "further_reading_needed": False, "missing_detail": ""}]})
    result = loop.run()
    assert len(model.calls_for("objective_evidence_synthesis")) == 1
    assert any("only an index" in str(a.payload) for a in result.runtime.artifacts.values()
               if a.artifact_type == "subagent_failure")
    assert "chromosome 17" in model.calls_for("objective_evidence_synthesis")[0].payload["materials"][0]["content"]
