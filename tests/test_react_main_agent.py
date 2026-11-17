"""ReAct contract tests: the model owns order; tool facts gate publication."""
from medical_qa_data_factory.generation.agent_loop import ObjectiveQuestionAgentLoop
from medical_qa_data_factory.generation.react_main_agent import ReactMainAgent, REACT_WORKFLOW
from medical_qa_data_factory.generation.objective_question_runtime import ObjectiveRuntimeRepository, RuntimeBudget
from medical_qa_data_factory.generation.objective_question_persistence import ObjectiveQuestionPersistence
from test_objective_agent_loop import _manifest, _goal, _model, FakeMaterialProvider


def configure_react_model(model):
    model.set_response("material_retrieval", lambda r: {
        "sufficient": bool(r.payload["candidates"]), "rationale": "Relevant fixture evidence",
        "selected_material_ids": [c["material_id"] for c in r.payload["candidates"]], "follow_up_queries": []})
    model.set_response("objective_candidate_review", lambda r: {"reviews": [
        {"hypothesis_id": h["hypothesis_id"], "disposition": "KEEP", "rationale": "Useful bounded question"}
        for h in r.payload["hypotheses"]]})
    model.set_response("objective_react_agent", scripted_choice)
    return model


def scripted_choice(request):
    p = request.payload
    steps = p["prior_searches"]
    done = [s["action"] for s in steps]
    def choice(action, parameters=None, inputs=None):
        return {"action": action, "parameters": parameters or {}, "input_artifact_ids": inputs or [],
                "rationale": "Fixture model chose this action", "research_note": "Inspect then verify"}
    if not steps:
        return choice("retrieve_materials", {"provider_id": "primekg", "query": {"mention": "BRCA1"}, "evidence_need": "Discover location"})
    if p["materials_without_extracted_claims"]:
        return choice("understand_facts", inputs=p["materials_without_extracted_claims"])
    if not p["hypotheses"]:
        return choice("design_questions", {"evidence_sufficiency_reason": "Selected claims directly state BRCA1 chromosome location; a location question needs no mechanism evidence.", "claim_ids": [a["id"] for a in p["facts"] if a["type"] == "candidate_claim"]})
    hid, h = next(iter(p["hypotheses"].items()))
    if "review_candidate" not in done:
        return choice("review_candidate", {"hypothesis_id": hid})
    if "request_more_evidence" not in done:
        return choice("request_more_evidence", {"hypothesis_id": hid, "provider_id": "pubmed",
            "query": {"term": "BRCA1 chromosome location"}, "evidence_need": "Independently answer public question"})
    if "verify_answer" not in done:
        return choice("verify_answer", {"hypothesis_id": hid})
    if h["state"] == "RELEASE_REVIEWED":
        return choice("publish_question", {"hypothesis_id": hid})
    return choice("stop")


def make_loop(tmp_path, choice=None, max_actions=30):
    model = configure_react_model(_model())
    if choice:
        model.set_response("objective_react_agent", choice)
    runtime = ObjectiveRuntimeRepository.create(tmp_path, manifest=_manifest().model_copy(update={"orchestrator_version": REACT_WORKFLOW}),
        goal=_goal(), budget=RuntimeBudget(max_cost=10, max_model_calls=100))
    loop = ObjectiveQuestionAgentLoop(runtime=runtime, persistence=ObjectiveQuestionPersistence(tmp_path),
        goal=_goal(), main_agent=ReactMainAgent(model, provider_ids=["primekg", "pubmed"], memory_root=tmp_path), model=model,
        providers={"primekg": FakeMaterialProvider("primekg", "BRCA1 is located on human chromosome 17."),
                   "pubmed": FakeMaterialProvider("pubmed", "Independent report: BRCA1 maps to chromosome 17.")}, max_actions=max_actions)
    return loop, model


def test_each_step_is_chosen_by_model_and_publication_is_explicit(tmp_path):
    loop, model = make_loop(tmp_path)
    result = loop.run()
    actions = [d.action for d in loop.runtime.ordered_decisions()]
    assert actions == ["retrieve_materials", "understand_facts", "design_questions", "review_candidate",
                       "request_more_evidence", "verify_answer", "publish_question", "stop"]
    assert len(model.calls_for("objective_react_agent")) == len(actions)
    assert not model.calls_for("objective_workflow_planner")
    assert len(result.published_questions) == 1


def test_model_can_continue_searching_with_unprocessed_materials_and_choose_related_entity(tmp_path):
    def choose(r):
        n = len(r.payload["prior_searches"])
        if n < 2:
            return {"action": "retrieve_materials", "rationale": "Need another relation before design", "parameters": {
                "provider_id": "pubmed", "query": {"term": "TFAM"}, "target_entity": "TFAM", "evidence_need": "Study related entity"}}
        return {"action": "stop", "rationale": "No further useful leads in this fixture"}
    loop, model = make_loop(tmp_path, choose)
    result = loop.run()
    assert [d.action for d in loop.runtime.ordered_decisions()] == ["retrieve_materials", "retrieve_materials", "stop"]
    assert not model.calls_for("material_retrieval")
    searches = [a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "provider_result"]
    assert all(a["request"]["query"] == {"term": "TFAM"} for a in searches)
    assert not result.published_questions


def test_verification_does_not_publish_and_resume_requires_explicit_publish(tmp_path):
    loop, model = make_loop(tmp_path, max_actions=6)
    first = loop.run()
    assert not first.published_questions
    assert not any(a.artifact_type == "objective_validation_report" for a in first.runtime.artifacts.values())
    assert any(a.artifact_type == "react_publication_proposal" for a in first.runtime.artifacts.values())
    loop.runtime.resume_from_recovery()
    resumed = loop.run()
    assert len(resumed.published_questions) == 1


def test_invalid_publish_returns_error_without_selecting_another_business_action(tmp_path):
    loop, model = make_loop(tmp_path, lambda r: {"action": "publish_question", "rationale": "Bad request", "parameters": {"hypothesis_id": "missing"}}, max_actions=1)
    result = loop.run()
    assert [d.action for d in loop.runtime.ordered_decisions()] == ["tool_feedback"]
    assert any(a.artifact_type == "react_observation" and "hypothesis" in a.payload["error"] for a in result.runtime.artifacts.values())


def test_pubmed_text_query_reaches_provider_without_changing_search(tmp_path):
    term = 'BRCA1 AND "DNA repair"[Title/Abstract]'
    loop, _ = make_loop(tmp_path, lambda r: {"action": "retrieve_materials", "rationale": "Follow graph lead",
        "parameters": {"provider_id": "pubmed", "query": term, "evidence_need": "Find mechanism"}}, max_actions=1)
    result = loop.run()
    searches = [a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "provider_result"]
    assert len(searches) == 1
    assert searches[0]["request"]["query"] == {"term": term}


def test_unknown_candidate_feedback_preserves_search_for_model_repair(tmp_path):
    def choose(request):
        if not request.payload["prior_searches"]:
            return {"action": "request_more_evidence", "rationale": "Investigate lead", "parameters": {
                "hypothesis_id": "invented", "provider_id": "pubmed", "query": "BRCA1 repair",
                "evidence_need": "Find mechanism"}}
        return {"action": "retrieve_materials", "rationale": "No candidate yet; investigate lead", "parameters": {
            "provider_id": "pubmed", "query": {"term": "BRCA1 repair"}, "evidence_need": "Find mechanism"}}
    loop, _ = make_loop(tmp_path, choose, max_actions=2)
    result = loop.run()
    feedback = next(a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "react_observation")
    assert "retrieve_materials" in feedback["error"]
    assert feedback["attempted_tool_call"]["parameters"]["query"] == "BRCA1 repair"
    assert [d.action for d in loop.runtime.ordered_decisions()] == ["tool_feedback", "retrieve_materials"]


def test_empty_pubmed_query_is_feedback_not_a_provider_call(tmp_path):
    loop, _ = make_loop(tmp_path, lambda r: {"action": "retrieve_materials", "rationale": "Search",
        "parameters": {"provider_id": "pubmed", "query": "  ", "evidence_need": "Find mechanism"}}, max_actions=1)
    result = loop.run()
    assert not any(a.artifact_type == "provider_result" for a in result.runtime.artifacts.values())
    assert any(a.artifact_type == "react_observation" and "PubMed query requires" in a.payload["error"]
               for a in result.runtime.artifacts.values())


def test_material_claim_dependency_is_visible_and_model_can_repair(tmp_path):
    def choose(request):
        p = request.payload
        step = len(p["prior_searches"])
        if step == 0:
            return scripted_choice(request)
        material_ids = [a["id"] for a in p["artifact_index"] if a["type"] == "evidence_material"]
        assert material_ids
        assert all(a["type"] == "candidate_claim" for a in p["facts"])
        if step == 1:
            assert p["facts"] == []
            assert p["design_inputs"]["claim_count"] == 0
        if step in (1, 3):
            return {"action": "design_questions", "rationale": "Attempt with material", "parameters": {"evidence_sufficiency_reason": "Selected material states location directly.", "claim_ids": material_ids}}
        if step == 2:
            assert "no candidate_claim facts exist" in str(p["latest_observations"])
            assert "understand_facts" in str(p["latest_observations"])
            return {"action": "understand_facts", "rationale": "Create missing facts", "input_artifact_ids": material_ids}
        assert "evidence_material" in str(p["latest_observations"])
        assert p["design_inputs"]["available_claim_ids"] == [a["id"] for a in p["facts"]]
        return {"action": "design_questions", "rationale": "Use persisted claims", "parameters": {
            "evidence_sufficiency_reason": "Selected persisted claims directly support the location answer.",
            "claim_ids": p["design_inputs"]["available_claim_ids"]}}
    loop, model = make_loop(tmp_path, choose, max_actions=5)
    result = loop.run()
    assert [d.action for d in loop.runtime.ordered_decisions()] == [
        "retrieve_materials", "tool_feedback", "understand_facts", "tool_feedback", "design_questions"]
    assert len(model.calls_for("objective_question_design")) == 1
    assert any(a.artifact_type == "question_hypothesis" for a in result.runtime.artifacts.values())


def test_search_returns_unreviewed_hits_without_hidden_selection_model(tmp_path):
    loop, model = make_loop(tmp_path, max_actions=1)
    def fail(r):
        raise RuntimeError("provider selection unavailable")
    model.set_response("material_retrieval", fail)
    result = loop.run()
    assert not model.calls_for("material_retrieval")
    results = [a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "provider_result"]
    assert results[0]["usage"]["provider_calls"] == 1
    assert results[0]["usage"]["selection_status"] == "unreviewed"
    assert results[0]["usage"]["selected_material_ids"] == []
    assert results[0]["material_ids"]
    assert not any(a.artifact_type == "candidate_claim" for a in result.runtime.artifacts.values())


def test_quality_reject_cannot_be_published_automatically(tmp_path):
    loop, model = make_loop(tmp_path, max_actions=7)
    model.set_response("objective_candidate_review", lambda r: {"reviews": [
        {"hypothesis_id": h["hypothesis_id"], "disposition": "REJECT", "rationale": "Question has no meaningful scope"}
        for h in r.payload["hypotheses"]]})
    result = loop.run()
    assert not result.published_questions
    assert loop.runtime.ordered_decisions()[-1].action == "tool_feedback"
    assert any(a.artifact_type == "react_observation" and "KEEP" in a.payload.get("error", "")
               for a in result.runtime.artifacts.values())


def test_read_material_returns_source_and_is_observed_by_next_model_call(tmp_path):
    def choose(r):
        if not r.payload["prior_searches"]:
            return scripted_choice(r)
        if len(r.payload["prior_searches"]) == 1:
            return {"action": "read_artifacts", "rationale": "Inspect source",
                    "input_artifact_ids": r.payload["materials_without_extracted_claims"], "parameters": {"limit": 100}}
        return {"action": "stop", "rationale": "Read test completed"}
    loop, model = make_loop(tmp_path, choose)
    result = loop.run()
    observations = [a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "react_observation"]
    assert observations[0]["source_views"][0]["content"] == "BRCA1 is located on human chromosome 17."
    assert observations[0]["source_views"][0]["content_complete"] is True
    last = model.calls_for("objective_react_agent")[-1].payload
    assert any(o["type"] == "react_observation" for o in last["latest_observations"])
    assert last["provider_query_schemas"]["pubmed"] == {"term": "PubMed query string"}


def test_three_verification_gaps_return_to_model_without_closing_candidate(tmp_path):
    def choose(r):
        if sum(s["action"] == "verify_answer" for s in r.payload["prior_searches"]) < 3:
            selected = scripted_choice(r)
            if selected["action"] == "stop":
                return {"action": "verify_answer", "rationale": "Inspect unresolved gap again",
                        "parameters": {"hypothesis_id": next(iter(r.payload["hypotheses"]))}}
            return selected
        return {"action": "retrieve_materials", "rationale": "Explore another direction despite active candidate",
                "parameters": {"provider_id": "pubmed", "query": {"term": "TFAM partners"}, "evidence_need": "New relationship", "target_entity": "TFAM"}}
    loop, model = make_loop(tmp_path, choose, max_actions=9)
    def verify(r):
        if r.payload["stage"] == "final_blind":
            return {"query_id": r.payload["blind_answer_query"]["query_id"], "answer_values": ["chromosome 17"], "uncertain": False}
        return dict(question_is_self_contained=True, answer_matches_blind=True, answer_scope_complete=True,
                    explanation="Missing direct location evidence", conflict_found=False,
                    directly_supporting_anchor_ids=[], support_directly_entails_answer=False, missing_evidence_kind="direct_relation")
    model.set_response("objective_answer_verification", verify)
    result = loop.run()
    assert sum(a.artifact_type == "verification_gap" for a in result.runtime.artifacts.values()) == 3
    assert all(s.value not in {"REJECTED", "INDETERMINATE"} for s in result.runtime.hypotheses.values())
    assert loop.runtime.ordered_decisions()[-1].action == "retrieve_materials"
    assert not result.published_questions


def test_design_keeps_llm_choices_with_same_answer_and_claims(tmp_path):
    from test_objective_agent_loop import _hypothesis
    loop, model = make_loop(tmp_path, max_actions=3)
    def design(r):
        original = _hypothesis(claim_id=r.payload["candidate_claims"][0]["claim_id"])
        revised = original.model_copy(update={"question_intent": "Which chromosome contains the human BRCA1 gene?"})
        return {"hypotheses": [original.model_dump(mode="json"), revised.model_dump(mode="json")]}
    model.set_response("objective_question_design", design)
    result = loop.run()
    candidates = [a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "question_hypothesis"]
    assert len(candidates) == 2  # Semantic duplication is for the LLM reviewer, not claim overlap heuristics.
    assert candidates[0]["proposed_answer_values"] == candidates[1]["proposed_answer_values"]
    assert len(model.calls_for("objective_question_design")) == 1
    assert model.calls_for("objective_question_design")[0].payload["question_portfolio"]["preferred_claim_ids"] == []


def test_nested_query_returns_feedback_without_calling_provider(tmp_path):
    loop, model = make_loop(tmp_path, lambda r: {"action": "retrieve_materials", "rationale": "Search",
        "parameters": {"provider_id": "pubmed", "query": {"term": {"term": "BRCA1"}}, "evidence_need": "Identity"}}, max_actions=1)
    result = loop.run()
    assert loop.runtime.ordered_decisions()[0].action == "tool_feedback"
    assert not any(a.artifact_type == "provider_result" for a in result.runtime.artifacts.values())


def test_full_text_fetch_requires_explicit_model_choice(tmp_path):
    for enabled in (False, True):
        loop, model = make_loop(tmp_path / str(enabled), lambda r: {
            "action": "retrieve_materials", "rationale": "Search literature", "parameters": {
                "provider_id": "pubmed", "query": {"term": "BRCA1"}, "evidence_need": "Location",
                "fetch_full_text": enabled}}, max_actions=1)
        provider = loop.providers["pubmed"]
        calls = []
        def details(request, *, material_ids):
            calls.append(material_ids)
            return provider.retrieve(request)
        provider.retrieve_details = details
        result = loop.run()
        assert bool(calls) == enabled
        assert not any(a.artifact_type == "subagent_failure" for a in result.runtime.artifacts.values())


def test_latest_search_shows_new_evidence_not_stale_research_note(tmp_path):
    def choose(r):
        n = len(r.payload["prior_searches"])
        if n < 2:
            provider = "primekg" if n == 0 else "pubmed"
            return {"action": "retrieve_materials", "rationale": "Explore", "research_note": "So far only anatomy",
                    "parameters": {"provider_id": provider, "query": {"term": "BRCA1"}, "evidence_need": "Relationships"}}
        return {"action": "stop", "rationale": "Fixture ends; host must not force an action"}
    loop, model = make_loop(tmp_path, choose)
    loop.providers["primekg"].raw_text = "BRCA1 is present in kidney."
    loop.providers["pubmed"].raw_text = "BRCA1 --disease_protein--> Fanconi anemia"
    loop.run()
    p = model.calls_for("objective_react_agent")[-1].payload
    latest = p["latest_observations"][0]
    assert latest["type"] == "provider_result"
    assert latest["payload"]["returned_material_count"] == 1
    assert "Fanconi anemia" in latest["payload"]["materials"][0]["preview"]
    assert "content_hash" not in latest["payload"]["materials"][0]["preview"]
    assert p["research_note"] == "So far only anatomy"
    assert "stale" in p["note_status"]
    assert loop.runtime.ordered_decisions()[-1].action == "stop"


def test_candidate_observation_contains_actual_question_and_answer(tmp_path):
    loop, model = make_loop(tmp_path, max_actions=4)
    loop.run()
    p = model.calls_for("objective_react_agent")[-1].payload
    candidates = [o["payload"] for o in p["latest_observations"] if o["type"] == "question_hypothesis"]
    assert candidates
    assert candidates[0]["question_intent"] == next(iter(p["hypotheses"].values()))["question"]
    assert candidates[0]["proposed_answer_values"]


def test_search_preview_reports_omissions_without_ranking_or_hiding_count(tmp_path):
    from medical_qa_data_factory.generation.react_observations import ObservationView
    loop, model = make_loop(tmp_path, max_actions=1)
    result = loop.run()
    source = next(a for a in result.runtime.artifacts.values() if a.artifact_type == "provider_result")
    material_id = source.payload["material_ids"][0]
    source = source.model_copy(update={"payload": {**source.payload, "material_ids": [material_id] * 50}})
    projection = ObservationView(result.runtime).content(source)
    assert projection["returned_material_count"] == 50
    assert len(projection["materials"]) == 12
    assert projection["omitted_material_count"] == 38
