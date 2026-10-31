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


def test_nested_usage_payload_is_never_exported_and_is_counted_unknown():
    import pytest
    for bad in [{"private_prompt":"DO_NOT_EXPORT"},["DO_NOT_EXPORT"],float("nan"),float("inf"),-2,True]:
        view=saved_view();view["calls"][0]["receipt"]["usage"]={"total_tokens":bad}
        out=export_radar_diagnostics(view)
        assert "DO_NOT_EXPORT" not in str(out)
        assert out["model_usage"]["unknown_usage_calls"]==1
        assert out["model_usage"]["known_total_tokens"]==0
        assert out["model_usage"]["total_tokens"] is None
        assert out["model_stages"]["read_plan"]["total_tokens"] is None
        assert "invalid_model_usage_field:c1:total_tokens" in out["usage_issues"]


def test_conflicting_duplicate_does_not_claim_first_tokens_as_lower_bound():
    view=saved_view();view["calls"][0]["receipt"]["usage"]["total_tokens"]=100
    second=copy.deepcopy(view["calls"][0]);second["receipt"]["usage"]["total_tokens"]=1
    view["calls"].append(second)
    out=export_radar_diagnostics(view)
    assert out["model_usage"]["calls"]==1
    assert out["model_usage"]["known_total_tokens"]==0
    assert out["model_usage"]["unknown_usage_calls"]==1
    assert out["model_stages"]["read_plan"]["known_total_tokens"]==0
    assert out["model_stages"]["read_plan"]["total_tokens"] is None
    assert "conflicting_model_call_id:c1" in out["usage_issues"]


def test_cross_stage_conflict_marks_each_original_stage_incomplete():
    view=saved_view()
    view["calls"]=[{"call_id":"read-known","stage":"read","status":"received","receipt":{"usage":{"total_tokens":5}}},
                   {"call_id":"write-known","stage":"write","status":"received","receipt":{"usage":{"total_tokens":7}}},
                   {"call_id":"conflict","stage":"read","status":"received","receipt":{"usage":{"total_tokens":100}}},
                   {"call_id":"conflict","stage":"write","status":"received","receipt":{"usage":{"total_tokens":1}}}]
    out=export_radar_diagnostics(view)
    assert out["model_usage"]["calls"]==3
    assert out["model_usage"]["known_total_tokens"]==12
    assert out["model_usage"]["unknown_usage_calls"]==1
    for stage,lower in [("read",5),("write",7)]:
        assert out["model_stages"][stage]["known_total_tokens"]==lower
        assert out["model_stages"][stage]["total_tokens"] is None
        assert out["model_stages"][stage]["unknown_usage_calls"]==1
        assert out["model_stages"][stage]["ambiguous_call_ids"]==["conflict"]


# Matches BioCheck core.verify()/support_audit() native result structure:
# verify.units[*].claims[*], paragraph sentence_numbers, located evidence.version.
def claim_view():
    view = saved_view()
    located = {"evidence_id": "E1", "version": "native-v1", "segment_id": "S2",
               "quote": "SOURCE_SHORT_QUOTE", "start": 4, "end": 22}
    unit = {"unit_id": "U1", "text": "PRIVATE_UNIT_BODY", "verdict": "insufficient",
            "claims": [{"claim_id": "native-claim", "claim_index": 0,
                        "span": "PRIVATE_CLAIM_BODY", "verdict": "insufficient",
                        "sentence_numbers": [1, 2], "evidence": [located]}],
            "issues": [{"issue_index": 0, "labels": ["REF"], "sentence_numbers": [1],
                        "span": "PRIVATE_ISSUE_SPAN", "reason": "invalid selected segment"}]}
    view["result"]["report"] = {"status": "REVIEW", "verify": {
        "status": "REVIEW", "draft": "PRIVATE_DRAFT", "units": [unit],
        "verification_errors": [{"unit_id": "U1", "errors": ["source_segment_invalid"]}],
        "raw_response": {"reason": "PRIVATE_RAW_OUTPUT"}},
        "support_audit": {"status": "REVIEW", "checks": [{"unit_id": "U1", "claim_index": 0,
            "verdict": "uncertain", "support": [located],
            "validation": {"code": "support_quote_not_in_segment", "field_path": "/support/0/quote"}}]}}
    return view


def test_native_claim_verification_identity_and_selected_evidence_are_retained():
    out = export_radar_diagnostics(claim_view())
    rows = {row["path"]: row["values"] for row in out["diagnostic_rows"]}
    root = "/result/report/verify/units/0"
    assert rows[root]["unit_id"] == "U1"
    assert rows[root]["verdict"] == "insufficient"
    assert rows[root + "/claims/0"]["claim_id"] == "native-claim"
    assert rows[root + "/claims/0"]["claim_index"] == 0
    assert rows[root + "/claims/0"]["sentence_numbers"] == [1, 2]
    assert rows[root + "/claims/0/evidence/0"] == {
        "evidence_id": "E1", "version": "native-v1", "segment_id": "S2", "start": 4, "end": 22}
    assert rows["/result/report/verify/verification_errors/0"]["errors"] == ["source_segment_invalid"]
    audit = "/result/report/support_audit/checks/0"
    assert rows[audit]["claim_index"] == 0
    assert rows[audit]["verdict"] == "uncertain"
    assert rows[audit + "/validation"]["code"] == "support_quote_not_in_segment"
    assert out["native_report_status"] == "REVIEW"


def test_claim_containers_do_not_export_source_quote_draft_or_raw_response():
    import json
    view = claim_view()
    view["result"]["report"]["verify"]["units"][0]["claims"][0]["payload"] = {
        "reason": "PRIVATE_PAYLOAD", "evidence": [{"evidence_id": "PRIVATE_HIDDEN_ID"}]}
    rendered = json.dumps(export_radar_diagnostics(view))
    for forbidden in ["SOURCE_SHORT_QUOTE", "PRIVATE_UNIT_BODY", "PRIVATE_CLAIM_BODY", "PRIVATE_DRAFT",
                      "PRIVATE_ISSUE_SPAN", "PRIVATE_RAW_OUTPUT", "PRIVATE_PAYLOAD", "PRIVATE_HIDDEN_ID"]:
        assert forbidden not in rendered


def test_missing_native_claim_fields_are_reported_without_filling_identity_or_verdict():
    view = claim_view()
    del view["result"]["report"]["verify"]["units"][0]["claims"][0]["verdict"]
    out = export_radar_diagnostics(view)
    missing = out["missing_diagnostic_fields"]
    assert {"path": "/result/report/verify/units/0/claims/0", "fields": ["verdict"]} in missing
    row = next(row for row in out["diagnostic_rows"] if row["path"].endswith("/claims/0"))
    assert "verdict" not in row["values"]
    del view["result"]["report"]
    absent = export_radar_diagnostics(view)
    assert absent["diagnostic_presence"]["report"] == "absent"
    assert absent["diagnostic_presence"]["claim_checks"] == "absent"


def test_new_report_round_calls_and_unsent_reservations_keep_exact_unique_usage():
    view = saved_view()
    view["calls"] += [
        {"call_id": "new-round-write", "stage": "write", "status": "received",
         "receipt": {"usage": {"total_tokens": 23}}},
        {"call_id": "new-round-check", "stage": "verify_paragraph", "status": "failed", "receipt": {}},
        {"call_id": "reserved", "stage": "verify_paragraph", "status": "prepared", "receipt": {}},
        {"call_id": "unsent", "stage": "write", "status": "failed",
         "receipt": {"status": "not_sent", "usage": {"total_tokens": 999}}},
    ]
    out = export_radar_diagnostics(view)
    assert out["model_usage"] == {"calls": 3, "known_total_tokens": 40,
                                  "unknown_usage_calls": 1, "total_tokens": None}
    assert out["model_stages"]["write"]["known_total_tokens"] == 23
    assert out["model_stages"]["verify_paragraph"]["unknown_usage_calls"] == 1
    assert {row["call_id"] for row in out["model_calls"]} == {
        "c1", "new-round-write", "new-round-check", "reserved", "unsent"}


def test_actual_native_biocheck_projection_fixture_keeps_errors_without_raw_text():
    import json
    from pathlib import Path
    fixture = json.loads((Path(__file__).parent / "fixtures/biocheck_native_diagnostics.json").read_text())
    out = export_radar_diagnostics(fixture["view"])
    rows = {row["path"]: row["values"] for row in out["diagnostic_rows"]}
    claim = "/result/report/verification/units/0/claims/0"
    assert rows[claim]["claim_index"] == 0
    assert rows[claim]["sentence_numbers"] == [1]
    assert rows[claim + "/evidence/0"]["version"] == "v-synthetic"
    assert rows[claim + "/evidence/0"]["segment_id"] == "S1"
    error = "/result/report/support_audit/verification_errors/0"
    assert rows[error]["unit_id"] == "U1" and rows[error]["claim_index"] == 0
    assert "复核摘录不在本条所选片段中" in rows[error]["reason"]
    assert out["native_report_status"] == "REVIEW"
    assert out["missing_diagnostic_fields"] == []
    rendered = json.dumps(out)
    for forbidden in ["SOURCE_SHORT_QUOTE", "PRIVATE_CLAIM_BODY", "NOT_IN_SELECTED_SOURCE"]:
        assert forbidden not in rendered
