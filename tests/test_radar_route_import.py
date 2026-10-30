"""Native-shape fixtures test the importer; they are not real Radar outcomes."""
import copy

import pytest

from medical_qa_data_factory.evaluation.radar_routes import evaluate_radar_view


def native_view():
    action = {"id": "search-1", "kind": "document_search", "arguments": {"provider": "fabric", "source_id": "epmc", "terms": ["candidate"]},
              "covers": ["evidence"], "estimated_cost": {"tool_calls": 1}}
    receipt = {"action_id": "search-1", "query_index": 0, "status": "read", "evidence_ids": ["e1"], "cost": {"tool_calls": 1, "tokens": None}}
    observed = {"action_id": "search-1", "query_index": 0, "status": "read", "evidence_ids": ["e1"], "new_artifact_refs": [], "check_updates": []}
    trace = {"schema_version": "query-planner.external.v2", "execution_version": "query-step-v1", "requested": "query_planner", "effective": "query_planner",
             "version": "fixture-policy-hash", "selector": "information_gain_v1", "stop": "observed_checks_v1", "training_status": "heuristic_untrained",
             "direction": "research", "evidence_time": {"mode": "current", "cutoff": "2026-10-01T00:00:00Z"}, "status": "complete", "pending_action": None,
             "checks": [{"id": "evidence", "required": True, "status": "passed", "evidence_ids": ["e1"]}],
             "artifacts": [{"id": "source", "version": "fixed-v1", "kind": "source_documents", "snapshot_ids": ["snapshot-1"],
                            "scope": {"direction": "research", "mode": "current", "cutoff": "2026-10-01T00:00:00Z"}}],
             "artifact_results": {}, "execution": [],
             "decisions": [{"step": 0, "decision": "continue", "action": action, "reason": "information", "state_summary": {"observed_history": []}, "receipt": receipt},
                           {"step": 1, "decision": "complete", "action": None, "reason": "required_observations_satisfied", "state_summary": {"observed_history": [observed]}, "receipt": None}],
             "budget": {"limits": {"tool_calls": 4}, "remaining": {"tool_calls": 1},
                        "usage": {"tool_calls": 3, "known_tool_calls": 3, "unknown_tool_calls": 0, "reserved_tool_calls": 0, "budgeted_tool_calls": 3, "preparation_tool_calls": 2}}}
    return {"id": "analysis-fixture", "investigation_id": "investigation-fixture", "input_hash": "fixed-input-v1", "is_demo": False,
            "question": "Which fixed observations cover this question?", "query_policy": trace, "result": {"query_policy": copy.deepcopy(trace)},
            "evidence": [{"key": "e1", "snapshot_id": "snapshot-1"}],
            "queries": [{"action_id": "search-1", "kind": "document_search", "provider": "fabric", "plan": {"source_id": "epmc", "terms": ["candidate"]},
                         "status": "read", "evidence_keys": ["e1"], "cost": {"tool_calls": 1, "tokens": None}}], "calls": [],
            "budget": {"usage": {"calls": 0, "total_tokens": 0, "known_total_tokens": 0, "unknown_usage_calls": 0}}}


def validator(view, *, terminal):
    assert terminal
    return {"preparation_tool_calls": 2, "status": "complete"}


def evaluate(view, **kwargs):
    return evaluate_radar_view(view, provenance="synthetic_fixture", trace_validator=validator, validator_version="test-checker", **kwargs)


def test_import_reports_observations_and_cost_without_inventing_solver_or_gold():
    view = native_view(); before = copy.deepcopy(view)
    out = evaluate(view)
    assert out["integrity"]["status"] == "passed"
    assert out["trajectory_origin"] == "persisted_radar_host_trace"
    assert out["executed_new_actions"] == 0
    assert out["route_metrics"]["selected_action_count"] == 1
    assert out["route_metrics"]["observable_check_coverage"] == 1
    assert out["route_metrics"]["actual_cost"]["action_tokens"] is None
    assert out["route_metrics"]["actual_cost"]["total_query_tool_calls"] == 3
    assert out["independent_outcome"]["observation_coverage"] is None
    assert out["independent_outcome"]["correct_observable_stop"] is None
    assert out["route_metrics"]["saved_requests"]["status"] == "not_recorded"
    assert view == before


def test_independent_target_only_enters_evaluator_never_native_checker():
    view = native_view(); calls = []
    target = {"qid": view["id"], "environment_ref": "bioradar-analysis:" + view["id"], "environment_version": view["input_hash"],
              "provenance": "synthetic_fixture", "review_status": "fixture_expected", "expected_outcome": "sufficient",
              "requirements": [{"id": "hidden-requirement", "acceptable_evidence_sets": [["missing-independent-evidence"]]}]}
    def checker(received, *, terminal):
        calls.append(copy.deepcopy(received))
        return validator(received, terminal=terminal)
    out = evaluate_radar_view(view, provenance="synthetic_fixture", trace_validator=checker, validator_version="test-checker", target=target)
    assert calls == [view]
    assert out["independent_outcome"]["observation_coverage"] == 0
    assert out["independent_outcome"]["false_observable_completion"] is True


def test_native_failure_never_reports_a_valid_coverage_score():
    def failed(view, *, terminal):
        raise AssertionError("actual receipt differs from saved query")
    out = evaluate_radar_view(native_view(), provenance="synthetic_fixture", trace_validator=failed, validator_version="test-checker")
    assert out["integrity"]["status"] == "failed"
    assert out["route_metrics"] is None and out["independent_outcome"] is None


def test_model_usage_is_independently_reconciled_and_unknown_not_zero():
    view = native_view()
    view["calls"] = [{"call_id": "m1", "status": "completed", "receipt": {"usage": {"total_tokens": 7}}}]
    assert evaluate(view)["integrity"]["reason"] == "analysis_model_ledger_mismatch:calls"
    view["budget"]["usage"] = {"calls": 1, "total_tokens": 7, "known_total_tokens": 7, "unknown_usage_calls": 0}
    assert evaluate(view)["route_metrics"]["actual_cost"]["model_usage"]["total_tokens"] == 7
    view["calls"][0]["receipt"]["usage"] = {}
    view["budget"]["usage"] = {"calls": 1, "total_tokens": None, "known_total_tokens": 0, "unknown_usage_calls": 1}
    assert evaluate(view)["route_metrics"]["actual_cost"]["model_usage"]["total_tokens"] is None


def add_saved_requests(view):
    trace = view["query_policy"]
    initial = {"schema_version": "query-planner.external.v2", "question": view["question"], "direction": "research", "history": [],
               "candidates": [copy.deepcopy(trace["decisions"][0]["action"])]}
    trace["decisions"][0]["policy_request"] = initial
    after = copy.deepcopy(initial); after["candidates"] = []
    after["history"] = [{"action_id": "search-1", "kind": "document_search", "arguments": copy.deepcopy(initial["candidates"][0]["arguments"]),
                         "status": "completed", "new_evidence_ids": ["e1"], "actual_cost": {"tool_calls": 1, "tokens": None}}]
    trace["decisions"][1]["policy_request"] = after
    return after


@pytest.mark.parametrize("field,value", [("actual_cost", {"tool_calls": 7, "tokens": None}), ("observed_information", {"evidence": 1})])
def test_saved_requests_cannot_change_actual_receipt_information(field, value):
    view = native_view(); after = add_saved_requests(view)
    assert evaluate(view)["route_metrics"]["saved_requests"]["status"] == "complete"
    after["history"][0][field] = value
    out = evaluate(view)
    assert out["integrity"]["reason"] == "saved_policy_request_receipt_mismatch:" + field
    assert out["route_metrics"] is None


def test_mining_results_are_not_routing_trajectories_and_demo_is_not_real():
    result = evaluate({"id": "mining-run", "query_policy": None})
    assert result["integrity"] == {"status": "not_evaluable", "validator_version": "test-checker", "reason": "no_saved_supported_route_trace"}
    view = native_view(); view["is_demo"] = True
    with pytest.raises(ValueError, match="non_demo"):
        evaluate_radar_view(view, provenance="real_task", trace_validator=validator, validator_version="test-checker")


def test_in_progress_trace_is_partial_without_an_invented_stop():
    view = native_view(); view["state"] = "running"
    view["query_policy"]["status"] = "running"
    view["query_policy"]["decisions"] = view["query_policy"]["decisions"][:1]
    def partial_checker(received, *, terminal):
        assert terminal is False
        return {"preparation_tool_calls": 2, "status": "running"}
    out = evaluate_radar_view(view, provenance="synthetic_fixture", trace_validator=partial_checker, validator_version="test-checker")
    assert out["assessment_scope"] == "partial_route_trace"
    assert out["route_metrics"]["stop_reason"] is None
    assert out["route_metrics"]["explicit_stop_decision"] is None


def test_reviewed_target_does_not_grade_a_running_route_as_a_stop_failure():
    view = native_view(); view["state"] = "running"
    view["query_policy"]["status"] = "running"
    view["query_policy"]["decisions"] = view["query_policy"]["decisions"][:1]
    target = {"qid": view["id"], "environment_ref": "bioradar-analysis:" + view["id"], "environment_version": view["input_hash"],
              "provenance": "synthetic_fixture", "review_status": "fixture_expected", "expected_outcome": "sufficient",
              "requirements": [{"id": "independent-evidence", "acceptable_evidence_sets": [["e1"]]}]}
    out = evaluate_radar_view(view, provenance="synthetic_fixture", trace_validator=lambda *_args, **_kwargs: {"preparation_tool_calls": 2},
                              validator_version="test-checker", target=target)
    assert out["assessment_scope"] == "partial_route_trace"
    assert out["independent_outcome"]["observation_coverage"] == 1
    assert out["independent_outcome"]["correct_observable_stop"] is None
    assert out["independent_outcome"]["false_observable_completion"] is None


def test_frozen_v1_is_imported_as_v1_without_new_capability_equivalence():
    view = native_view()
    view["query_policy"]["schema_version"] = "query-planner.external.v1"
    view["query_policy"]["requested"] = view["query_policy"]["effective"] = "baseline"
    view["query_policy"]["selector"] = "heuristic"
    view["query_policy"]["stop"] = "rule"
    after = add_saved_requests(view)
    for decision in view["query_policy"]["decisions"]:
        decision["policy_request"]["schema_version"] = "query-planner.external.v1"
        decision["policy_request"].pop("direction")
    out = evaluate(view)
    assert out["integrity"]["status"] == "passed"
    assert out["route_schema_version"] == "query-planner.external.v1"
    assert out["policy"]["effective"] == "baseline"
    assert "frozen_legacy_action_space_not_new_v2_capability_equivalent" in out["limitations"]
    after["history"][0]["actual_cost"]["tool_calls"] = 99
    assert evaluate(view)["integrity"]["status"] == "failed"


def test_import_keeps_unknown_coverage_and_unread_limits_separate_from_integrity():
    view = native_view()
    summary = view["query_policy"]["decisions"][-1]["state_summary"]
    summary.update(coverage_issues=[{"check_id": "model:unknown", "status": "unknown"}],
                   unmet_reading=[{"source_record_id": "visible-unavailable", "status": "unavailable"}],
                   action_attempt_counts={"document_read": 1})
    out = evaluate(view)
    assert out["route_metrics"]["coverage_issues"] == summary["coverage_issues"]
    assert out["route_metrics"]["unmet_reading"] == summary["unmet_reading"]
    assert out["independent_outcome"]["observation_coverage"] is None


def test_model_stages_and_non_route_accounting_are_not_invented():
    view = native_view()
    view["calls"] = [{"call_id": "m1", "stage": "read_plan", "status": "received", "receipt": {"usage": {"total_tokens": 9}}},
                     {"call_id": "m2", "stage": "write", "status": "unknown", "receipt": {}}]
    view["budget"]["usage"] = {"calls": 2, "total_tokens": None, "known_total_tokens": 9, "unknown_usage_calls": 1}
    out = evaluate_radar_view(view, provenance="synthetic_fixture", trace_validator=lambda *_args, **_kwargs: {
        "preparation_tool_calls": 2, "non_route_professional_tool_calls": 3, "unknown_tool_calls": 1, "reserved_tool_calls": 0}, validator_version="test-checker")
    metrics = out["route_metrics"]
    assert metrics["model_stages"]["read_plan"]["known_total_tokens"] == 9
    assert metrics["model_stages"]["write"]["total_tokens"] is None
    assert metrics["actual_cost"]["non_route_professional_tool_calls"] == 3


def test_historical_replay_is_explicit_and_never_accepts_synthetic_sources():
    view = native_view(); view["is_demo"] = True
    with pytest.raises(ValueError, match="non_demo"):
        evaluate_radar_view(view, provenance="historical_replay", trace_validator=validator, validator_version="test-checker")
    view["is_demo"] = False
    out = evaluate_radar_view(view, provenance="historical_replay", trace_validator=validator, validator_version="test-checker")
    assert out["provenance"] == "historical_replay"
    assert out["independent_outcome"]["observation_coverage"] is None
