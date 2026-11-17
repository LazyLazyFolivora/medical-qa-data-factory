import json
from types import SimpleNamespace

import pytest

from medical_qa_data_factory.generation.evidence_provider import (
    EvidenceProviderCapability,
    EvidenceProviderResult,
    ProviderTerminalStatus,
)
from medical_qa_data_factory.generation.objective_training_export import export_training_release


@pytest.mark.parametrize("accept, exported", [(True, 1), (False, 0)])
def test_training_export_retrieves_before_stopping_and_requires_review(tmp_path, accept, exported):
    public = tmp_path / "public"
    audit = tmp_path / "audit"
    public.mkdir()
    audit.mkdir()
    question = "What outcomes did Trial A record, and how long did follow-up last?"
    suggestion = {"query": "Trial A outcomes follow-up", "sources": ["PubMed"],
                  "expected_evidence": "Reported outcomes and follow-up duration.",
                  "stop_condition": "Find both the outcomes and follow-up duration."}
    (public / "questions.jsonl").write_text(json.dumps({
        "question_id": "draft.a", "question": question, "answer": ["Outcome X; one year."],
        "suggested_retrieval": suggestion,
    }), encoding="utf-8")
    (audit / "validation_reports.jsonl").write_text(json.dumps({
        "report_id": "report.a", "question_id": "draft.a", "verdict": "PASS",
        "release_allowed": True, "primary_reason": "PASS",
        "public_question_audit_id": "audit.a", "answer_audit_id": "audit.a",
        "evidence_used": ["pubmed:1"], "validator_version": "test", "input_digest": "a" * 64,
    }), encoding="utf-8")
    material_id = "material.pubmed.1"

    class Provider:
        capability = EvidenceProviderCapability.read_only(provider_id="pubmed")

        def result(self, request, material_type, text):
            return EvidenceProviderResult(
                request=request, terminal_status=ProviderTerminalStatus.SUCCEEDED,
                material_ids=[material_id], raw_content_by_material_id={material_id: text},
                materials=[{"material_id": material_id, "source_record_id": "pmid:1",
                            "material_type": material_type, "content_hash": material_type}],
            )

        def retrieve(self, request):
            assert request.query == {"term": "Trial A"}
            return self.result(request, "literature_abstract", "Trial A measured Outcome X.")

        def retrieve_details(self, request, *, material_ids):
            assert material_ids == [material_id]
            return self.result(request, "literature_full_text", "Trial A measured Outcome X for one year.")

    class Model:
        def generate_structured(self, request, schema):
            assert request.payload["question"] == question
            if request.role == "objective_training_trace_review":
                assert request.payload["verified_answer"]
                assert len(request.payload["steps"]) == 3
                return SimpleNamespace(parsed={"accepted": accept, "supporting_material_ids": [material_id],
                                               "reason": "Checked final evidence."})
            assert "verified_answer" not in request.payload
            assert "suggested_retrieval" not in request.payload
            evidence = request.payload["evidence"]
            if not evidence:
                assert request.payload["previous_steps"] == []
                decision = {"action": "search", "query": "Trial A"}
            elif evidence[0]["material_type"] == "literature_abstract":
                decision = {"action": "read_full_text", "material_id": material_id}
            else:
                assert "one year" in evidence[0]["text"]
                decision = {"action": "evidence_sufficient"}
            return SimpleNamespace(parsed={**decision, "reason": "Inspect the available evidence."})

    output = tmp_path / "train.jsonl"
    summary = export_training_release(tmp_path, output, Model(), Provider(), max_searches=2)
    assert summary["exported"] == exported
    if accept:
        row = json.loads(output.read_text(encoding="utf-8"))
        assert row["question"] == question and row["answer_sources"] == ["pubmed:1"]
        assert row["suggested_retrieval"] == suggestion
        assert row["trajectory"][0]["visible_material_ids"] == []
        assert row["trajectory"][-1]["visible_material_ids"] == [material_id]
        assert row["trajectory"][1]["result"]["new_evidence"][0]["material_type"] == "literature_full_text"
    else:
        assert output.read_text(encoding="utf-8") == ""
