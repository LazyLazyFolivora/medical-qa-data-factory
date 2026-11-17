from types import SimpleNamespace
from medical_qa_data_factory.generation.research_first_pilot import run_pilot
from test_objective_agent_loop import FakeMaterialProvider


def understanding(ready=False, findings=None):
    return {"theme": "EGFR conditional effects", "findings": findings or [],
            "integrated_conclusion": "Evidence must preserve context", "connections_and_differences": [],
            "unresolved_gaps": [], "ready_for_question": ready}


class ScriptedModel:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.stages = []

    def generate_structured(self, request, schema):
        self.stages.append(request.payload["stage"])
        answer = next(self.answers)
        if callable(answer):
            answer = answer(request)
        return SimpleNamespace(parsed=answer)


def test_partial_understanding_can_design_supported_question(tmp_path):
    finding = {"finding": "Scoped observation", "conditions": "mice",
               "source_ids": ["paper"], "limitations": "Mechanism unresolved"}
    model = ScriptedModel([
        {"understanding": understanding(False, [finding]), "action": "finish", "purpose": "Mechanism unresolved"},
        {"ready": True, "issues": ["Mechanism remains unknown"], "next_evidence_actions": []},
        {"candidate": None, "reason": "Design was reached; candidate still requires evidence"},
    ])
    result = run_pilot(model, FakeMaterialProvider("pubmed", "text"), entity="EGFR",
                       graph_leads=[], output=tmp_path / "partial",
                       initial_sources={"paper": "Scoped observation"})
    assert model.stages == ["research-0", "research-review-0", "design"]
    assert result["published"] is False


def test_insufficient_understanding_does_not_design(tmp_path):
    model = ScriptedModel([{"understanding": understanding(), "action": "finish", "purpose": "Need better sources"}])
    result = run_pilot(model, FakeMaterialProvider("pubmed", "text"), entity="EGFR", graph_leads=[], output=tmp_path / "pilot")
    assert result["design"] is None
    assert model.stages == ["research-0"]


def test_research_then_design_preserves_sources_and_review(tmp_path):
    def finish(request):
        sid = request.payload["sources"][0]["source_id"]
        return {"understanding": understanding(True, [{"finding": "A scoped observation", "conditions": "mice",
                   "source_ids": [sid], "limitations": "Not human evidence"}]), "action": "finish", "purpose": "Ready"}
    def design(request):
        sid = request.payload["sources"][0]["source_id"]
        return {"candidate": {"question": "A bounded comparison?", "answer": "Scoped answer",
                "supporting_source_ids": [sid], "necessary_integrations": ["Compare conditions"],
                "possible_shortcut": "One abstract may suffice"}, "reason": "Experimental draft"}
    model = ScriptedModel([
        {"understanding": understanding(), "action": "search", "query": "EGFR", "purpose": "Read before design"},
        finish, {"ready": True, "issues": [], "next_evidence_actions": []}, design, {"disposition": "revise", "grounded": True, "self_contained": True,
                        "genuine_integration": False, "issues": ["Direct lookup"], "shortest_reasonable_solution": "One record"}])
    result = run_pilot(model, FakeMaterialProvider("pubmed", "EGFR example"), entity="EGFR", graph_leads=[], output=tmp_path / "pilot")
    assert model.stages == ["research-0", "research-1", "research-review-1", "design", "critique"]
    assert result["published"] is False
    assert result["critique"]["disposition"] == "revise"
    assert (tmp_path / "pilot" / "retrieval-0.json").exists()


def test_read_pages_expose_later_results_to_design(tmp_path):
    def read(request):
        return {"understanding": understanding(), "action": "read", "offset": 4500,
                "source_ids": [request.payload["sources"][0]["source_id"]], "purpose": "Read results beyond introduction"}
    def finish(request):
        view = request.payload["sources"][0]
        assert "RESULT_MARKER" in view["read_passages"][0]["text"]
        return {"understanding": understanding(True, [{"finding": "Later result", "conditions": "specified",
                "source_ids": [view["source_id"]], "limitations": "experimental"}]), "action": "finish", "purpose": "Read"}
    def design(request):
        assert "RESULT_MARKER" in request.payload["sources"][0]["read_passages"][0]["text"]
        return {"candidate": None, "reason": "Not yet a synthesis question"}
    model = ScriptedModel([
        {"understanding": understanding(), "action": "search", "query": "EGFR", "purpose": "Find source"},
        read, finish, {"ready": True, "issues": [], "next_evidence_actions": []}, design])
    result = run_pilot(model, FakeMaterialProvider("pubmed", "x" * 4600 + "RESULT_MARKER"),
                       entity="EGFR", graph_leads=[], output=tmp_path / "pilot")
    assert result["design"]["candidate"] is None


def test_premature_finish_returns_review_gap_to_research(tmp_path):
    def continue_research(request):
        assert request.payload["history"][-1]["issues"] == ["Read the experimental controls"]
        assert request.payload["previous_understanding"]["ready_for_question"] is False
        return {"understanding": understanding(), "action": "finish", "purpose": "Evidence not sufficient"}
    model = ScriptedModel([
        {"understanding": understanding(True), "action": "finish", "purpose": "Premature"},
        {"ready": False, "issues": ["Read the experimental controls"], "next_evidence_actions": ["Read results"]},
        continue_research])
    result = run_pilot(model, FakeMaterialProvider("pubmed", "text"), entity="EGFR", graph_leads=[], output=tmp_path / "pilot")
    assert result["design"] is None
    assert model.stages == ["research-0", "research-review-0", "research-1"]


def test_read_does_not_require_speculative_understanding(tmp_path):
    def finish(request):
        assert request.payload["previous_understanding"] is None
        assert "actual result" in request.payload["sources"][0]["read_passages"][0]["text"]
        return {"action": "finish", "purpose": "Insufficient", "understanding": understanding()}
    model = ScriptedModel([
        {"action": "read", "source_ids": ["paper"], "purpose": "Inspect result", "understanding": None}, finish])
    result = run_pilot(model, FakeMaterialProvider("pubmed", "text"), entity="EGFR", graph_leads=[],
                       initial_sources={"paper": "actual result"}, output=tmp_path / "pilot")
    assert result["design"] is None


def test_read_whole_paragraphs_keeps_the_comparison_together(tmp_path):
    from medical_qa_data_factory.generation.research_documents import index_plain_text
    document = index_plain_text("A" * 4400 + "\n\nControl result\n\nContrasting result")
    def finish(request):
        parts = request.payload["sources"][0]["read_passages"]
        assert [p["text"] for p in parts] == ["Control result", "Contrasting result"]
        assert all(p["next_offset"] is None for p in parts)
        return {"action": "finish", "purpose": "Done", "understanding": understanding()}
    model = ScriptedModel([
        {"action": "read", "source_ids": ["paper"], "paragraph_ids": ["p1", "p2"], "purpose": "Compare"}, finish])
    run_pilot(model, FakeMaterialProvider("pubmed", "text"), entity="EGFR", graph_leads=[],
              initial_documents={"paper": document}, output=tmp_path / "pilot")


def test_details_reuses_saved_document(tmp_path):
    from medical_qa_data_factory.generation.research_documents import index_plain_text
    def finish(request):
        assert request.payload["history"][-1]["source_ids"] == ["paper.details-seed"]
        assert len(request.payload["sources"]) == 1
        return {"action": "finish", "purpose": "Done", "understanding": understanding()}
    model = ScriptedModel([
        {"action": "details", "source_ids": ["paper.details-seed"], "purpose": "Get body"}, finish])
    run_pilot(model, FakeMaterialProvider("pubmed", "text"), entity="EGFR", graph_leads=[],
              initial_documents={"paper.details-seed": index_plain_text("Body")}, output=tmp_path / "pilot")
