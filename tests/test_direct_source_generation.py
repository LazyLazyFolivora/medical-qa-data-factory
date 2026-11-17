"""Direct sources traverse the production loop without extraction or fake retrieval."""
from test_react_main_agent import make_loop
from test_objective_agent_loop import _hypothesis


def choose_direct(request):
    p = request.payload
    done = [s["action"] for s in p["prior_searches"]]
    if not done:
        return {"action": "retrieve_materials", "rationale": "Get original", "parameters": {
            "provider_id": "primekg", "query": {"mention": "BRCA1"}, "evidence_need": "Read source"}}
    if not p["hypotheses"]:
        return {"action": "design_questions", "rationale": "Source already sufficient", "parameters": {
            "evidence_sufficiency_reason": "material.primekg directly states BRCA1 chromosome 17; this supports a location question without requiring mechanism evidence.",
            "source_sections": {p["materials_without_extracted_claims"][0]: None}}}
    hid = next(iter(p["hypotheses"]))
    action = ("review_candidate" if "review_candidate" not in done else
              "verify_answer" if "verify_answer" not in done else
              "publish_question" if "publish_question" not in done else "stop")
    return {"action": action, "rationale": "Read and assess source", "parameters": {"hypothesis_id": hid}}


def configure_direct(model):
    def design(request):
        assert request.payload["candidate_claims"] == []
        assert "chromosome 17" in request.payload["evidence_sufficiency_reason"]
        return {"hypotheses": [_hypothesis().model_copy(update={
            "evidence_basis": "source_passages", "candidate_claim_ids": [],
            "discovery_anchor_ids": [a["anchor_id"] for a in request.payload["evidence_excerpts"]]
        }).model_dump(mode="json")]}

    def verify(request):
        p = request.payload
        if p["stage"] == "final_blind":
            assert "proposed_answer_values" not in p
            assert set(p["blind_answer_query"]) == {"query_id", "question"}
            assert p["independent_evidence"]
            assert "NOT independent sources" in p["evidence_scope"]
            return {"query_id": p["blind_answer_query"]["query_id"], "answer_values": ["chromosome 17"], "uncertain": False}
        assert p["verification_plan"]["expected_attempt_ids"] == []
        assert p["attempt_summaries"] == []
        return dict(question_is_self_contained=True, answer_matches_blind=True, answer_scope_complete=True,
                    explanation="Original source supports this answer; not independent corroboration",
                    conflict_found=False, support_directly_entails_answer=True,
                    directly_supporting_anchor_ids=[a["anchor_id"] for a in p["evidence_anchors"]])
    model.set_response("objective_question_design", design)
    model.set_response("objective_answer_verification", verify)


def test_direct_source_end_to_end_and_restored_snapshot(tmp_path):
    loop, model = make_loop(tmp_path, choose_direct, max_actions=4)
    configure_direct(model)
    first = loop.run()
    assert not first.published_questions
    assert not model.calls_for("fact_understanding")
    assert any(a.artifact_type == "candidate_usability_review" for a in first.runtime.artifacts.values())
    loop.runtime.resume_from_recovery()
    result = loop.run()
    actions = [d.action for d in loop.runtime.ordered_decisions()]
    assert actions == ["retrieve_materials", "design_questions", "review_candidate", "verify_answer", "publish_question", "stop"]
    assert len(result.published_questions) == 1
    resolutions = [a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "answer_resolution"]
    assert resolutions[0]["verification_policy_version"] == "existing-source-review-v1"
    assert len([a for a in result.runtime.artifacts.values() if a.artifact_type == "provider_result"]) == 1
    notes = [a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "question_design_notes"]
    assert any("chromosome 17" in n.get("evidence_sufficiency_reason", "") for n in notes)


def test_unknown_source_does_not_create_a_question(tmp_path):
    loop, model = make_loop(tmp_path, lambda r: {"action": "design_questions", "rationale": "Invalid source",
        "parameters": {"evidence_sufficiency_reason": "Claimed source supports location.", "source_sections": {"invented": None}}}, max_actions=1)
    result = loop.run()
    assert not model.calls_for("objective_question_design")
    assert not result.runtime.hypotheses


def test_specific_repair_reaches_designer_and_revision_is_verified_afresh(tmp_path):
    def choose(request):
        p = request.payload
        done = [s["action"] for s in p["prior_searches"]]
        if done.count("verify_answer") == 1 and done.count("design_questions") == 1:
            return {"action": "design_questions", "rationale": "Fix unsupported wording", "parameters": {
                "evidence_sufficiency_reason": "Original location evidence supports a narrower answer after removing unsupported wording.",
                "revision_of": next(iter(p["hypotheses"])), "source_sections": {"material.primekg": None}}}
        if done.count("design_questions") == 2:
            hid = next(h for h in p["hypotheses"] if h != original_ids[0])
            action = ("review_candidate" if done.count("review_candidate") < 2 else
                      "verify_answer" if done.count("verify_answer") < 2 else
                      "publish_question" if "publish_question" not in done else "stop")
            return {"action": action, "rationale": "Verify new version", "parameters": {"hypothesis_id": hid}}
        return choose_direct(request)

    original_ids = []
    loop, model = make_loop(tmp_path, choose, max_actions=12)
    configure_direct(model)

    def design(request):
        p = request.payload
        revision = p.get("revision_context")
        if revision:
            previous = revision["previous_candidate"]
            original_ids.append(previous["hypothesis_id"])
            gap = next(f for f in revision["verification_feedback"] if "repair_issues" in f)
            assert gap["evidence_need"] is None
            assert gap["repair_issues"][0]["statement"] == "unsupported mechanism"
        return {"hypotheses": [_hypothesis().model_copy(update={
            "question_intent": "Which chromosome contains the human BRCA1 gene?" if revision else _hypothesis().question_intent,
            "evidence_basis": "source_passages", "candidate_claim_ids": [],
            "discovery_anchor_ids": [a["anchor_id"] for a in p["evidence_excerpts"]],
        }).model_dump(mode="json")]}

    reviews = []

    def verify(request):
        p = request.payload
        if p["stage"] == "final_blind":
            return {"query_id": p["blind_answer_query"]["query_id"], "answer_values": ["chromosome 17"]}
        reviews.append(p)
        return dict(question_is_self_contained=True, answer_matches_blind=True, answer_scope_complete=True,
                    explanation="Check every statement", conflict_found=False, support_directly_entails_answer=True,
                    directly_supporting_anchor_ids=[a["anchor_id"] for a in p["evidence_anchors"]],
                    recommended_action="revise" if len(reviews) == 1 else "accept",
                    repair_issues=[dict(target="blind_answer", statement="unsupported mechanism",
                        problem="Source reports location only", anchor_ids=[p["evidence_anchors"][0]["anchor_id"]],
                        suggested_change="Remove mechanism")] if len(reviews) == 1 else [])

    model.set_response("objective_question_design", design)
    model.set_response("objective_answer_verification", verify)
    result = loop.run()
    assert len(reviews) == 2
    assert len(result.published_questions) == 1
    assert len(result.runtime.hypotheses) == 2
    assert any(a.artifact_type == "question_design_notes" and a.payload.get("revision_of") == original_ids[0]
               for a in result.runtime.artifacts.values())
    assert not any(a.artifact_type == "react_publication_proposal" and a.payload["hypothesis_id"] == original_ids[0]
                   for a in result.runtime.artifacts.values())


def test_design_combines_more_than_one_reading_page_without_truncation(tmp_path):
    from test_objective_agent_loop import FakeMaterialProvider
    from medical_qa_data_factory.generation.source_question_inputs import design_source_anchors
    from medical_qa_data_factory.generation.research_documents import index_plain_text
    raw = "\n\n".join(f"Result {i}: original observation." for i in range(7))
    loop, _ = make_loop(tmp_path, choose_direct, max_actions=1)
    loop.providers["primekg"] = FakeMaterialProvider("primekg", raw)
    snapshot = loop.run().runtime
    ids = [p["paragraph_id"] for p in index_plain_text(raw)["paragraph_index"]]
    anchors = design_source_anchors(loop, snapshot, {"material.primekg": ids})
    assert len(anchors) == 7
    for anchor in anchors:
        assert raw[anchor.start_offset:anchor.end_offset] == anchor.quoted_text
