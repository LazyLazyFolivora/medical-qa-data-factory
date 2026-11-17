from types import SimpleNamespace

from medical_qa_data_factory.generation.react_observations import ObservationView


def artifact(aid, kind, **payload):
    return SimpleNamespace(artifact_id=aid, artifact_type=kind, payload=payload, metadata={})


def test_download_preview_index_failure_and_model_note_do_not_count_as_reading():
    paper = artifact("paper", "evidence_material", provider_id="pubmed",
                     source_record_id="123", material_type="full_text")
    abstract = artifact("abstract", "evidence_material", provider_id="pubmed",
                        source_record_id="456", material_type="abstract")
    sources = {a.artifact_id: a for a in [paper, abstract,
        artifact("anchor", "evidence_anchor", material_id="paper", quoted_text="x" * 1000),
        artifact("index", "react_observation", source_views=[{
            "material_id": "paper", "content": "", "paragraph_index": [{"paragraph_id": "p1"}]}]),
        artifact("failed", "react_observation", source_views=[{
            "material_id": "paper", "content": "partial", "read_error": "failed", "content_complete": True}]),
        artifact("note", "react_step", research_note="Read both full papers") ]}
    view = ObservationView(SimpleNamespace(artifacts=sources))
    assert all(not m["returned_paragraph_ids"] and not m["whole_stored_content_returned"]
               for m in view.reading_ledger()["materials"])
    card = view.card(paper, limit=5)
    assert card["truncated"] and card["source_record_id"] == "123" and card["material_type"] == "full_text"
    sources["read"] = artifact("read", "react_observation", source_views=[{
        "material_id": "paper", "content": "paragraph", "excerpts": [{"paragraph_id": "p2"}],
        "content_complete": False}])
    entries = {m["material_id"]: m for m in view.reading_ledger()["materials"]}
    assert entries["paper"]["returned_paragraph_ids"] == ["p2"]
    assert not entries["paper"]["whole_stored_content_returned"]
    assert entries["abstract"]["returned_paragraph_ids"] == []


def test_observation_projection_keeps_one_original_and_preserves_unread_index():
    import json
    text = "Exact original paragraph, with conditions and negation."
    observation = artifact("read", "react_observation", source_views=[{
        "material_id": "paper", "material_type": "literature_full_text", "content": text,
        "excerpts": [{"paragraph_id": "p1", "offset": 100, "text": text}],
        "returned_paragraph_ids": ["p1"], "content_complete": False,
        "paragraph_index": [{"paragraph_id": "p1", "preview": text},
                            {"paragraph_id": "p2", "preview": "Unread methods"}]}])
    projected = ObservationView(SimpleNamespace(artifacts={})).content(observation)
    assert json.dumps(projected).count(text) == 1
    assert projected["source_views"][0]["excerpts"][0] == {"paragraph_id": "p1", "offset": 100}
    assert projected["source_views"][0]["paragraph_index"][1]["preview"] == "Unread methods"
    assert observation.payload["source_views"][0]["excerpts"][0]["text"] == text
