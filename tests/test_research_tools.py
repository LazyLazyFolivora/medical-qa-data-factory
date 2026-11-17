from threading import Barrier

from test_react_main_agent import make_loop
from test_react_main_agent import scripted_choice
from test_objective_agent_loop import FakeMaterialProvider


def test_batch_overlaps_remote_io_and_commits_each_provider_result(tmp_path):
    barrier = Barrier(2)
    class ConcurrentProvider(FakeMaterialProvider):
        def retrieve(self, request):
            barrier.wait(timeout=5)
            return super().retrieve(request)
    def choose(request):
        return {"action": "retrieve_materials", "rationale": "Independent source questions", "parameters": {"searches": [
            {"provider_id": "pubmed", "query": "BRCA1", "evidence_need": "Mechanism"},
            {"provider_id": "hgnc", "query": {"symbol": "BRCA1"}, "evidence_need": "Identity"}]}}
    loop, _ = make_loop(tmp_path, choose, max_actions=1)
    loop.providers = {pid: ConcurrentProvider(pid, "BRCA1 information") for pid in ("pubmed", "hgnc")}
    loop.main_agent.provider_ids = list(loop.providers)
    result = loop.run()
    searches = [a for a in result.runtime.artifacts.values() if a.artifact_type == "provider_result"]
    assert len(searches) == 2
    assert all(a.payload["terminal_status"] == "SUCCEEDED" for a in searches)
    assert len({a.payload["request"]["request_id"] for a in searches}) == 2
    assert sum(a.artifact_type == "react_step" for a in result.runtime.artifacts.values()) == 1


def test_duplicate_batch_hits_and_failed_source_keep_successes(tmp_path):
    class Broken(FakeMaterialProvider):
        def retrieve(self, request):
            raise ConnectionError("source offline")
    def choose(request):
        return {"action": "retrieve_materials", "rationale": "Compare sources", "parameters": {"searches": [
            {"provider_id": "pubmed", "query": {"term": term}, "evidence_need": "Mechanism"}
            for term in ("BRCA1", "BRCA1 repair")] + [
            {"provider_id": "primekg", "query": {"mention": "BRCA1"}, "evidence_need": "Relations"}]}}
    loop, _ = make_loop(tmp_path, choose, max_actions=1)
    loop.providers["primekg"] = Broken("primekg", "unused")
    result = loop.run()
    searches = [a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "provider_result"]
    assert len(searches) == 3
    assert sum(p["terminal_status"] == "SUCCEEDED" for p in searches) == 2
    assert next(p for p in searches if p["terminal_status"] == "FAILED")["error_category"] == "ConnectionError"
    assert sum(a.artifact_type == "evidence_material" for a in result.runtime.artifacts.values()) == 1


def test_batch_query_validation_happens_before_any_network_call(tmp_path):
    loop, _ = make_loop(tmp_path, lambda r: {"action": "retrieve_materials", "rationale": "Malformed batch",
        "parameters": {"searches": [{"provider_id": "pubmed", "query": {"term": "BRCA1"}, "evidence_need": "Mechanism"},
                                    {"provider_id": "unknown", "query": {}, "evidence_need": "Identity"}]}}, max_actions=1)
    result = loop.run()
    assert all(p.calls == 0 for p in loop.providers.values())
    assert any(a.artifact_type == "react_observation" and "searches[1]" in a.payload["error"]
               for a in result.runtime.artifacts.values())


def test_details_preserve_abstract_and_can_be_read_again(tmp_path):
    class WithDetails(FakeMaterialProvider):
        def retrieve(self, request):
            r = super().retrieve(request)
            r.materials[0]["source_record_id"] = "pmid:123"
            return r
        def retrieve_details(self, request, *, material_ids):
            assert material_ids == ["material.pubmed.123"]
            detail = FakeMaterialProvider("pubmed", "Detailed methods and results with exact population and conditions.").retrieve(request)
            detail.materials[0]["source_record_id"] = "pmid:123"
            detail.materials[0]["material_type"] = "literature_full_text"
            return detail
    def choose(request):
        if not request.payload["prior_searches"]:
            return {"action": "retrieve_materials", "rationale": "Find article", "parameters": {
                "provider_id": "pubmed", "query": "BRCA1", "evidence_need": "Mechanism"}}
        return {"action": "read_source_details", "rationale": "Need conditions", "input_artifact_ids": ["material.pubmed"],
                "parameters": {"evidence_need": "Read study methods and results"}}
    loop, _ = make_loop(tmp_path, choose, max_actions=3)
    loop.providers["pubmed"] = WithDetails("pubmed", "Brief abstract")
    result = loop.run()
    materials = [a for a in result.runtime.artifacts.values() if a.artifact_type == "evidence_material"]
    assert len(materials) == 2
    assert result.runtime.artifacts["material.pubmed"].payload["material_type"] == "text"
    assert any(a.payload["material_type"] == "literature_full_text" for a in materials)
    assert len([a for a in result.runtime.artifacts.values() if a.artifact_type == "provider_result"]) == 3
    acquired = [a.payload for a in result.runtime.artifacts.values()
                if a.artifact_id.startswith("fulltext.")]
    assert acquired
    article = acquired[0]["articles"][0]
    assert article["acquisition_status"] == "acquired"
    assert article["reading_status"] == "not_read_by_this_tool"
    assert article["documents"][0]["material_id"] != "material.pubmed"
    assert article["documents"][0]["paragraph_index"]
    assert "content" not in article["documents"][0]


def test_fulltext_acquisition_failure_is_explicit_and_keeps_abstract(tmp_path):
    from medical_qa_data_factory.generation.evidence_provider import EvidenceProviderResult, ProviderTerminalStatus

    class NoFulltext(FakeMaterialProvider):
        def retrieve_details(self, request, *, material_ids):
            return EvidenceProviderResult(request=request, terminal_status=ProviderTerminalStatus.UNKNOWN,
                error_category="PUBMED_FULLTEXT_ERROR", usage={"error_message": "connection denied"})

    def choose(request):
        if not request.payload["prior_searches"]:
            return {"action": "retrieve_materials", "rationale": "Find article", "parameters": {
                "provider_id": "pubmed", "query": {"term": "BRCA1"}, "evidence_need": "Mechanism"}}
        return {"action": "read_source_details", "rationale": "Need experimental conditions",
                "input_artifact_ids": ["material.pubmed"], "parameters": {"evidence_need": "Read methods"}}

    loop, _ = make_loop(tmp_path, choose, max_actions=2)
    loop.providers["pubmed"] = NoFulltext("pubmed", "Abstract remains available")
    result = loop.run()
    observation = next(a.payload for a in result.runtime.artifacts.values() if a.artifact_id.startswith("fulltext."))
    assert observation["articles"][0]["acquisition_status"] == "not_acquired"
    assert observation["articles"][0]["documents"] == []
    assert "connection denied" in str(observation["retrieval_diagnostics"])
    assert "material.pubmed" in result.runtime.artifacts


def test_synthesis_uses_source_text_and_does_not_create_claims(tmp_path):
    def choose(request):
        if not request.payload["prior_searches"]:
            return {"action": "retrieve_materials", "rationale": "Find evidence", "parameters": {
                "provider_id": "pubmed", "query": "BRCA1", "evidence_need": "Mechanism"}}
        return {"action": "synthesize_evidence", "rationale": "Assess detail", "input_artifact_ids": ["material.pubmed"],
                "parameters": {"research_question": "What conditions support a useful comparison?"}}
    loop, model = make_loop(tmp_path, choose, max_actions=2)
    def assess(request):
        assert "Independent report" in request.payload["materials"][0]["content"]
        return {"sources": [{"material_id": "material.pubmed", "information_value": "Location only", "limitations": "No mechanism",
                             "further_reading_needed": True, "missing_detail": "Experimental conditions"}],
                "remaining_gaps": ["Read methods before proposing a causal question"]}
    model.set_response("objective_evidence_synthesis", assess)
    result = loop.run()
    assert any(a.payload.get("status") == "model_assessment_not_verified_fact" for a in result.runtime.artifacts.values())
    assert not any(a.artifact_type == "candidate_claim" for a in result.runtime.artifacts.values())


def test_parallel_verification_results_keep_candidate_ownership(tmp_path):
    def choose(request):
        choice = scripted_choice(request)
        if choice["action"] == "request_more_evidence":
            p = choice["parameters"]
            choice["parameters"] = {"hypothesis_id": p["hypothesis_id"], "searches": [
                {"provider_id": "pubmed", "query": {"term": term}, "evidence_need": "Independently answer public question"}
                for term in ("BRCA1 chromosome location", "BRCA1 genome location")]}
        return choice
    loop, _ = make_loop(tmp_path, choose)
    result = loop.run()
    results = [a for a in result.runtime.artifacts.values() if a.artifact_type == "provider_result"
               and a.metadata.get("purpose") == "verification"]
    assert len(results) == 2
    assert all(a.metadata["hypothesis_id"] in result.runtime.hypotheses for a in results)
    assert len(result.published_questions) == 1


def test_selected_research_passages_reach_claims_and_design_without_old_theory(tmp_path):
    from test_objective_agent_loop import _claim, _hypothesis
    text = "[Introduction]\n" + "X" * 13000 + "\n\n[Results]\nBRCA1 is located on human chromosome 17."
    def choose(request):
        p = request.payload
        n = len(p["prior_searches"])
        if n == 0:
            return scripted_choice(request)
        if n == 1:
            return {"action": "read_artifacts", "input_artifact_ids": ["material.primekg"],
                    "rationale": "Locate results", "parameters": {"source_view": True}}
        if n == 2:
            view = p["latest_observations"][0]["payload"]["source_views"][0]
            results = next(row["paragraph_id"] for row in view["paragraph_index"] if row["section"] == "Results")
            return {"action": "synthesize_evidence", "input_artifact_ids": ["material.primekg"],
                    "rationale": "Compare actual results", "parameters": {"research_question": "Which location is supported?",
                        "source_sections": {"material.primekg": [results]}}}
        brief = p["design_inputs"]["research_briefs"][0]
        if n == 3:
            return {"action": "understand_facts", "input_artifact_ids": brief["source_ids"],
                    "rationale": "Extract selected observations", "parameters": {"research_id": brief["research_id"]}}
        if n == 4:
            assert "M0001" in p["materials_without_extracted_claims"]  # Not the entire paper; model-facing handle.
            return {"action": "design_questions", "rationale": "Abstract the information need", "parameters": {
                "evidence_sufficiency_reason": "Selected research passages and claims supply the observed comparison; no causal inference is required.",
                "research_id": brief["research_id"], "claim_ids": p["design_inputs"]["available_claim_ids"]}}
        return {"action": "stop", "rationale": "Integration fixture complete"}
    loop, model = make_loop(tmp_path, choose, max_actions=6)
    loop.providers["primekg"] = FakeMaterialProvider("primekg", text)
    def synthesize(request):
        assert "X" not in request.payload["materials"][0]["content"]
        assert "paragraph_index" not in request.payload["materials"][0]
        location = request.payload["materials"][0]["excerpt_locations"][0]
        assert location["paragraph_id"] == "p3"
        assert location["offset"] > 13000
        return {"sources": [{"material_id": "material.primekg", "information_value": "Location", "limitations": "Human",
            "further_reading_needed": False, "missing_detail": ""}], "integrated_understanding": "UNSUPPORTED THEORY",
            "question_focus": "Identify the supported human location", "answer_boundaries": ["Human only"], "ready_for_design": True}
    def extract(request):
        assert "X" not in request.payload["trust_boundary"]["materials"][0]["content"]
        anchor = request.payload["evidence_anchors"][0]
        assert anchor["start_offset"] > 13000
        claim = _claim().model_copy(update={"supporting_anchor_ids": [anchor["anchor_id"]]})
        return {"claims": [claim.model_dump(mode="json")], "material_attributions": [
            {"material_id": "material.primekg", "status": "matched", "identity_basis": "BRCA1"}]}
    def design(request):
        assert "UNSUPPORTED THEORY" not in str(request.payload)
        context = request.payload["research_context"]
        assert context["question_focus"] == "Identify the supported human location"
        assert "chromosome 17" in context["source_passages"][0]["excerpts"][0]["text"]
        claim = request.payload["candidate_claims"][0]
        return {"hypotheses": [_hypothesis(claim_id=claim["claim_id"]).model_copy(update={
            "discovery_anchor_ids": claim["supporting_anchor_ids"]}).model_dump(mode="json")]}
    model.set_response("objective_evidence_synthesis", synthesize)
    model.set_response("fact_understanding", extract)
    model.set_response("objective_question_design", design)
    result = loop.run()
    assert len(result.runtime.hypotheses) == 1
    assert len(model.calls_for("objective_question_design")) == 1
    for a in result.runtime.artifacts.values():
        if a.artifact_id.startswith("anchor.research."):
            assert loop.persistence.load_anchor(a.artifact_id).quoted_text in text


def test_oversized_combined_synthesis_is_returned_as_tool_feedback(tmp_path):
    def choose(request):
        if not request.payload["prior_searches"]:
            return {"action": "retrieve_materials", "rationale": "Two independent sources", "parameters": {"searches": [
                {"provider_id": pid, "query": {"term": "BRCA1"}, "evidence_need": "Results"} for pid in ("pubmed", "primekg")]}}
        return {"action": "synthesize_evidence", "rationale": "Compare", "input_artifact_ids": ["material.pubmed", "material.primekg"],
                "parameters": {"research_question": "Compare outcomes"}}
    loop, model = make_loop(tmp_path, choose, max_actions=2)
    loop.providers = {pid: FakeMaterialProvider(pid, "数据" * 5000) for pid in ("pubmed", "primekg")}
    result = loop.run()
    assert not model.calls_for("objective_evidence_synthesis")
    assert any("aggregate budget" in str(a.payload) for a in result.runtime.artifacts.values())


def test_long_fact_source_returns_to_controller_and_direct_selection_recovers(tmp_path):
    from test_objective_agent_loop import _claim
    text = "[Introduction]\n" + "X" * 20000 + "\n\n[Results]\nBRCA1 is located on human chromosome 17."
    def choose(request):
        p = request.payload
        n = len(p["prior_searches"])
        if n == 0:
            return scripted_choice(request)
        if n == 1:
            return {"action": "understand_facts", "rationale": "Read all", "input_artifact_ids": ["material.primekg"]}
        if n == 2:
            assert "Fact extraction was not sent" in str(p["latest_observations"])
            return {"action": "read_artifacts", "rationale": "Find results", "input_artifact_ids": ["material.primekg"],
                    "parameters": {"source_view": True}}
        view = p["latest_observations"][0]["payload"]["source_views"][0]
        pid = next(row["paragraph_id"] for row in view["paragraph_index"] if row["section"] == "Results")
        return {"action": "understand_facts", "rationale": "Extract selected result", "input_artifact_ids": ["material.primekg"],
                "parameters": {"source_sections": {"material.primekg": [pid]}}}
    loop, model = make_loop(tmp_path, choose, max_actions=4)
    loop.providers["primekg"] = FakeMaterialProvider("primekg", text)
    def extract(request):
        assert "X" not in request.payload["trust_boundary"]["materials"][0]["content"]
        anchor = request.payload["evidence_anchors"][0]
        return {"claims": [_claim().model_copy(update={"supporting_anchor_ids": [anchor["anchor_id"]]}).model_dump(mode="json")],
                "material_attributions": [{"material_id": "material.primekg", "status": "matched", "identity_basis": "BRCA1"}]}
    model.set_response("fact_understanding", extract)
    result = loop.run()
    assert len(model.calls_for("fact_understanding")) == 1
    assert any(a.artifact_type == "candidate_claim" for a in result.runtime.artifacts.values())
    assert any(a.artifact_type == "fact_understanding_batch" and a.payload["partial_source_selection"]
               for a in result.runtime.artifacts.values())
