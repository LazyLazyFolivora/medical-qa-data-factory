import json
from types import SimpleNamespace

import pytest

from medical_qa_data_factory.generation.exploration_memory import HistoryNote, prepare_exploration_history
from medical_qa_data_factory.generation.workflow_scheduler import SearchChoice
from medical_qa_data_factory.llm.contracts import StructuredGenerationRequest


class Model:
    def __init__(self):
        self.summaries = []

    def needs_history_compaction(self, request, schema):
        if schema is HistoryNote:
            return len(json.dumps(request.payload)) > 1200
        return len(json.dumps(request.payload["facts"])) > 700

    def generate_structured(self, request, schema):
        assert schema is HistoryNote
        assert not self.needs_history_compaction(request, schema)
        self.summaries.append(request.payload)
        return SimpleNamespace(parsed={"note": "查过方向 A，仍缺独立来源。"})


def request(facts):
    return StructuredGenerationRequest(invocation_id="call", idempotency_key="key",
        role="objective_workflow_planner", system_prompt_version="test",
        payload={"stage": "exploration", "facts": facts, "published_question_count": 0,
                 "candidate_outcomes": [{"state": "INDETERMINATE"}]})


def test_new_facts_stay_raw_and_note_survives_restart(tmp_path):
    old, new = "a" * 500, "b" * 500
    model = Model()
    def run(values, model=model):
        return prepare_exploration_history(model, request(values), SearchChoice, tmp_path, "test")
    assert run([old]).payload["facts"] == [old]
    assert not model.summaries
    active = run([new, old])
    assert active.payload["facts"] == [new]
    assert len(model.summaries) == 1
    restarted = Model()
    assert run([new, old], restarted).payload == active.payload
    assert not restarted.summaries
    assert active.payload["candidate_outcomes"] == [{"state": "INDETERMINATE"}]
    assert active.payload["published_question_count"] == 0
    later = run(["c" * 500, new, old], restarted)
    assert later.payload["facts"] == ["c" * 500]
    assert restarted.summaries[0]["previous_note"] == active.payload["exploration_history_note"]
    assert "exploration_history_note" not in run(["replacement"], restarted).payload


def test_oversized_history_split_and_failed_summary_does_not_hide_record(tmp_path):
    model = Model()
    req = request(["a" * 4100])
    def run():
        return prepare_exploration_history(model, req, SearchChoice, tmp_path, "test")
    normal = model.generate_structured
    def interrupted(req, schema):
        if model.summaries:
            raise RuntimeError("interrupted")
        return normal(req, schema)
    model.generate_structured = interrupted
    with pytest.raises(RuntimeError, match="interrupted"):
        run()
    assert not list(tmp_path.rglob("*.json"))
    model.generate_structured = normal
    assert run().payload["facts"] == []
    excerpts = [payload["records"][0][1]["excerpt"] for payload in model.summaries[1:]]
    assert json.loads("".join(excerpts)) == req.payload["facts"][0]
    blind = req.model_copy(update={"payload": {"stage": "verification_gap"}})
    assert prepare_exploration_history(model, blind, SearchChoice, tmp_path, "test") is blind


def test_artifact_index_compacts_without_losing_current_design_inputs(tmp_path):
    class ArtifactModel(Model):
        def needs_history_compaction(self, request, schema):
            if schema is HistoryNote:
                return super().needs_history_compaction(request, schema)
            return len(json.dumps(request.payload.get("artifact_index", []))) > 700

    model = ArtifactModel()
    req = request([])
    inputs = {"available_claim_ids": ["claim.example"], "claim_count": 1}
    req = req.model_copy(update={"payload": {**req.payload,
        "artifact_index": [{"id": "material.example", "preview": "x" * 900}], "design_inputs": inputs}})
    active = prepare_exploration_history(model, req, SearchChoice, tmp_path, "test")
    assert model.summaries
    assert active.payload["artifact_index"] == []
    assert active.payload["design_inputs"] == inputs


def test_react_compaction_keeps_host_records_and_does_not_reuse_legacy_note(tmp_path):
    from medical_qa_data_factory.generation.objective_question_contracts import canonical_digest
    folder = tmp_path / "objective_runtime" / "exploration_memory"
    folder.mkdir(parents=True)
    old = folder / (canonical_digest("test") + ".json")
    old.write_text(json.dumps({"note": "WRONG: read all papers", "covered": [], "order": []}))
    host = {"prior_searches": [{"action": "read_source_details"}],
            "search_results": [{"material_ids": ["paper"]}],
            "artifact_index": [{"id": "paper", "material_type": "abstract"}],
            "reading_ledger": {"materials": [{"material_id": "paper", "returned_paragraph_ids": []}]}}
    req = request(["x" * 900])
    req = req.model_copy(update={"payload": {**req.payload, **host, "control_mode": "objective-react-v1"}})
    model = Model()
    active = prepare_exploration_history(model, req, SearchChoice, tmp_path, "test")
    assert all(active.payload[key] == value for key, value in host.items())
    assert model.summaries[0]["previous_note"] == ""
    assert all(field == "facts" for call in model.summaries for field, _ in call["records"])
    assert "WRONG" in old.read_text()


def test_tool_history_compacts_oldest_keeps_latest_and_restores_after_restart(tmp_path):
    class ObservationModel(Model):
        def needs_history_compaction(self, request, schema):
            if schema is HistoryNote:
                return False
            return len(request.payload.get("latest_observations", [])) > 1

    observations = [{"id": f"obs.{i}", "step_sequence": i, "action": "read_artifacts",
        "payload": {"source_views": [{"material_id": f"M{i:04d}", "content": f"original {i}"}]}}
        for i in (3, 2, 1)]
    req = request([])
    req = req.model_copy(update={"payload": {**req.payload, "control_mode": "objective-react-v1",
        "latest_observations": observations, "visible_source_text_ids": ["M0001", "M0002", "M0003"]}})
    model = ObservationModel()
    active = prepare_exploration_history(model, req, SearchChoice, tmp_path, "continuity")
    assert active.payload["latest_observations"] == observations[:1]
    assert active.payload["visible_source_text_ids"] == ["M0003"]
    assert active.payload["compacted_observation_ids"] == ["obs.1", "obs.2"]
    assert [v[1]["id"] for call in model.summaries for v in call["records"]] == ["obs.1", "obs.2"]
    restarted = ObservationModel()
    restored = prepare_exploration_history(restarted, req, SearchChoice, tmp_path, "continuity")
    assert restored.payload == active.payload
    assert not restarted.summaries
