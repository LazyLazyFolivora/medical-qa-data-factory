"""Synthetic boundary inputs; no benchmark text or expected case answers."""
import copy
from medical_qa_data_factory.evaluation.radar_routes import export_radar_diagnostics, evaluate_radar_view


def saved_view():
    return {"id":"a1", "state":"blocked", "created_at":"2026-10-31T01:00:00Z", "error_code":"analysis_passage_invalid",
      "question":"PRIVATE_QUESTION", "evidence":[{"text":"SOURCE_FULL_BODY"}],
      "calls":[{"call_id":"c1","stage":"read_plan","status":"received","request":{"prompt":"PRIVATE_PROMPT"},
                "receipt":{"usage":{"total_tokens":17},"response":{"text":"PRIVATE_MODEL_OUTPUT"}}}],
      "queries":[{"index":0,"action_id":"search1","kind":"document_search","provider":"fabric","status":"read",
        "plan":{"source_id":"source1","terms":["specific query"]},"pagination":{"has_more":True,"page_number":2,"next_cursor":"cursor2","max_seq":8}}],
      "progress":{"narrative_extractions":{"k":{"record_ref":{"source_id":"source1","source_record_id":"r1","version_id":"v1"},
        "gaps":[{"observation_index":3,"stage":"period","reason":"period_scope_missing","rule_id":"literal-period"}],
        "observations":[{"value":999,"field_locators":{"quote":"SOURCE_QUOTE"}}]}},
        "repair":{"trigger_reason":"analysis_passage_invalid","attempt":1,"error_code":"bad_locator"}},
      "result":{"report":{"status":"FAIL","text":"NATIVE_REPORT_BODY","check":{"issues":[{"sentence_index":2,"issue_id":"i1","issue":"locator mismatch","rule_id":"exact-source"}]}}}}


def test_failed_view_exports_diagnosis_without_prompt_source_or_fact_values():
    view=saved_view(); before=copy.deepcopy(view)
    out=export_radar_diagnostics(view, scenario="C", phase="smoke")
    rendered=str(out)
    for value in ["analysis_passage_invalid","specific query","cursor2","period_scope_missing","exact-source","locator mismatch","source1","r1","v1"]:
        assert value in rendered
    for value in ["PRIVATE_QUESTION","SOURCE_FULL_BODY","PRIVATE_PROMPT","PRIVATE_MODEL_OUTPUT","SOURCE_QUOTE","NATIVE_REPORT_BODY","999"]:
        assert value not in rendered
    assert out["native_report_status"]=="FAIL"
    assert out["business_acceptance"]=="not_assessed"
    assert out["model_usage"]["known_total_tokens"]==17
    assert out["model_stages"]["read_plan"]["calls"]==1
    assert view==before


def test_absent_receipts_and_usage_remain_unknown_not_zero_pass():
    view=saved_view(); view["calls"].append({"call_id":"c2","stage":"write","status":"unconfirmed","receipt":{}})
    out=export_radar_diagnostics(view)
    assert out["model_usage"]["unknown_usage_calls"]==1
    assert out["model_usage"]["total_tokens"] is None
    assert out["model_stages"]["write"]["known_total_tokens"]==0
    assert out["model_stages"]["write"]["total_tokens"] is None
    del view["calls"]
    assert export_radar_diagnostics(view)["model_usage"] is None


def test_duplicate_call_identity_is_explicit_and_cannot_inflate_known_total():
    view=saved_view(); view["calls"].append(copy.deepcopy(view["calls"][0]))
    out=export_radar_diagnostics(view)
    assert out["model_usage"]["calls"]==1
    assert out["model_usage"]["known_total_tokens"]==17
    assert out["model_usage"]["total_tokens"] is None
    assert "duplicate_model_call_id:c1" in out["usage_issues"]


def test_unsupported_trace_keeps_diagnostics_in_import():
    out=evaluate_radar_view(saved_view(),provenance="synthetic_fixture",trace_validator=lambda **kw: {},validator_version="test")
    assert out["integrity"]["status"]=="not_evaluable"
    assert out["diagnostics"]["analysis_error_code"]=="analysis_passage_invalid"


def test_cli_uses_same_projection_and_preserves_existing_output(tmp_path, capsys):
    import json
    from argparse import Namespace
    from medical_qa_data_factory.evaluation.kdd_routes_cli import command
    source=tmp_path/"saved.json"; source.write_text(json.dumps(saved_view()))
    out=tmp_path/"diagnostics.jsonl"
    args=Namespace(route_mode="export-radar-diagnostics",views=[str(source)],scenario="C",phase="smoke",out=str(out))
    assert command(args)==0
    result=json.loads(out.read_text())
    assert result["phase"]=="smoke" and result["scenario"]=="C"
    assert result["source_file_sha256"]
    before=out.read_bytes()
    import pytest
    with pytest.raises(FileExistsError): command(args)
    assert out.read_bytes()==before


def test_untrusted_nested_payloads_cannot_smuggle_body_into_diagnostics():
    view=saved_view()
    view["progress"]["arbitrary_blob"]={"reason":"SOURCE_SMUGGLED"}
    view["queries"][0]["payload"]={"terms":["SOURCE_SMUGGLED"]}
    view["result"]["report"]["text"]={"reason":"SOURCE_SMUGGLED"}
    assert "SOURCE_SMUGGLED" not in str(export_radar_diagnostics(view))
