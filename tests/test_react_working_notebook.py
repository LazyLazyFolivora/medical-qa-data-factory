"""Working notes survive tool execution/resume without becoming evidence."""
import pytest
from pydantic import ValidationError
from test_react_main_agent import make_loop
from medical_qa_data_factory.generation.react_main_agent import WorkingNotebook, latest_notebook


def notebook(progress="No tools executed yet"):
    return {"current_goal": "Explore BRCA1", "findings": [],
            "last_step_progress": progress, "open_questions": ["Which sources support location?"],
            "explored_directions": [], "next_step": "Search sources"}


def test_note_is_persisted_and_used_after_resume(tmp_path):
    def choose(request):
        if not request.payload["prior_searches"]:
            assert request.payload["working_notebook"] is None
            return {"action": "retrieve_materials", "rationale": "Explore", "working_notebook": notebook(),
                    "parameters": {"provider_id": "pubmed", "query": {"term": "BRCA1"},
                                   "evidence_need": "Find location"}}
        assert request.payload["working_notebook"]["current_goal"] == "Explore BRCA1"
        assert all("working_notebook" not in s["parameters"] for s in request.payload["prior_searches"])
        assert request.payload["latest_observations"]
        return {"action": "stop", "rationale": "Fixture completed", "working_notebook": notebook("Search returned evidence")}
    loop, model = make_loop(tmp_path, choose, max_actions=1)
    first = loop.run()
    steps = [a.payload for a in first.runtime.artifacts.values() if a.artifact_type == "react_step"]
    assert isinstance(latest_notebook(steps)["observed_state_revision"], int)
    assert not any(a.artifact_type == "candidate_claim" for a in first.runtime.artifacts.values())
    loop.runtime.resume_from_recovery()
    loop.run()
    final = loop.runtime.ordered_decisions()[-1]
    assert "last_step_progress" not in final.action_parameters["working_notebook"]


def test_missing_source_is_marked_not_promoted_to_evidence(tmp_path):
    note = notebook()
    note["findings"] = [{"statement": "Unconfirmed finding", "status": "tentative", "source_ids": ["unknown"]}]
    loop, _ = make_loop(tmp_path, lambda r: {"action": "stop", "rationale": "Fixture", "working_notebook": note})
    loop.run()
    decision = loop.runtime.ordered_decisions()[-1]
    assert decision.action_parameters["working_notebook"]["unresolved_source_ids"] == ["unknown"]


def test_notebook_bounds_and_legacy_steps():
    with pytest.raises(ValidationError):
        WorkingNotebook.model_validate({**notebook(), "open_questions": ["x" * 301]})
    assert latest_notebook([{"parameters": {"working_notebook": notebook()}}, {"parameters": {}}]) == {
        k: v for k, v in notebook().items() if k != "last_step_progress"}



def test_invalid_tool_retains_note_and_reference_warnings(tmp_path):
    note = notebook()
    note["findings"] = [{"statement": "Needs checking", "status": "tentative", "source_ids": ["missing"]}]
    loop, _ = make_loop(tmp_path, lambda r: {"action": "unknown_tool", "rationale": "Fixture", "working_notebook": note}, max_actions=1)
    loop.run()
    decision = loop.runtime.ordered_decisions()[-1]
    assert decision.action == "tool_feedback"
    assert decision.action_parameters["working_notebook"]["unresolved_source_ids"] == ["missing"]
    assert "observed_state_revision" in decision.action_parameters["working_notebook"]


def test_echoed_host_metadata_is_ignored_and_recomputed(tmp_path):
    note = notebook()
    note.update(unresolved_source_ids=[], observed_state_revision=99999)
    note["findings"] = [{"statement": "Unconfirmed", "status": "tentative", "source_ids": ["missing"]}]
    loop, _ = make_loop(tmp_path, lambda r: {"action": "stop", "rationale": "Fixture", "working_notebook": note})
    loop.run()
    saved = loop.runtime.ordered_decisions()[-1].action_parameters["working_notebook"]
    assert saved["unresolved_source_ids"] == ["missing"]
    assert saved["observed_state_revision"] != 99999
