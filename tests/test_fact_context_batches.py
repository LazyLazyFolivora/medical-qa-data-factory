import pytest
from medical_qa_data_factory.generation.fact_exploration import _generate_fact_batch, CandidateClaimBatch
from medical_qa_data_factory.generation.subagent_contracts import RoleContractViolation
from medical_qa_data_factory.llm.contracts import StructuredGenerationRequest, StructuredGenerationResult
from medical_qa_data_factory.llm.errors import ModelError, ModelErrorCode
from test_objective_subagent_roles import _claim
from medical_qa_data_factory.generation.fact_exploration import _project_ensembl_fields
from copy import deepcopy


def request():
    return StructuredGenerationRequest(invocation_id="facts", role="fact_understanding", system_prompt_version="v1", idempotency_key="facts",
        payload={"trust_boundary": {"materials": [{"material_id": str(i), "content": "whole source " * 2000} for i in range(4)]},
                 "evidence_anchors": [{"material_id": str(i), "anchor_id": f"a{i}", "quoted_text": "whole quote " * 2000,
                                       "start_offset": 0, "end_offset": 24000} for i in range(4)]})


def test_fact_batches_keep_all_materials_and_exact_anchors():
    seen = []
    class Model:
        def generate_structured(self, r, schema):
            materials = r.payload["trust_boundary"]["materials"]
            if len(materials) > 1:
                raise ModelError(ModelErrorCode.BAD_REQUEST, "context budget exceeded", retryable=False)
            seen.append(r)
            mid = materials[0]["material_id"]
            assert materials[0]["content"] == "whole source " * 2000
            assert r.payload["evidence_anchors"] == [request().payload["evidence_anchors"][int(mid)]]
            claim = _claim().model_copy(update={"claim_id": f"c{mid}", "supporting_anchor_ids": [f"a{mid}"], "contradicting_anchor_ids": []})
            return StructuredGenerationResult(invocation_id=r.invocation_id, role=r.role,
                parsed=CandidateClaimBatch(claims=[claim]).model_dump(mode="json"), usage={"input_tokens": 5}, model_config_id="test")
    result = _generate_fact_batch(Model(), request(), CandidateClaimBatch)
    assert len(result.parsed["claims"]) == 4
    assert result.usage["fact_batches"] == 4
    assert result.usage["input_tokens"] == 20
    assert len({r.idempotency_key for r in seen}) == 4


def test_single_oversized_material_is_not_truncated_or_silently_skipped():
    class Model:
        def generate_structured(self, *args):
            raise ModelError(ModelErrorCode.BAD_REQUEST, "context budget exceeded", retryable=False)
    with pytest.raises(ModelError, match="context budget exceeded"):
        _generate_fact_batch(Model(), request(), CandidateClaimBatch)


def test_auth_error_is_not_treated_as_context_overflow():
    class Model:
        def generate_structured(self, *args):
            raise ModelError(ModelErrorCode.AUTH_ERROR, "unauthorized")
    with pytest.raises(ModelError, match="unauthorized"):
        _generate_fact_batch(Model(), request(), CandidateClaimBatch)


def test_evidence_quote_preserves_boundary_whitespace_and_offsets():
    from medical_qa_data_factory.generation.objective_question_contracts import EvidenceAnchor, text_digest
    quote = "  NRF1 observation.\n"
    anchor = EvidenceAnchor(anchor_id="a", material_id="m", document_id="d", section_or_field="abstract",
        start_offset=10, end_offset=10 + len(quote), quoted_text=quote, quote_hash=text_digest(quote))
    assert anchor.quoted_text == quote
    assert anchor.end_offset - anchor.start_offset == len(quote)


def test_large_source_batch_is_split_before_model_call():
    r = request()
    r.payload["trust_boundary"]["materials"] = [{"material_id": str(i), "content": "source"} for i in range(10)]
    r.payload["evidence_anchors"] = []
    seen = []
    class Model:
        def generate_structured(self, child, schema):
            ids = [m["material_id"] for m in child.payload["trust_boundary"]["materials"]]
            assert len(ids) <= 5
            seen.extend(ids)
            return StructuredGenerationResult(invocation_id=child.invocation_id, role=child.role,
                parsed={"claims": []}, usage={}, model_config_id="test")
    result = _generate_fact_batch(Model(), r, CandidateClaimBatch)
    assert seen == [str(i) for i in range(10)]
    assert result.usage["fact_batches"] == 2


def test_expanded_ensembl_record_uses_exact_field_evidence():
    record = {"material_id": "gene", "anchor_id": "record", "section_or_field": "record",
              "quoted_text": "x" * 180000, "start_offset": 0, "end_offset": 180000}
    field = {"material_id": "gene", "anchor_id": "name", "section_or_field": "display_name",
             "quoted_text": '"MAN1B1"', "start_offset": 170000, "end_offset": 170008}
    payload = {"trust_boundary": {"materials": [{"material_id": "gene",
        "material_type": "ensembl_gene_record", "content": record["quoted_text"],
        "anchor_ids": ["record", "name"]}]}, "evidence_anchors": [record, field]}
    original = deepcopy(payload)
    _project_ensembl_fields(payload)
    assert payload["evidence_anchors"] == [original["evidence_anchors"][1]]
    material = payload["trust_boundary"]["materials"][0]
    assert material["content"] == 'display_name: "MAN1B1"'
    assert material["anchor_ids"] == ["name"]
    assert "not the complete" in material["content_scope"]
    assert len(record["quoted_text"]) == 180000


def test_projection_preserves_articles_and_records_without_gene_fields():
    for kind in ("pubmed_abstract", "ensembl_gene_record"):
        payload = {"trust_boundary": {"materials": [{"material_id": "m", "material_type": kind,
            "content": "complete", "anchor_ids": ["a"]}]}, "evidence_anchors": [{
                "material_id": "m", "anchor_id": "a", "section_or_field": "record", "quoted_text": "complete"}]}
        original = deepcopy(payload)
        _project_ensembl_fields(payload)
        assert payload == original
