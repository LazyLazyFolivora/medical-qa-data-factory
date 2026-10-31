"""Read-only assessment of persisted Radar v1/v2 host trajectories.

No action is dispatched, no missing policy request or receipt is reconstructed,
and a saved host trace is never relabelled as a fresh independent solver run.
The caller supplies an independently maintained native trace checker so fixed
artifact payloads and professional ledger bindings are checked in their schema.
"""
from __future__ import annotations

import copy
import math
from collections import Counter

from .kdd_routes import RouteEvaluationTarget, digest

EVALUATOR_VERSION = "radar-route-import-1.3"
DIRECTIONS = {"pipeline", "research", "business", "public_health"}
PENDING = {"prepared", "sent", "unknown", "unconfirmed"}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _nullable_sum(values):
    _require(all(v is None or _number(v) for v in values), "invalid_actual_cost")
    return None if any(v is None for v in values) else sum(values)


def _refs(values):
    _require(isinstance(values, list), "invalid_artifact_refs")
    pairs = []
    for value in values:
        _require(isinstance(value, dict) and isinstance(value.get("id"), str) and bool(value["id"])
                 and isinstance(value.get("version"), str) and bool(value["version"]), "invalid_artifact_reference")
        pairs.append((value["id"], value["version"]))
    _require(len(set(pairs)) == len(pairs), "duplicate_artifact_refs")
    return set(pairs)


def _query_receipt(query, action):
    status = query["status"]
    if status in {"read", "empty"}:
        status = "completed"
    elif status == "unavailable":
        status = "failed"
    if (query.get("usage") or {}).get("request_status") == "not_sent":
        status = "not_sent"
    return {"action_id": action["id"], "kind": action["kind"], "arguments": action["arguments"],
            "status": status, "new_evidence_ids": query.get("evidence_keys", []),
            "new_artifact_refs": query.get("artifact_refs", []), "check_updates": query.get("check_updates", []),
            "actual_cost": query.get("cost"), "observed_information": query.get("observed_information", {}),
            "pagination": copy.deepcopy(query.get("pagination"))}


def _same_history(actual, recorded):
    for key in ("action_id", "kind", "arguments", "status", "actual_cost", "pagination"):
        _require(recorded.get(key) == actual.get(key), "saved_policy_request_receipt_mismatch:" + key)
    _require(set(recorded.get("new_evidence_ids", [])) == set(actual["new_evidence_ids"]),
             "saved_policy_request_receipt_mismatch:new_evidence_ids")
    _require(_refs(recorded.get("new_artifact_refs", [])) == _refs(actual["new_artifact_refs"]),
             "saved_policy_request_receipt_mismatch:new_artifact_refs")
    _require(recorded.get("check_updates", []) == actual["check_updates"],
             "saved_policy_request_receipt_mismatch:check_updates")
    _require(recorded.get("observed_information", {}) == actual["observed_information"],
             "saved_policy_request_receipt_mismatch:observed_information")


def _saved_requests(view, trace):
    """Verify only requests actually saved at decision time; absence stays absent."""
    prior, present = {}, 0
    for decision in trace["decisions"]:
        request = decision.get("policy_request")
        if request is not None:
            present += 1
            _require(isinstance(request, dict) and request.get("schema_version") == trace["schema_version"],
                     "invalid_saved_policy_request")
            _require(request.get("question") == view["question"], "saved_policy_request_changed_task")
            if trace["schema_version"] == "query-planner.external.v2" or "direction" in request:
                _require(request.get("direction") == trace.get("direction"), "saved_policy_request_changed_task")
            _require(not ({"gold", "evaluation_target", "gold_evidence_ids", "generation_traces", "oracle"} & set(request)),
                     "private_targets_in_saved_policy_request")
            history = request.get("history", [])
            _require(isinstance(history, list), "invalid_saved_request_history")
            by_id = {entry["action_id"]: entry for entry in history}
            _require(len(by_id) == len(history) and set(by_id) == set(prior), "saved_policy_request_history_set_mismatch")
            for action_id, receipt in prior.items():
                _same_history(receipt, by_id[action_id])
            action = decision.get("action")
            if action is not None:
                _require(action in request.get("candidates", []), "selected_action_not_in_saved_candidates")
                _require(not any(h.get("status") in PENDING for h in history), "action_after_pending_receipt")
        action, receipt = decision.get("action"), decision.get("receipt")
        if action is not None and receipt is not None:
            index = receipt["query_index"]
            prior[action["id"]] = _query_receipt(view["queries"][index], action)
    return {"saved_request_count": present, "decision_count": len(trace["decisions"]),
            "status": "complete" if present == len(trace["decisions"]) else "partial" if present else "not_recorded"}


def _model_usage(calls):
    known, counts, unknown = 0, 0, 0
    ids = set()
    for call in calls:
        call_id = call.get("call_id")
        _require(isinstance(call_id, str) and call_id not in ids, "model_call_identity_missing_or_duplicated")
        ids.add(call_id)
        receipt = call.get("receipt") or {}
        if call.get("status") == "prepared" or receipt.get("status") == "not_sent":
            continue
        counts += 1
        usage = receipt.get("usage") or {}
        value = usage.get("total_tokens")
        if value is None and all(_number(usage.get(key)) for key in ("prompt_tokens", "completion_tokens")):
            value = usage["prompt_tokens"] + usage["completion_tokens"]
        if value is None:
            unknown += 1
        else:
            _require(_number(value), "invalid_model_token_usage")
            known += value
    return {"calls": counts, "known_total_tokens": known, "unknown_usage_calls": unknown,
            "total_tokens": None if unknown else known}


def _target_result(target, *, analysis_id, environment, provenance, evidence, artifacts, decision, terminal):
    result = {"review_status": "unreviewed", "evaluation_status": "unreviewed_no_gold_score",
              "observation_coverage": None, "correct_observable_stop": None,
              "false_observable_completion": None, "requirements": []}
    if target is None:
        return result
    target = RouteEvaluationTarget.model_validate(target)
    _require(target.qid == analysis_id and target.environment_ref == environment["environment_ref"]
             and target.environment_version == environment["environment_version"] and target.provenance == provenance,
             "independent_target_binding_mismatch")
    result["review_status"] = target.review_status
    if target.review_status == "unreviewed":
        return result
    rows = []
    for requirement in target.requirements:
        passed = any(set(group) <= evidence for group in requirement.acceptable_evidence_sets)
        passed |= any({(ref.id, ref.version) for ref in group} <= artifacts for group in requirement.acceptable_artifact_sets)
        rows.append({"id": requirement.id, "weight": requirement.weight, "passed": passed})
    coverage = sum(r["weight"] for r in rows if r["passed"]) / sum(r["weight"] for r in rows) if rows else None
    result.update(evaluation_status="evaluated_independent_observation_targets", requirements=rows, observation_coverage=coverage)
    # Partial evidence may be assessed, but a task that has not stopped has no stop score.
    if not terminal or decision not in {"complete", "insufficient"}:
        return result
    if target.expected_outcome == "sufficient":
        result["correct_observable_stop"] = decision == "complete" and coverage == 1
        result["false_observable_completion"] = decision == "complete" and coverage != 1
    elif target.expected_outcome == "insufficient":
        result["correct_observable_stop"] = decision == "insufficient"
        result["false_observable_completion"] = decision == "complete"
    return result



# Diagnostics are a positive projection. Bodies, prompts, model outputs and
# numeric observations are not traversed. Named rejection/issue text is retained
# because excluding full source material must not erase the reason for failure.
_DIAGNOSTIC_FIELDS = {
    "id", "call_id", "call_key", "action_id", "query_index", "index", "step", "stage", "phase", "kind", "provider",
    "status", "error_code", "code", "rule_id", "rule_ids", "field_path", "path", "reason", "trigger_reason", "repair_reason",
    "issue", "issue_id", "issue_type", "sentence_index", "sentence_id", "sentence_ordinal", "paragraph_index",
    "source_id", "source_instance", "source_record_id", "record_id", "version_id", "snapshot_id", "text_sha256",
    "normalized_text_hash", "evidence_key", "evidence_keys", "evidence_ids", "passage_id", "passage_ids", "check_id",
    "terms", "query", "page", "page_number", "next_cursor", "cursor", "has_more", "max_seq", "search_limit", "read_limit",
    "observation_index", "candidate_index", "metric_id", "metric_role", "extractor_version", "proposal_version",
    "attempt", "attempt_count", "repair_count", "truncated", "finish_reason", "request_status", "http_status",
}
_DIAGNOSTIC_CONTAINERS = {
    "record_ref", "pagination", "plan", "arguments", "cost", "usage", "error", "errors", "diagnostics", "issues", "findings",
    "gaps", "rejections", "rejected", "rejection_reasons", "numeric_rejections", "validation_errors", "validation_issues",
    "repairs", "repair", "repair_diagnostics", "narrative_extractions", "data_preparation", "decisions", "state_summary",
    "coverage_issues", "unmet_reading", "incomplete_search_chains", "checks", "check", "audit", "support_audit",
    "verification", "sentence_checks", "failed_sentences", "receipt", "history", "repair_attempts", "validation",
}


def _diagnostic_rows(value, path, *, indexed=False):
    if isinstance(value, list):
        return [row for index, child in enumerate(value)
                for row in _diagnostic_rows(child, path + "/" + str(index))]
    if not isinstance(value, dict):
        return []
    # Only scalar values or flat scalar lists under explicitly diagnostic names.
    values = {key: copy.deepcopy(child) for key, child in value.items()
              if key in _DIAGNOSTIC_FIELDS
              and (child is None or isinstance(child, (str, int, float, bool))
                   or isinstance(child, list) and all(item is None or isinstance(item, (str, int, float, bool)) for item in child))}
    rows = [{"path": path, "values": values}] if values else []
    for key, child in value.items():
        if key in _DIAGNOSTIC_CONTAINERS or indexed:
            # narrative_extractions is a dict keyed by persisted call hash.
            rows.extend(_diagnostic_rows(child, path + "/" + key, indexed=key == "narrative_extractions"))
    return rows


def export_radar_diagnostics(view, *, scenario=None, phase=None):
    """Read-only, source-body-free diagnostics even for blocked/absent routes.

    scenario/phase are caller-supplied run-manifest labels, never inferred from
    private grading data. Absence is explicit; no cause or observation is filled.
    """
    _require(isinstance(view, dict), "analysis_view_required")
    rows = []
    for key in ("queries", "query_policy", "progress"):
        rows.extend(_diagnostic_rows(view.get(key), "/" + key))
    native = (view.get("result") or {}).get("report")
    if isinstance(native, dict):
        rows.extend(_diagnostic_rows(native, "/result/report"))
    raw_calls = view.get("calls")
    ledger, unique, problems = [], {}, []
    if isinstance(raw_calls, list):
        for index, call in enumerate(raw_calls):
            if not isinstance(call, dict):
                problems.append("invalid_model_call_row:" + str(index))
                continue
            rows.extend(_diagnostic_rows(call, "/calls/" + str(index)))
            identity = call.get("call_id")
            if not isinstance(identity, str) or not identity:
                problems.append("missing_model_call_id:" + str(index))
                continue
            if identity in unique:
                problems.append("duplicate_model_call_id:" + identity)
                continue
            unique[identity] = call
            receipt = call.get("receipt") or {}
            usage = receipt.get("usage") or {}
            # Whitelist usage fields too; receipts can contain private payloads.
            ledger.append({"call_id": identity, "stage": call.get("stage"), "status": call.get("status"),
                           "receipt_status": receipt.get("status"),
                           "usage": {key: copy.deepcopy(usage[key]) for key in
                                     ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens") if key in usage}})
        try:
            usage = _model_usage(list(unique.values()))
            stages = {stage: _model_usage([call for call in unique.values() if (call.get("stage") or "not_recorded") == stage])
                      for stage in sorted({call.get("stage") or "not_recorded" for call in unique.values()})}
        except (ValueError, TypeError):
            usage, stages = None, {}
            problems.append("invalid_model_usage")
        if problems and usage is not None:
            usage["total_tokens"] = None
    else:
        usage, stages = None, {}
        problems.append("actual_model_calls_not_exported")
    return {"schema_version": "radar-diagnostics-export-v1", "analysis_id": view.get("id"),
            "source_view_sha256": digest(view), "scenario": scenario, "phase": phase,
            "analysis_state": view.get("state"), "created_at": view.get("created_at"),
            "analysis_error_code": view.get("error_code"),
            "native_report_status": native.get("status") if isinstance(native, dict) else None,
            "business_acceptance": "not_assessed", "diagnostic_rows": rows,
            "model_calls": ledger, "model_usage": usage, "model_stages": stages, "usage_issues": problems,
            "excluded": ["source_bodies", "questions", "private_prompts", "raw_model_outputs", "numeric_observation_values"],
            "scope": "persisted_analysis_generation_calls_only; embedding/rerank/index are separately accounted",
            "missing_diagnostics": "absence stays absent; exporter does not reconstruct errors, search pages or causes"}


def evaluate_radar_view(view, *, provenance, trace_validator, validator_version, target=None):
    """Evaluate a native saved AnalysisView without invoking any solver/tool.

    trace_validator(view, terminal=bool) must be the separately maintained native
    provenance checker. Its content hash is recorded; it never receives targets.
    """
    _require(provenance in {"real_task", "synthetic_fixture", "historical_replay"}, "explicit_provenance_required")
    _require(isinstance(view, dict), "analysis_view_required")
    _require(provenance not in {"real_task", "historical_replay"} or view.get("is_demo") is False, "real_or_historical_requires_non_demo_analysis")
    report = {"schema_version": "kdd-route-radar-evaluation-v1", "evaluator_version": EVALUATOR_VERSION,
              "trajectory_origin": "persisted_radar_host_trace", "provenance": provenance,
              "analysis_id": view.get("id"), "investigation_id": view.get("investigation_id"), "analysis_state": view.get("state"),
              "source_view_sha256": digest(view), "executed_new_actions": 0,
              "integrity": {"status": "not_evaluable", "validator_version": validator_version},
              "route_metrics": None, "independent_outcome": None,
              "diagnostics": export_radar_diagnostics(view),
              "limitations": ["scientific_validity_not_assessed", "business_value_not_assessed",
                              "counterfactual_policy_benefit_requires_independent_runs"]}
    trace = view.get("query_policy")
    if not isinstance(trace, dict) or trace.get("schema_version") not in {"query-planner.external.v1", "query-planner.external.v2"}:
        report["integrity"]["reason"] = "no_saved_supported_route_trace"
        return report
    report["route_schema_version"] = trace["schema_version"]
    report["direction"] = trace.get("direction")
    native_report = (view.get("result") or {}).get("report")
    report["native_report"] = {"status": native_report.get("status") if isinstance(native_report, dict) else None,
                               "sha256": digest(native_report) if native_report is not None else None,
                               "independent_fact_review": "not_inferred_from_biocheck"}
    report["execution_context"] = {"question_sha256": digest(view.get("question")),
        "prompt_version": view.get("prompt_version"), "trigger": view.get("trigger"),
        "parent_analysis_id": view.get("parent_analysis_id"), "is_demo": view.get("is_demo")}
    if trace["schema_version"] == "query-planner.external.v1":
        report["limitations"].append("frozen_legacy_action_space_not_new_v2_capability_equivalent")
    report["policy"] = {key: copy.deepcopy(trace.get(key)) for key in
                        ("requested", "effective", "version", "selector", "stop", "training_status")}
    report["environment"] = {"environment_ref": "bioradar-analysis:" + str(view.get("id")),
                             "environment_version": view.get("input_hash"),
                             "evidence_time": copy.deepcopy(trace.get("evidence_time")),
                             "binding": "persisted_analysis_input_not_a_new_query_environment"}
    try:
        _require(isinstance(view.get("id"), str) and bool(view["id"]) and isinstance(view.get("input_hash"), str)
                 and bool(view["input_hash"]), "analysis_identity_or_fixed_input_hash_missing")
        _require(trace.get("direction") in DIRECTIONS or (trace["schema_version"] == "query-planner.external.v1" and trace.get("direction") is None),
                 "unsupported_direction")
        if trace.get("direction") is None:
            report["limitations"].append("legacy_direction_not_recorded")
        _require(callable(trace_validator) and bool(validator_version), "independent_native_trace_validator_required")
        if not trace.get("decisions"):
            report["integrity"]["reason"] = "no_policy_decisions_persisted"
            return report
        terminal = trace.get("status") in {"complete", "insufficient", "blocked"}
        report["assessment_scope"] = "terminal_route_trace" if terminal else "partial_route_trace"
        native = trace_validator(copy.deepcopy(view), terminal=terminal)
        _require(isinstance(native, dict), "native_validator_did_not_return_assessment")
        requests = _saved_requests(view, trace)
        if requests["status"] != "complete":
            report["limitations"].append("decision_replay_not_evaluable_without_all_saved_policy_requests")
        evidence = {row["key"] for row in view.get("evidence", [])}
        artifacts = _refs(trace.get("artifacts", []))
        report["environment"]["source_artifacts"] = [
            {"id": row["id"], "version": row["version"], "snapshot_ids": copy.deepcopy(row.get("snapshot_ids", []))}
            for row in trace.get("artifacts", []) if row.get("kind") == "source_documents"]
        report["environment"]["evidence_snapshot_ids"] = sorted({
            row["snapshot_id"] for row in view.get("evidence", []) if isinstance(row.get("snapshot_id"), str)})
        required = [check for check in trace.get("checks", []) if check.get("required", True)]
        passed = []
        for check in required:
            if check.get("status") == "passed":
                refs, ids = _refs(check.get("artifact_refs", [])), set(check.get("evidence_ids", []))
                _require(bool(refs or ids) and refs <= artifacts and ids <= evidence,
                         "passed_observation_check_without_current_support")
                passed.append(check["id"])
        queries = view.get("queries", [])
        calls = view.get("calls")
        _require(isinstance(calls, list), "actual_model_calls_not_exported")
        model_usage = _model_usage(calls)
        reported_usage = (view.get("budget") or {}).get("usage", {})
        for key, value in model_usage.items():
            if key in reported_usage:
                _require(reported_usage[key] == value, "analysis_model_ledger_mismatch:" + key)
        action_costs = [q.get("cost") or {} for q in queries]
        action_calls = _nullable_sum([c.get("tool_calls") for c in action_costs])
        action_tokens = _nullable_sum([c.get("tokens") for c in action_costs])
        final = trace["decisions"][-1]
        stop_decision = final["decision"] if final.get("action") is None else None
        report["route_metrics"] = {
            "selected_action_count": sum(d.get("action") is not None for d in trace["decisions"]),
            "receipt_count": len(queries), "action_kinds": dict(Counter(q.get("kind", "document_search") for q in queries)),
            "receipt_statuses": dict(Counter(q["status"] for q in queries)),
            "required_check_count": len(required), "passed_required_check_count": len(passed),
            "observable_check_coverage": len(passed) / len(required) if required else None,
            "unmet_checks": [c["id"] for c in required if c["id"] not in passed],
            "trace_status": trace["status"], "explicit_stop_decision": stop_decision,
            "stop_reason": (trace.get("reason") or final.get("reason")) if terminal else None,
            "last_decision_reason": final.get("reason"),
            "actual_cost": {"action_tool_calls": action_calls, "preparation_tool_calls": native.get("preparation_tool_calls"),
                             "non_route_professional_tool_calls": native.get("non_route_professional_tool_calls"),
                             "unknown_tool_calls": native.get("unknown_tool_calls"), "reserved_tool_calls": native.get("reserved_tool_calls"),
                            "total_query_tool_calls": trace.get("budget", {}).get("usage", {}).get("tool_calls"),
                            "action_tokens": action_tokens, "model_usage": model_usage},
            "unknown_action_token_receipts": sum(c.get("tokens") is None for c in action_costs),
            "pending_receipt_count": sum(q["status"] in PENDING for q in queries),
            "saved_requests": requests,
            "coverage_issues": copy.deepcopy(final.get("state_summary", {}).get("coverage_issues", [])),
            "unmet_reading": copy.deepcopy(final.get("state_summary", {}).get("unmet_reading", [])),
            # This is a countable set of unfinished query chains, not unseen documents/pages.
            "incomplete_search_chains": copy.deepcopy(final.get("state_summary", {}).get("incomplete_search_chains", [])),
            "search_attempt_counts_by_phase": copy.deepcopy(final.get("state_summary", {}).get("search_attempt_counts_by_phase", {})),
            "action_attempt_counts": copy.deepcopy(final.get("state_summary", {}).get("action_attempt_counts", {})),
            "model_stages": {stage: _model_usage([call for call in calls if (call.get("stage") or "not_recorded") == stage])
                             for stage in sorted({call.get("stage") or "not_recorded" for call in calls})}}
        report["independent_outcome"] = _target_result(target, analysis_id=view["id"], environment=report["environment"],
            provenance=provenance, evidence=evidence, artifacts=artifacts, decision=stop_decision, terminal=terminal)
        if report["independent_outcome"]["review_status"] == "unreviewed":
            report["limitations"].append("independent_target_coverage_and_stop_correctness_unreviewed")
        report["integrity"].update(status="passed", native_assessment=native)
    except (AssertionError, ValueError, KeyError, TypeError, IndexError) as exc:
        report["integrity"].update(status="failed", reason=str(exc))
        report["route_metrics"] = None
        report["independent_outcome"] = None
    return report
