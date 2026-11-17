import pytest

from medical_qa_data_factory.generation.react_main_agent import ReactChoice
from test_react_main_agent import make_loop


@pytest.mark.parametrize("nested,legacy_flag", [(False, False), (True, True)])
def test_same_batch_read_for_canonical_and_saved_call(tmp_path, nested, legacy_flag):
    def choose(request):
        if not request.payload["prior_searches"]:
            return {"action": "retrieve_materials", "rationale": "Get two sources", "parameters": {"searches": [
                {"provider_id": "pubmed", "query": {"term": "BRCA1"}, "evidence_need": "source"},
                {"provider_id": "primekg", "query": {"mention": "BRCA1"}, "evidence_need": "source"}]}}
        ids = ["material.pubmed", "material.primekg"]
        parameters = {"source_view": True} if legacy_flag else {}
        choice = {"action": "read_artifacts", "rationale": "Read both", "parameters": parameters}
        (parameters if nested else choice)["input_artifact_ids"] = ids
        return choice

    loop, _ = make_loop(tmp_path, choose, max_actions=2)
    result = loop.run()
    assert [d.action for d in loop.runtime.ordered_decisions()] == ["retrieve_materials", "read_artifacts"]
    decision = loop.runtime.ordered_decisions()[-1]
    assert "input_artifact_ids" not in decision.action_parameters
    observation = next(a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "react_observation" and "source_views" in a.payload)
    assert [v["material_id"] for v in observation["source_views"]] == ["material.pubmed", "material.primekg"]
    assert all(v["content_complete"] and "chromosome 17" in v["content"] for v in observation["source_views"])


def test_conflicting_id_locations_are_not_silently_overridden():
    with pytest.raises(ValueError, match="Conflicting input_artifact_ids"):
        ReactChoice.model_validate({"action": "read_artifacts", "rationale": "Read",
            "input_artifact_ids": ["one"], "parameters": {"input_artifact_ids": ["two"]}})


def test_missing_source_is_reported_without_crashing_run(tmp_path, monkeypatch):
    loop, _ = make_loop(tmp_path, lambda r: {
        "action": "retrieve_materials" if not r.payload["prior_searches"] else "read_artifacts",
        "rationale": "Read source", "input_artifact_ids": [] if not r.payload["prior_searches"] else ["material.pubmed"],
        "parameters": {"provider_id": "pubmed", "query": {"term": "BRCA1"}, "evidence_need": "source"}
            if not r.payload["prior_searches"] else {}}, max_actions=1)
    loop.run()
    loop.runtime.resume_from_recovery()
    def missing(material):
        raise FileNotFoundError("source missing")
    monkeypatch.setattr(loop.persistence, "load_raw_content", missing)
    result = loop.run()
    view = next(a.payload["source_views"][0] for a in result.runtime.artifacts.values()
                if "source_views" in a.payload)
    assert view["material_id"] == "material.pubmed"
    assert view["content_complete"] is False
    assert "FileNotFoundError" in view["read_error"]


def test_batch_passages_are_selected_per_material(tmp_path):
    def choose(request):
        if not request.payload["prior_searches"]:
            return {"action": "retrieve_materials", "rationale": "Get sources", "parameters": {"searches": [
                {"provider_id": pid, "query": {"term": "BRCA1"}, "evidence_need": "source"}
                for pid in ("pubmed", "primekg")]}}
        return {"action": "read_artifacts", "rationale": "Read passages",
                "input_artifact_ids": ["material.pubmed", "material.primekg"],
                "parameters": {"source_sections": {"material.pubmed": ["p0"], "material.primekg": ["p0"]}}}
    loop, _ = make_loop(tmp_path, choose, max_actions=2)
    result = loop.run()
    observation = next(a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "react_observation" and "source_views" in a.payload)
    assert len(observation["source_views"]) == 2
    assert all(v["excerpts"][0]["paragraph_id"] == "p0" for v in observation["source_views"])
    assert all(v["content_complete"] for v in observation["source_views"])
    from medical_qa_data_factory.generation.react_observations import ObservationView
    ledger = ObservationView(result.runtime).reading_ledger()
    assert all(m["returned_paragraph_ids"] == ["p0"] and m["whole_stored_content_returned"]
               for m in ledger["materials"])
