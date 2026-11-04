import json

import pytest

from medical_qa_data_factory.generation.answer_verification_subagent import BlindSolveResult
from medical_qa_data_factory.generation.verification_evidence_view import generate_with_evidence
from medical_qa_data_factory.llm.contracts import StructuredGenerationRequest
from medical_qa_data_factory.llm.fake import FakeStructuredModel


def run_case(callback, question, *, stage="blind_solve"):
    transcripts = [{"id": tid, "Exon": [{"id": eid, "start": 123456} for eid in ids]}
                   for tid, ids in [("T1", ["shared", "A"]), ("T2", ["shared", "B"])]]
    record = {"gene": "ZRSR2", "chromosome": "X", "Transcript": transcripts, "notes": "detail " * 1000}
    anchors = [{"anchor_id": "a.record", "document_id": "ensembl:gene", "quoted_text": json.dumps(record)}]
    field = "evidence_anchors" if stage == "counterexample_review" else "independent_evidence"
    data = {"anchors": anchors} if stage == "answer_evidence_audit" else anchors
    role = "objective_release_review" if stage == "answer_evidence_audit" else "objective_answer_verification"
    model = FakeStructuredModel(responses={role: callback}, input_tokens=10, output_tokens=5)
    request = StructuredGenerationRequest(invocation_id="verify", role=role, system_prompt_version="test",
        payload={"stage": stage, "question": question, field: data}, idempotency_key="test")
    result = generate_with_evidence(model, request, BlindSolveResult, field)
    assert request.payload[field] == data
    return model, result


@pytest.mark.parametrize("stage", ["blind_solve", "counterexample_review", "answer_evidence_audit"])
def test_location_question_gets_values_and_can_identify_missing_band_without_reading_exons(stage):
    def verify(request):
        payload = json.dumps(request.payload)
        assert "ensembl:gene" in payload and '"chromosome": "X"' in payload
        assert "read_ref" in payload and "123456" not in payload and '"shared"' not in payload
        return {"step": {"query_id": "q", "uncertain": True, "evidence_need": "cytogenetic band"}}
    model, result = run_case(verify, "What is the cytogenetic band?", stage=stage)
    assert result.parsed["uncertain"] and len(model.invocations) == 1


def test_compare_exons_and_open_the_underlying_source_rows():
    def verify(request):
        pages = request.payload["evidence_pages"]
        if not pages:
            fields = request.payload["independent_evidence"][0]["excerpt"]["fields"]
            return {"step": {"transcript_list_ref": fields["Transcript"]["read_ref"],
                             "first_transcript_id": "T1", "second_transcript_id": "T2"}}
        comparison = next(iter(pages.values()))
        assert comparison["anchor_id"] == "a.record" and comparison["distinct_exon_counts"] == [2, 2]
        assert comparison["shared_count"] == 1
        row = comparison["shared_rows"]["fields"]["0"]
        assert row["id"] == "shared"
        if len(pages) == 1:
            return {"step": {"read_refs": [row["read_ref"]]}}
        first = pages[row["read_ref"]]["fields"]["first"]["read_ref"]
        if first not in pages:
            return {"step": {"read_refs": [first]}}
        assert pages[first]["fields"] == {"id": "shared", "start": 123456}
        return {"query_id": "q", "answer_values": ["shared"]}
    _, result = run_case(verify, "Which exon IDs do transcripts T1 and T2 share?")
    assert result.usage["model_calls"] == 4 and result.usage["input_tokens"] == 40
    assert result.parsed["answer_values"] == ["shared"]


def test_hidden_or_invalid_reference_never_resolves():
    def verify(request):
        assert not request.payload["evidence_pages"]
        if request.invocation_id.endswith("view0"):
            return {"step": {"read_refs": ["hidden-discovery-anchor#/secret@0"]}}
        return {"query_id": "q", "uncertain": True, "evidence_need": "independent record"}
    model, result = run_case(verify, "Which field supports the answer?")
    assert len(model.invocations) == 2 and result.parsed["uncertain"]
