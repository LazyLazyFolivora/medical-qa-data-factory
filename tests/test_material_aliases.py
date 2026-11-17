from types import SimpleNamespace

from medical_qa_data_factory.generation.material_aliases import material_aliases, translate_references
from test_react_main_agent import make_loop


def test_aliases_survive_restart_and_new_earlier_sorted_material():
    snapshot = SimpleNamespace(artifacts={"material.z": SimpleNamespace(artifact_type="evidence_material")})
    initial = material_aliases(snapshot, [])
    snapshot.artifacts["material.a"] = SimpleNamespace(artifact_type="evidence_material")
    resumed = material_aliases(snapshot, [{"parameters": {"material_aliases": initial}}])
    assert resumed == {"M0001": "material.z", "M0002": "material.a"}
    assert material_aliases(snapshot, [{"parameters": {"material_aliases": resumed}}]) == resumed


def test_references_in_section_keys_and_notes_roundtrip_without_fuzzy_matching():
    raw = {"input_artifact_ids": ["material.z"], "source_sections": {"material.z": ["p0"]},
           "findings": [{"source_ids": ["material.z"]}], "text": "material.z is mentioned"}
    visible = translate_references(raw, {"material.z": "M0001"})
    assert visible["source_sections"] == {"M0001": ["p0"]}
    assert translate_references(visible, {"M0001": "material.z"}) == raw
    assert translate_references("M000l", {"M0001": "material.z"}) == "M000l"
    ordinary = {"query": {"term": "M0001"}, "rationale": "M0001", "content": "M0001"}
    assert translate_references(ordinary, {"M0001": "material.z"}) == ordinary


def test_model_can_read_material_by_short_handle(tmp_path):
    def choose(request):
        if not request.payload["prior_searches"]:
            return {"action": "retrieve_materials", "rationale": "Find evidence", "parameters": {
                "provider_id": "primekg", "query": {"mention": "BRCA1"}, "evidence_need": "location"}}
        mids = request.payload["materials_without_extracted_claims"]
        assert mids == ["M0001"]
        assert request.payload["unread_material_ids"] == ["M0001"]
        assert request.payload["fully_read_material_ids"] == []
        assert "material_aliases" not in str(request.payload)
        return {"action": "read_artifacts", "rationale": "Read", "input_artifact_ids": mids, "parameters": {}}
    loop, _ = make_loop(tmp_path, choose, max_actions=2)
    result = loop.run()
    steps = sorted((a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "react_step"), key=lambda s: s["sequence"])
    assert steps[-1]["action"] == "read_artifacts"
    assert steps[-1]["parameters"]["material_aliases"] == {"M0001": "material.primekg"}
    views = [a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "react_observation"]
    assert views[-1]["source_views"][0]["material_id"] == "material.primekg"


def test_bad_batch_reports_all_missing_ids_and_limit_without_reading(tmp_path):
    def choose(request):
        return {"action": "read_artifacts", "rationale": "Bad references", "parameters": {},
                "input_artifact_ids": [f"M{i:04d}" for i in range(9)]}
    loop, _ = make_loop(tmp_path, choose, max_actions=1)
    result = loop.run()
    obs = next(a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "react_observation")
    assert all(f"M{i:04d}" in obs["error"] for i in range(9))
    assert "at most 8" in obs["error"]
    assert not obs["attempted_tool_executed"]
    step = next(a.payload for a in result.runtime.artifacts.values() if a.artifact_type == "react_step")
    assert step["execution_record"]["status"] == "NOT_EXECUTED"
    assert step["execution_record"]["feedback_delivery_status"] == "SUCCEEDED"


def test_old_feedback_success_is_not_presented_as_tool_success():
    from medical_qa_data_factory.generation.react_execution_state import execution_state
    snapshot = SimpleNamespace(artifacts={}, state_revision=1)
    state = execution_state(snapshot, [{"action": "tool_feedback", "sequence": 1,
        "result_ids": [], "execution_record": {"status": "SUCCEEDED"}}])
    assert state["last_completed_step"]["execution"]["status"] == "NOT_EXECUTED"
