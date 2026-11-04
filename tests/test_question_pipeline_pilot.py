"""Pipeline handoff and stop behavior, without external services."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from medical_qa_data_factory import question_pipeline_pilot as pipeline
from medical_qa_data_factory.fact_pair_pilot import FactPair
from medical_qa_data_factory.focused_question_pilot import Delivery, Selection
from medical_qa_data_factory.graph_question_pilot import Exploration, Question


def pair(proposal="pair"):
    return FactPair(
        observation={"fact": "R overexpression inhibits A-induced death", "source": "PMID:1", "quote": "R inhibits A"},
        added_fact={"fact": "R regulates permeability", "source": "UniProt:R", "quote": "regulates permeability"},
        information_gain="Unsupported causal interpretation must not reach writer",
        reader_need="Untrusted explanation", proposal=proposal,
    )


def delivery():
    return Delivery(question=Question(
        question="Which protein and function?", answer="R; regulates permeability",
        operation="connection", evidence_needs=[], dependency="identify then lookup",
        completion="both facts", difficulty="unmeasured", shortcut="unknown"), gap="")


@pytest.mark.parametrize("proposal,skill", [("pair", "pilot-focused-composition"),
                                          ("single", "pilot-single-composition")])
def test_writer_routing_and_evidence_only_handoff(monkeypatch, tmp_path, proposal, skill):
    seen = []
    def invoke(*args, **kwargs):
        seen.append((args, kwargs))
        return delivery()
    monkeypatch.setattr(pipeline, "invoke", invoke)
    metadata = [{"source": "UniProt:R", "organism": "Human"}]
    result = pipeline.compose_pair(object(), tmp_path, pair(proposal), "Need", metadata, Path("frozen"))
    args, kwargs = seen[0]
    assert args[3] == skill
    assert "Unsupported causal" not in json.dumps(args[4])
    assert "Untrusted explanation" not in json.dumps(args[4])
    assert args[4]["source_metadata"] == metadata
    assert "property_source_species" not in args[4]["evidence_packet"]
    if proposal == "pair":
        assert kwargs["skills_root"] == Path("frozen")
    else:
        assert "property_evidence" not in args[4]["evidence_packet"]
    assert result["status"] == "draft"


def test_insufficient_stops_without_writer(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("Insufficient evidence must not invoke writer")
    monkeypatch.setattr(pipeline, "invoke", forbidden)
    result = pipeline.compose_pair(object(), tmp_path, pair("insufficient"), "Need", [], tmp_path)
    assert result["status"] == "needs_material"
    assert result["question"] is None


def test_entity_to_question_orchestration(monkeypatch, tmp_path):
    fetched = []
    def retrieve(entity):
        fetched.append(entity)
        return {"entity": entity, "source_url": f"https://example.test/{entity}",
                "record": {"primaryAccession": entity, "organism": {"scientificName": "Human"}, "comments": []}}
    monkeypatch.setattr(pipeline, "retrieve", retrieve)
    monkeypatch.setattr(pipeline, "PubMedClient", lambda **kwargs: object())
    monkeypatch.setattr(pipeline, "search_round", lambda *args: [
        {"material": {"fetch": {"records": [{"pmid": "1", "abstract": "R inhibits A"}]}}}])
    stages = []
    def invoke(model, output, stage, skill, payload, schema, **kwargs):
        stages.append((stage, kwargs))
        if stage == "exploration":
            return Exploration(directions=["function"], searches=[])
        if stage == "selection":
            return Selection(assignment={
                "information_need": "Find regulator and function", "relation": pair().observation,
                "source_pmid": "1", "lookup_entity": "R", "lookup_property": "function",
                "connection_value": "context", "question_outline": "Which regulator?"}, reason="supported")
        if stage == "fact-pair":
            return pair()
        return delivery()
    monkeypatch.setattr(pipeline, "invoke", invoke)
    output = tmp_path / "run"
    result = pipeline.run(SimpleNamespace(entity="A", output=output, graph=None), object())
    assert fetched == ["A", "R"]
    assert [stage for stage, _ in stages] == ["exploration", "selection", "fact-pair", "delivery"]
    assert all(kwargs["skills_root"] == pipeline.BASELINE / "skills" for _, kwargs in stages[-2:])
    assert result["status"] == "draft"
    assert (output / "review.md").exists()


def test_frozen_skill_tampering_is_detected(tmp_path):
    manifest = json.loads((pipeline.BASELINE / "manifest.json").read_text(encoding="utf-8"))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "skills/pilot-fact-pair/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Frozen skill changed"):
        pipeline.frozen_skills(tmp_path)
