"""Behavioral tests for hierarchical generation, including local loops."""
from medical_qa_data_factory.generation.agent_loop import ObjectiveQuestionAgentLoop
from medical_qa_data_factory.generation.workflow_scheduler import FINAL_QUESTION_WORKFLOW, HierarchicalMainAgent
from medical_qa_data_factory.generation.objective_question_runtime import ObjectiveRuntimeRepository, RuntimeBudget
from medical_qa_data_factory.generation.objective_question_persistence import ObjectiveQuestionPersistence
from medical_qa_data_factory.generation.objective_question_contracts import ObjectiveVerdict, HypothesisState
from test_objective_agent_loop import _manifest, _goal, _model, FakeMaterialProvider


def make_loop(tmp_path, model=None, count=1, difficulties=None):
    model = model or _model()
    goal = _goal().model_copy(update={"question_count_upper_bound": count})
    if difficulties:
        goal = goal.model_copy(update={"difficulty_targets": difficulties})
    runtime = ObjectiveRuntimeRepository.create(tmp_path, manifest=_manifest(), goal=goal,
        budget=RuntimeBudget(max_cost=10, max_model_calls=100))
    loop = ObjectiveQuestionAgentLoop(runtime=runtime, persistence=ObjectiveQuestionPersistence(tmp_path),
        goal=goal, main_agent=HierarchicalMainAgent(model, provider_ids=["primekg", "pubmed"]), model=model,
        providers={"primekg": FakeMaterialProvider("primekg", "BRCA1 is located on human chromosome 17."),
                   "pubmed": FakeMaterialProvider("pubmed", "Independent report: BRCA1 maps to chromosome 17.")})
    return loop, model


def actions(loop):
    return [d.action for d in loop.runtime.ordered_decisions()]


def test_medium_quota_uses_audited_difficulty(tmp_path):
    from medical_qa_data_factory.generation.workflow_scheduler import target_question_count
    for difficulty, expected in (("easy", 0), ("medium", 1)):
        loop, model = make_loop(tmp_path / difficulty, difficulties=["medium"])
        original = model.responses["objective_release_review"]
        def review(request):
            response = original(request)
            if request.payload["stage"] == "answer_evidence_audit":
                return response.model_copy(update={"assessed_difficulty": difficulty})
            return response
        model.set_response("objective_release_review", review)
        result = loop.run()
        assert len(result.published_questions) == 1
        assert result.published_questions[0].validation_report.assessed_difficulty == difficulty
        assert target_question_count(result.runtime) == expected
        assert (result.stop_reason == "target_reached") == bool(expected)
        if difficulty == "easy":
            last = model.calls_for("objective_workflow_planner")[-1].payload
            assert last["published_question_count"] == 1 and last["counted_toward_target"] == 0


def test_ready_stages_advance_without_main_agent_choice(tmp_path):
    loop, model = make_loop(tmp_path)
    result = loop.run()
    assert len(result.published_questions) == 1
    assert actions(loop) == ["retrieve_materials", "understand_facts", "design_questions",
                             "request_more_evidence", "verify_answer", "realize_question", "review_release", "stop"]
    assert [r.payload["stage"] for r in model.calls_for("objective_workflow_planner")] == ["exploration", "verification_gap"]
    assert not model.calls_for("objective_main_agent")
    assert len(model.calls_for("fact_understanding")) == 1


def test_hard_quota_only_counts_audited_hard_questions(tmp_path):
    from medical_qa_data_factory.generation.workflow_scheduler import target_question_count
    for difficulty in ("easy", "medium", "hard"):
        loop, model = make_loop(tmp_path / difficulty, difficulties=["hard"])
        original = model.responses["objective_release_review"]

        def review(request):
            response = original(request)
            if request.payload["stage"] == "answer_evidence_audit":
                return response.model_copy(update={"assessed_difficulty": difficulty})
            return response

        model.set_response("objective_release_review", review)
        result = loop.run()
        assert len(result.published_questions) == 1
        assert target_question_count(result.runtime) == int(difficulty == "hard")
        assert (result.stop_reason == "target_reached") == (difficulty == "hard")


def test_exploration_can_stop_without_filling_target(tmp_path):
    loop, model = make_loop(tmp_path)
    model.set_response("objective_workflow_planner", {"action": "stop", "rationale": "No useful supported direction"})
    result = loop.run()
    assert result.run_state.value == "COMPLETED_WITH_ZERO_QUESTIONS"
    assert actions(loop) == ["stop"]


def test_partial_v2_run_explores_remaining_directions_before_accepting_stop(tmp_path):
    loop, model = make_loop(tmp_path, count=3)
    snapshot = loop.runtime.load()
    snapshot = snapshot.model_copy(update={
        "manifest": snapshot.manifest.model_copy(update={"orchestrator_version": FINAL_QUESTION_WORKFLOW}),
        "hypotheses": {"first": HypothesisState.PUBLISHED, "second": HypothesisState.PUBLISHED},
    })
    model.set_response("objective_workflow_planner", {"action": "stop", "rationale": "Candidates complete"})
    routes = []
    while True:
        decision = loop.main_agent.decide(snapshot)
        if decision.action == "stop":
            break
        assert decision.action == "retrieve_materials"
        route = (decision.action_parameters["provider_id"], decision.action_parameters["query"])
        assert route not in routes
        routes.append(route)
        snapshot = snapshot.model_copy(update={"decisions": {**snapshot.decisions, decision.decision_id: decision}})
    assert {provider for provider, _ in routes} == {"primekg", "pubmed"}
    assert decision.stop_reason == "exploration_alternatives_exhausted"


def test_partial_output_does_not_force_repeated_design(tmp_path):
    loop, model = make_loop(tmp_path, count=2)
    original = model.responses["objective_workflow_planner"]
    model.set_response("objective_workflow_planner", lambda r:
        {"action": "stop", "rationale": "No useful remaining direction"} if r.payload.get("published_question_count") else original(r))
    result = loop.run()
    assert result.run_state.value == "COMPLETED"
    assert len(result.published_questions) == 1
    assert actions(loop).count("design_questions") == 1
    assert result.stop_reason == "No useful remaining direction"


def test_duplicate_after_standard_routes_redirects_without_spending_search_round(tmp_path):
    loop, model = make_loop(tmp_path, count=3)
    planner = loop.main_agent
    snapshot = loop.runtime.load()
    history = []
    while (route := planner._untried_direction(snapshot, history)) is not None:
        history.append(route)
        snapshot = snapshot.model_copy(update={"decisions": {**snapshot.decisions, route.decision_id: route}})
    repeated = history[-1].action_parameters
    def search(request):
        query = {"term": "BRCA1 neighboring genes"} if "redirect_instruction" in request.payload else repeated["query"]
        provider = "pubmed" if "redirect_instruction" in request.payload else repeated["provider_id"]
        return {"action": "retrieve_materials", "provider_id": provider, "query": query,
                "evidence_need": "Find a genomic neighbor", "rationale": "Explore genomic context"}
    model.set_response("objective_workflow_planner", search)
    decision = planner.decide(snapshot)
    assert decision.action == "retrieve_materials"
    assert decision.action_parameters["query"] == {"term": "BRCA1 neighboring genes"}
    first, redirected = model.calls_for("objective_workflow_planner")
    assert first.idempotency_key != redirected.idempotency_key
    assert first.payload["remaining_search_rounds"] == redirected.payload["remaining_search_rounds"]


def test_missing_evidence_retrieval_returns_to_same_verification(tmp_path):
    loop, model = make_loop(tmp_path)
    original = model.responses["objective_answer_verification"]
    blind_calls = 0
    def verify(request):
        nonlocal blind_calls
        value = original(request)
        if request.payload["stage"] == "blind_solve":
            blind_calls += 1
            if blind_calls == 1:
                loop.main_agent.provider_ids.append("ncbi_gene")
                return value.model_copy(update={"uncertain": True, "evidence_need": "Need an independent source establishing the human chromosome location"})
        return value
    model.set_response("objective_answer_verification", verify)
    original_search = model.responses["objective_workflow_planner"]
    loop.providers["ncbi_gene"] = FakeMaterialProvider("ncbi_gene", "New independent location evidence for BRCA1.")
    def search(request):
        value = original_search(request)
        if request.payload["stage"] == "verification_gap" and request.payload["prior_searches"]:
            value["provider_id"] = "ncbi_gene"
            value["query"] = {"symbol": "BRCA1"}
            assert request.payload["evidence_gap"]["reason"]
        return value
    model.set_response("objective_workflow_planner", search)
    result = loop.run()
    assert len(result.published_questions) == 1
    assert actions(loop)[3:7] == ["request_more_evidence", "verify_answer", "request_more_evidence", "verify_answer"]
    gaps = [a for a in result.runtime.artifacts.values() if a.artifact_type == "verification_gap"]
    assert len(gaps) == 1
    assert len(model.calls_for("fact_understanding")) == 1


def test_release_support_gap_searches_blind_and_reaudits_same_draft(tmp_path):
    loop, model = make_loop(tmp_path)
    original = model.responses["objective_release_review"]
    packages = []
    def audit(request):
        value = original(request)
        if request.payload["stage"] == "answer_evidence_audit":
            packages.append(request.payload)
            if len(packages) == 1:
                loop.providers["ensembl"] = FakeMaterialProvider("ensembl", "Independent locus record for BRCA1.")
                loop.main_agent.provider_ids.append("ensembl")
                return value.model_copy(update={"verdict": ObjectiveVerdict.REJECT,
                    "primary_reason": "INSUFFICIENT_VALIDATION_EVIDENCE",
                    "evidence_need": "Need a source linking BRCA1 directly to chromosome 17"})
        return value
    model.set_response("objective_release_review", audit)
    result = loop.run()
    assert len(result.published_questions) == 1
    assert len(packages) == 2 and packages[0]["public_question"] == packages[1]["public_question"]
    assert any(a["anchor_id"] == "anchor.ensembl" for a in packages[1]["independent_evidence"]["anchors"])
    search = next(d for d in loop.runtime.ordered_decisions() if d.action_parameters.get("repair_draft_id"))
    assert search.action_parameters["query"] == {"symbol": "BRCA1"}
    assert search.action_parameters["provider_id"] == "ensembl"
    blind = [r.payload for r in model.calls_for("objective_workflow_planner")
             if r.payload["stage"] == "verification_gap"][-1]
    assert "chromosome 17" not in str(blind).lower()
    assert actions(loop).count("realize_question") == 1


def test_release_gap_without_new_document_stays_unpublished(tmp_path):
    loop, model = make_loop(tmp_path)
    original = model.responses["objective_release_review"]
    def audit(request):
        value = original(request)
        return value.model_copy(update={"verdict": ObjectiveVerdict.REJECT,
            "primary_reason": "UNSUPPORTED_ANSWER"}) if request.payload["stage"] == "answer_evidence_audit" else value
    model.set_response("objective_release_review", audit)
    old = model.responses["objective_workflow_planner"]
    def plan(request):
        value = old(request)
        if (request.payload.get("evidence_gap") or {}).get("kind") == "release_evidence_gap":
            value["query"] = {"term": "BRCA1 gene cytogenetic band"}
        if request.payload["stage"] == "exploration" and request.payload["prior_searches"]:
            return {"action": "stop", "rationale": "No useful direction"}
        return value
    model.set_response("objective_workflow_planner", plan)
    # Limit this run to the candidate loop (subsequent exploration is tested separately).
    loop.max_actions = 9
    result = loop.run()
    assert not result.published_questions
    assert any(a.payload.get("reason") == "no_new_independent_release_evidence" for a in result.runtime.artifacts.values())


def test_public_wording_failure_gets_one_rewrite_then_review(tmp_path):
    loop, model = make_loop(tmp_path)
    original = model.responses["objective_release_review"]
    public_calls = 0
    def review(request):
        nonlocal public_calls
        value = original(request)
        if request.payload["stage"] == "public_question_audit":
            public_calls += 1
            if public_calls == 1:
                return value.model_copy(update={"self_contained": False, "verdict": ObjectiveVerdict.REJECT,
                                                "primary_reason": "MISSING_PUBLIC_CONDITION"})
        return value
    model.set_response("objective_release_review", review)
    result = loop.run()
    assert len(result.published_questions) == 1
    assert actions(loop)[5:] == ["realize_question", "review_release", "realize_question", "review_release", "stop"]
    assert result.published_questions[0].draft.draft_id.endswith(".revision1")
    assert model.calls_for("objective_question_realization")[-1].payload["design_requirements"]["repair_feedback"]


def test_pause_resume_keeps_candidate_evidence_rounds(tmp_path):
    loop, model = make_loop(tmp_path)
    original = model.responses["objective_workflow_planner"]
    def search(request):
        choice = original(request)
        if request.payload["stage"] == "verification_gap":
            loop.runtime.request_pause()
        return choice
    model.set_response("objective_workflow_planner", search)
    result = loop.run()
    assert result.stop_reason == "user_paused"
    loop.runtime.resume_from_recovery()
    model.set_response("objective_workflow_planner", original)
    result = loop.run()
    assert len(result.published_questions) == 1
    assert actions(loop).count("request_more_evidence") == 1


def test_unchanged_evidence_closes_candidate_instead_of_reverifying(tmp_path):
    loop, model = make_loop(tmp_path)
    original = model.responses["objective_answer_verification"]
    def verify(request):
        value = original(request)
        return value.model_copy(update={"uncertain": True, "evidence_need": "Need independently corroborated location"}) if request.payload["stage"] == "blind_solve" else value
    model.set_response("objective_answer_verification", verify)
    original_search = model.responses["objective_workflow_planner"]
    def search(request):
        value = original_search(request)
        if request.payload["stage"] == "verification_gap" and request.payload["prior_searches"]:
            value["query"] = {"term": "another location query"}
        return value
    model.set_response("objective_workflow_planner", search)
    result = loop.run()
    assert actions(loop).count("verify_answer") == 1
    assert actions(loop).count("close_candidate") == 1
    assert any(a.payload.get("reason") == "no_new_candidate_evidence" for a in result.runtime.artifacts.values())
    assert result.run_state.value == "COMPLETED_WITH_ZERO_QUESTIONS"


def test_second_wording_failure_rejects_without_new_exploration_loop(tmp_path):
    loop, model = make_loop(tmp_path)
    original = model.responses["objective_release_review"]
    def review(request):
        value = original(request)
        return value.model_copy(update={"self_contained": False, "verdict": ObjectiveVerdict.REJECT,
                                        "primary_reason": "MISSING_PUBLIC_CONDITION"}) if request.payload["stage"] == "public_question_audit" else value
    model.set_response("objective_release_review", review)
    result = loop.run()
    assert actions(loop).count("realize_question") == 2
    assert actions(loop).count("review_release") == 2
    assert all(s is HypothesisState.REJECTED for s in result.runtime.hypotheses.values())
    assert not result.published_questions
    final_search = model.calls_for("objective_workflow_planner")[-1].payload
    outcome = final_search["candidate_outcomes"][0]
    assert outcome["state"] == "REJECTED"
    assert outcome["answer_status"] == "VERIFIED"
    assert len(outcome["release_reviews"]) == 2
    assert all(r["reason"] for r in outcome["release_reviews"])


def test_waiting_candidate_rotates_to_unprocessed_sibling(tmp_path):
    loop, model = make_loop(tmp_path)
    original = model.responses["objective_candidate_review"]
    def review(request):
        loop.runtime.request_pause()
        return original(request)
    model.set_response("objective_candidate_review", review)
    loop.run()
    snapshot = loop.runtime.resume_from_recovery()
    original_artifact = next(a for a in snapshot.artifacts.values() if a.artifact_type == "question_hypothesis")
    second_id = "hypothesis.second"
    second = original_artifact.model_copy(update={"artifact_id": second_id,
        "payload": {**original_artifact.payload, "hypothesis_id": second_id}})
    snapshot = snapshot.model_copy(update={
        "artifacts": {**snapshot.artifacts, second_id: second},
        "hypotheses": {**snapshot.hypotheses, second_id: HypothesisState.PROPOSED}})
    first = loop.main_agent.decide(snapshot)
    snapshot = snapshot.model_copy(update={"decisions": {**snapshot.decisions, first.decision_id: first}})
    second = loop.main_agent.decide(snapshot)
    assert first.action == second.action == "request_more_evidence"
    assert first.action_parameters["hypothesis_id"] != second.action_parameters["hypothesis_id"]


def test_resume_retries_failed_design_without_growing_material_context(tmp_path):
    from medical_qa_data_factory.llm.errors import ModelError, ModelErrorCode
    loop, model = make_loop(tmp_path)
    original = model.responses["objective_question_design"]
    def fail(request):
        raise ModelError(ModelErrorCode.BAD_REQUEST, "context budget exceeded", retryable=False)
    model.set_response("objective_question_design", fail)
    first = loop.run()
    assert first.stop_reason == "model_request_requires_repair"
    snapshot = loop.runtime.resume_from_recovery()
    assert loop.main_agent.decide(snapshot).action == "design_questions"
    model.set_response("objective_question_design", original)
    result = loop.run()
    assert len(result.published_questions) == 1
    assert actions(loop).count("retrieve_materials") == 1


def test_initial_verification_receives_independent_need_not_keep_review(tmp_path):
    loop, model = make_loop(tmp_path)
    loop.run()
    request = next(r for r in model.calls_for("objective_workflow_planner")
                   if r.payload["stage"] == "verification_gap")
    gap = request.payload["evidence_gap"]
    assert gap["kind"] == "initial_independent_verification"
    assert "excluded_discovery_anchor_ids" not in gap
    assert "disposition" not in gap
    assert not {"facts", "candidate_outcomes", "hypothesis", "candidate_reviews"} & request.payload.keys()


def test_review_gap_reaches_candidate_search(tmp_path):
    loop, model = make_loop(tmp_path)
    original = model.responses["objective_candidate_review"]
    need = "Find a complete list of members, including the index protein"
    def review(request):
        batch = original(request)
        for row in batch["reviews"]:
            row.update(disposition="NEEDS_EVIDENCE", evidence_need=need)
        return batch
    model.set_response("objective_candidate_review", review)
    loop.run()
    gap = next(r.payload["evidence_gap"] for r in model.calls_for("objective_workflow_planner")
               if r.payload["stage"] == "verification_gap")
    assert "complete set" in gap["evidence_need"]


def test_numeric_gene_identity_reaches_fact_extraction(tmp_path):
    from medical_qa_data_factory.generation.structured_biomedical_providers import NcbiGeneMaterialProvider
    loop, model = make_loop(tmp_path)
    def transport(url, headers):
        if "esearch" in url:
            return {"esearchresult": {"idlist": ["672"]}}
        return {"result": {"672": {"uid": "672", "nomenclaturesymbol": "BRCA1",
            "nomenclaturename": "BRCA1 DNA repair associated", "organism": {"taxid": 9606}}}}
    loop.providers["ncbi_gene"] = NcbiGeneMaterialProvider(transport)
    loop.main_agent.provider_ids.append("ncbi_gene")
    model.set_response("fact_understanding", {"claims": [], "insufficiency_reason": "Identity lookup only"})
    loop.max_actions = 2
    loop.run()
    decision = loop.runtime.ordered_decisions()[0]
    assert decision.action_parameters["query"]["native_id"] == "672"
    identity = model.calls_for("fact_understanding")[0].payload["entity_resolutions"][0]
    assert identity["canonical_name"] == "BRCA1 DNA repair associated"
    assert "NCBI Gene 672" in identity["authority"]


def test_search_end_without_evidence_is_indeterminate_not_supported(tmp_path):
    loop, model = make_loop(tmp_path)
    original_facts = model.responses["fact_understanding"]
    def extract(request):
        if any(a["anchor_id"] == "anchor.primekg" for a in request.payload["evidence_anchors"]):
            return original_facts(request)
        return {"claims": [], "insufficiency_reason": "No new supported fact"}
    model.set_response("fact_understanding", extract)
    original = model.responses["objective_workflow_planner"]
    def search(request):
        if request.payload["stage"] == "verification_gap":
            return {"action": "finish_search", "rationale": "The candidate review says the evidence is sufficient"}
        if request.payload["candidate_outcomes"]:
            assert request.payload["published_question_count"] == 0
            outcome = request.payload["candidate_outcomes"][0]
            assert outcome["state"] == "INDETERMINATE"
            assert outcome["answer_status"] is None
            assert outcome["closure_reason"] == "no_independent_verification_evidence"
            return {"action": "stop", "rationale": "Zero published; independent evidence unavailable"}
        return original(request)
    model.set_response("objective_workflow_planner", search)
    result = loop.run()
    assert result.run_state.value == "COMPLETED_WITH_ZERO_QUESTIONS"
    assert not model.calls_for("objective_question_realization")
    assert actions(loop).count("close_candidate") == 1
    decisions = loop.runtime.ordered_decisions()
    closed = next(i for i, d in enumerate(decisions) if d.action == "close_candidate")
    assert any(d.action == "retrieve_materials" and d.action_parameters["provider_id"] == "pubmed"
               for d in decisions[closed + 1:])


def test_search_end_hands_pending_evidence_to_verifier(tmp_path):
    loop, model = make_loop(tmp_path)
    loop.max_actions = 4
    loop.run()
    snapshot = loop.runtime.load()
    hypothesis = next(a.payload for a in snapshot.artifacts.values() if a.artifact_type == "question_hypothesis")
    for action in ("finish_search", "search_exhausted"):
        model.set_response("objective_workflow_planner", {"action": action, "rationale": "End collection"})
        decision = loop.main_agent._search(snapshot, hypothesis=hypothesis, gap={"evidence_need": "independent answer"})
        assert decision.action == "verify_answer"
        assert decision.action_parameters["provider_result_ids"]
        assert decision.action_parameters["hypothesis_id"] == hypothesis["hypothesis_id"]


def test_exploration_sees_final_rejection_after_keep(tmp_path):
    loop, model = make_loop(tmp_path)
    original_verify = model.responses["objective_answer_verification"]
    def verify(request):
        value = original_verify(request)
        if request.payload["stage"] == "compare":
            return value.model_copy(update={"answer_matches_blind": False})
        return value
    model.set_response("objective_answer_verification", verify)
    original_search = model.responses["objective_workflow_planner"]
    observed = []
    def search(request):
        if request.payload["stage"] == "exploration" and request.payload["candidate_outcomes"]:
            observed.append(request.payload)
            return {"action": "stop", "rationale": "No further useful direction after failed verification"}
        return original_search(request)
    model.set_response("objective_workflow_planner", search)
    result = loop.run()
    assert not result.published_questions
    assert len(observed) == 1
    outcome = observed[0]["candidate_outcomes"][0]
    assert outcome["state"] == "REJECTED"
    assert outcome["answer_status"] == "REJECTED"
    assert outcome["verification_reason"]
    assert observed[0]["published_question_count"] == 0
