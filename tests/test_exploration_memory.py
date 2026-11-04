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
