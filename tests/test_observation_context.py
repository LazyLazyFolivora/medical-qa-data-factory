from types import SimpleNamespace

from medical_qa_data_factory.generation.react_observations import ObservationView
from medical_qa_data_factory.generation.material_aliases import translate_references


def test_pubmed_reading_route_survives_preview_truncation_and_aliases():
    from test_reading_ledger import artifact
    abstract = artifact("material.abstract", "evidence_material", provider_id="pubmed",
                        source_record_id="pmid:123", material_type="literature_abstract")
    evidence = artifact("anchor", "evidence_anchor", material_id=abstract.artifact_id, quoted_text="A" * 1000)
    read = artifact("read", "react_observation", source_views=[{
        "material_id": abstract.artifact_id, "content": "abstract text", "content_complete": True}])
    snapshot = SimpleNamespace(artifacts={a.artifact_id: a for a in (abstract, evidence, read)})
    view = ObservationView(snapshot)
    card = translate_references(view.card(abstract, limit=5), {abstract.artifact_id: "M0001"})
    assert card["truncated"]
    assert card["paper_access"]["full_text_status"] == "not_confirmed"
    assert card["paper_access"]["next_tool"] == "read_source_details"
    assert card["paper_access"]["input_artifact_ids"] == ["M0001"]
    assert view.content(read)["source_views"][0]["paper_access"]["current_content"] == "abstract_only"
    assert "paper_access" not in view.card(evidence)


def test_existing_fulltext_routes_to_reading_and_failed_attempt_stays_explicit():
    from test_reading_ledger import artifact
    abstract = artifact("abstract", "evidence_material", provider_id="pubmed",
                        source_record_id="pmid:123", material_type="literature_abstract")
    attempt = artifact("failed", "react_observation", articles=[{
        "source_record_id": "pmid:123", "acquisition_status": "not_acquired"}])
    snapshot = SimpleNamespace(artifacts={a.artifact_id: a for a in (abstract, attempt)})
    assert ObservationView(snapshot).card(abstract)["paper_access"]["full_text_status"] == "not_acquired_on_last_attempt"
    fulltext = artifact("fulltext", "evidence_material", provider_id="pubmed",
                        source_record_id="pmid:123", material_type="literature_full_text")
    snapshot.artifacts[fulltext.artifact_id] = fulltext
    access = ObservationView(snapshot).card(abstract)["paper_access"]
    assert access["full_text_status"] == "saved"
    assert access["next_tool"] == "read_artifacts"
    assert access["input_artifact_ids"] == ["fulltext"]
    assert "paper_access" not in ObservationView(snapshot).card(fulltext)


def test_latest_large_read_is_complete_structured_and_aliasable():
    body = "Evidence " * 4000 + "THE FINAL RESULT"
    payload = {"source_views": [{"material_id": "material.long", "content": body,
                                "content_complete": True}]}
    artifact = SimpleNamespace(artifact_type="react_observation", payload=payload)
    snapshot = SimpleNamespace(artifacts={"old": artifact, "latest": artifact})
    steps = [{"sequence": 1, "result_ids": ["old"]}, {"sequence": 2, "result_ids": ["latest"]}]
    result = ObservationView(snapshot).recent(steps, max_chars=16000)
    visible = translate_references(result, {"material.long": "M0001"})
    assert visible[0]["payload"]["source_views"][0]["content"] == body
    assert visible[0]["payload"]["source_views"][0]["material_id"] == "M0001"
    assert visible[1]["content_omitted"]
    assert "excerpt" not in visible[1]
    assert "not unread" in visible[1]["instruction"]


def test_all_latest_outputs_survive_budget():
    snapshot = SimpleNamespace(artifacts={k: SimpleNamespace(artifact_type="react_observation",
        payload={"message": "x" * 100}) for k in ("a", "b")})
    result = ObservationView(snapshot).recent([{"sequence": 1, "result_ids": ["a", "b"]}], max_chars=1)
    assert all("payload" in observation for observation in result)


def test_default_restores_outputs_older_than_three_steps_without_clipping():
    artifacts = {str(i): SimpleNamespace(artifact_type="react_observation",
        payload={"source_views": [{"material_id": f"material.{i}", "content": "x" * 18000}]})
        for i in range(6)}
    steps = [{"sequence": i, "action": "read_artifacts", "result_ids": [str(i)]} for i in range(6)]
    result = ObservationView(SimpleNamespace(artifacts=artifacts)).recent(steps)
    assert len(result) == 6
    assert all(len(v["payload"]["source_views"][0]["content"]) == 18000 for v in result)
    assert [v["step_sequence"] for v in result] == [5, 4, 3, 2, 1, 0]
    # Reconstructing the same snapshot after a pause yields the same conversation.
    assert ObservationView(SimpleNamespace(artifacts=artifacts)).recent(steps) == result
