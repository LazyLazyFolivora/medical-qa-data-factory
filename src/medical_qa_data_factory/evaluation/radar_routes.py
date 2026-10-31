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
    "issue", "issue_id", "issue_type", "issue_index", "labels", "sentence_index", "sentence_id", "sentence_ordinal", "sentence_numbers", "paragraph_index",
    "unit_id", "claim_id", "claim_index", "verdict", "citation_status", "evidence_id", "segment_id", "version", "start", "end", "errors",
    "verification_method", "method_provider_version", "draft_sha256", "fingerprint", "effective_verdict",
    "reused_claims", "repair_claims", "claim_ids", "action", "request_hash", "max_output_tokens", "reference",
    "error_codes", "reason_code", "changed_binding_fields",
    "source_id", "source_instance", "source_record_id", "record_id", "version_id", "snapshot_id", "text_sha256",
    "normalized_text_hash", "evidence_key", "evidence_keys", "evidence_ids", "passage_id", "passage_ids", "check_id",
    "terms", "query", "page", "page_number", "next_cursor", "cursor", "has_more", "max_seq", "search_limit", "read_limit",
    "observation_index", "candidate_index", "metric_id", "metric_role", "extractor_version", "proposal_version",
    "attempt", "attempt_count", "repair_count", "truncated", "finish_reason", "request_status", "http_status",
    "request_bytes", "response_bytes", "reserved_tokens", "reserve_tokens", "token_ratio_version",
}
_DIAGNOSTIC_CONTAINERS = {
    "record_ref", "pagination", "plan", "arguments", "cost", "usage", "error", "errors", "diagnostics", "issues", "findings",
    "gaps", "rejections", "rejected", "rejection_reasons", "numeric_rejections", "validation_errors", "validation_issues",
    "repairs", "repair", "repair_diagnostics", "narrative_extractions", "data_preparation", "decisions", "state_summary",
    "coverage_issues", "unmet_reading", "incomplete_search_chains", "checks", "check", "audit", "support_audit",
    "verification", "verify", "units", "claims", "reviews", "evidence", "support",
    "verification_errors", "verification_execution", "source_gaps", "scope_gaps", "read_requests",
    "claim_checks", "reuse_decisions", "coverage_receipt", "short_quotes",
    "sentence_checks", "failed_sentences", "receipt", "history", "repair_attempts", "validation",
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



def _report_diagnostic_presence(report):
    """Describe missing native checks, without reconstructing a verdict or ID."""
    presence = {"report": "present" if isinstance(report, dict) else "absent",
                "verification": "absent", "claim_checks": "absent"}
    missing = []
    if not isinstance(report, dict):
        return presence, missing

    def required(row, path, fields):
        absent = [key for key in fields if key not in row or row[key] is None]
        if absent:
            missing.append({"path": path, "fields": absent})

    # Both names occur in native report versions; do not alias/replace either.
    for key in ("verify", "verification", "check"):
        review = report.get(key)
        if not isinstance(review, dict):
            continue
        presence["verification"] = "present"
        if review.get("verification_method") == "cited_claims":
            root = "/result/report/" + key
            required(review, root, ("method_provider_version", "draft_sha256", "claim_checks", "reused_claims", "repair_claims"))
            checks = review.get("claim_checks")
            if isinstance(checks, list):
                for index, check in enumerate(checks):
                    if isinstance(check, dict):
                        required(check, root + "/claim_checks/" + str(index),
                                 ("claim_id", "fingerprint", "effective_verdict"))
        units = review.get("units")
        if not isinstance(units, list):
            continue
        for index, unit in enumerate(units):
            if not isinstance(unit, dict):
                continue
            path = "/result/report/" + key + "/units/" + str(index)
            required(unit, path, ("unit_id", "verdict", "claims"))
            claims = unit.get("claims")
            if not isinstance(claims, list):
                continue
            for claim_index, claim in enumerate(claims):
                if not isinstance(claim, dict):
                    continue
                presence["claim_checks"] = "present"
                claim_path = path + "/claims/" + str(claim_index)
                required(claim, claim_path, ("verdict",))
                if claim.get("claim_id") is None and claim.get("claim_index") is None:
                    missing.append({"path": claim_path, "fields": ["claim_id_or_claim_index"]})
    audit = report.get("support_audit")
    if isinstance(audit, dict) and isinstance(audit.get("checks"), list):
        for index, check in enumerate(audit["checks"]):
            if not isinstance(check, dict):
                continue
            presence["claim_checks"] = "present"
            path = "/result/report/support_audit/checks/" + str(index)
            required(check, path, ("unit_id", "verdict"))
            if check.get("claim_id") is None and check.get("claim_index") is None:
                missing.append({"path": path, "fields": ["claim_id_or_claim_index"]})
    return presence, missing



# Fixed native metadata contracts only. No traversal of arbitrary payload/source.
_PROVIDER_REF_FIELDS = {"provider", "object_type", "object_id", "adopted_version",
                        "version_source", "content_hash", "authorization_ref"}
_AXIS_FIELDS = {"grade", "assessment_status", "reason_code", "assessed_by", "assessed_at",
                "taxonomy_name", "taxonomy_version", "publisher_scope", "claim_id", "claim_text_sha256",
                "native_review_entry_id", "review_revision", "previous_assessment_ref"}


def _metadata_fields(value, fields):
    return {key: child for key, child in value.items() if key in fields
            and (child is None or isinstance(child, (str, bool)) or
                 isinstance(child, (int, float)) and math.isfinite(child))}


def _missing_metadata(projected, path, required, missing):
    absent = [key for key in required if key not in projected]
    if absent:
        missing.append({"path": path, "fields": absent})


def _metadata_refs(value):
    if isinstance(value, list):
        return [_metadata_fields(ref, _PROVIDER_REF_FIELDS) if isinstance(ref, dict) else None for ref in value]
    return None


def _assessment_metadata(value, missing):
    if not isinstance(value, dict):
        return None
    root = "/result/assessment_axes"
    out = _metadata_fields(value, {"evidence_level"})
    if "heat" in value:
        # Native heat is open JSON; only the existing score/qualification metadata
        # is publishable, never an untrusted nested source or a new combined grade.
        heat = value["heat"]
        if heat is None:
            out["heat"] = None
        elif isinstance(heat, dict):
            out["heat"] = _metadata_fields(heat, {"score", "qualification"})
    for name in ("source_reliability", "information_credibility"):
        items = value.get(name)
        if not isinstance(items, list):
            continue
        out[name] = []
        for index, item in enumerate(items):
            path = root + "/" + name + "/" + str(index)
            if not isinstance(item, dict):
                out[name].append(None)
                missing.append({"path": path, "fields": ["native_assessment_object"]})
                continue
            row = _metadata_fields(item, _AXIS_FIELDS)
            if isinstance(item.get("record_ref"), dict):
                row["record_ref"] = _metadata_fields(item["record_ref"], _PROVIDER_REF_FIELDS)
            for key in ("assessment_basis_refs", "independent_confirmation_refs"):
                if key in item:
                    row[key] = _metadata_refs(item[key])
            _missing_metadata(row, path, ("grade", "assessment_status", "record_ref"), missing)
            out[name].append(row)
    _missing_metadata(out, root, ("source_reliability", "information_credibility", "evidence_level", "heat"), missing)
    return out


def _human_review_metadata(value, missing):
    if not isinstance(value, dict):
        return None
    root = "/human_review_context"
    out = _metadata_fields(value, {"status", "analysis_id", "investigation_only_judgment_count"})
    if isinstance(value.get("reviews"), list):
        out["reviews"] = []
        target_fields = {
            "candidate": {"type", "run_id", "result_hash", "candidate_key"},
            "report": {"type", "analysis_id", "text_sha256"},
            "claim": {"type", "analysis_id", "claim_id", "claim_sha256"},
        }
        for index, item in enumerate(value["reviews"]):
            path = root + "/reviews/" + str(index)
            if not isinstance(item, dict):
                out["reviews"].append(None)
                missing.append({"path": path, "fields": ["native_review_object"]})
                continue
            row = _metadata_fields(item, {"entry_id", "review_revision", "reviewer_actor", "reviewed_at",
                "binding_status", "decision", "scope", "analysis_input_hash"})
            target = item.get("target")
            if isinstance(target, dict):
                kind = target.get("type")
                fields = target_fields.get(kind, {"type"}) if isinstance(kind, str) else {"type"}
                row["target"] = _metadata_fields(target, fields)
                _missing_metadata(row["target"], path + "/target", tuple(sorted(fields)), missing)
            if "basis_record_refs" in item:
                row["basis_record_refs"] = _metadata_refs(item["basis_record_refs"])
            if isinstance(item.get("basis_entry_ids"), list):
                row["basis_entry_ids"] = [identity for identity in item["basis_entry_ids"] if isinstance(identity, str)]
            _missing_metadata(row, path, ("entry_id", "review_revision", "reviewer_actor", "reviewed_at",
                "binding_status", "decision", "scope", "target", "basis_record_refs"), missing)
            out["reviews"].append(row)
    _missing_metadata(out, root, ("status", "analysis_id", "reviews"), missing)
    return out


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
    diagnostic_presence, missing_fields = _report_diagnostic_presence(native)
    result = view.get("result") if isinstance(view.get("result"), dict) else {}
    axes = _assessment_metadata(result.get("assessment_axes"), missing_fields)
    human_review = _human_review_metadata(view.get("human_review_context"), missing_fields)
    diagnostic_presence.update(assessment_axes="present" if axes is not None else "absent",
                               human_review_context="present" if human_review is not None else "absent")
    raw_calls = view.get("calls")
    ledger, unique, problems, variants, ambiguous_stages = [], {}, [], {}, {}
    token_fields = ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens")
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
            receipt = call.get("receipt") if isinstance(call.get("receipt"), dict) else {}
            raw_usage = receipt.get("usage")
            invalid = False
            if raw_usage is None:
                raw_usage = {}
            if not isinstance(raw_usage, dict):
                raw_usage = {}
                invalid = True
                problems.append("invalid_model_usage_shape:" + identity)
            safe_usage = {}
            for key in token_fields:
                if key not in raw_usage or raw_usage[key] is None:
                    continue
                try:
                    valid = _number(raw_usage[key])
                except (OverflowError, TypeError, ValueError):
                    valid = False
                if valid:
                    safe_usage[key] = raw_usage[key]
                else:
                    invalid = True
                    problems.append("invalid_model_usage_field:" + identity + ":" + key)
            # Never copy an unvalidated usage payload. Any malformed token field
            # makes this call's cost unknown even when another field looks valid.
            safe = {"call_id": identity,
                    "stage": call.get("stage") if isinstance(call.get("stage"), str) else "not_recorded",
                    "status": call.get("status") if isinstance(call.get("status"), str) else "unknown",
                    "receipt": {"status": receipt.get("status") if isinstance(receipt.get("status"), str) else None,
                                "usage": {} if invalid else safe_usage}}
            variants.setdefault(identity, []).append(safe)
        for identity, saved in variants.items():
            first = saved[0]
            if len(saved) > 1:
                problems.append("duplicate_model_call_id:" + identity)
            if any(item != first for item in saved[1:]):
                problems.append("conflicting_model_call_id:" + identity)
                stages_seen = {item["stage"] for item in saved}
                if len(stages_seen) > 1:
                    for stage in stages_seen:
                        ambiguous_stages.setdefault(stage, []).append(identity)
                first = {"call_id": identity, "stage": next(iter(stages_seen)) if len(stages_seen) == 1 else "conflicting_stage",
                         "status": "unknown", "receipt": {"status": "unknown", "usage": {}}}
            unique[identity] = first
            ledger.append({"call_id": identity, "stage": first["stage"], "status": first["status"],
                           "receipt_status": first["receipt"]["status"], "usage": copy.deepcopy(first["receipt"]["usage"])})
        usage = _model_usage(list(unique.values()))
        stages = {stage: _model_usage([call for call in unique.values() if call["stage"] == stage])
                  for stage in sorted({call["stage"] for call in unique.values()})}
        for stage, identities in ambiguous_stages.items():
            entry = stages.setdefault(stage, _model_usage([]))
            entry["total_tokens"] = None
            entry["unknown_usage_calls"] += len(identities)
            entry["ambiguous_call_ids"] = sorted(identities)
            entry["qualification"] = "possible stage attribution; ambiguity counts overlap across stages; use global unique-call ledger for totals"
        if problems:
            usage["total_tokens"] = None
    else:
        usage, stages = None, {}
        problems.append("actual_model_calls_not_exported")
    try:
        view_hash = digest(view)
    except (ValueError, TypeError, OverflowError):
        view_hash = None
        problems.append("source_view_not_canonical_json")
    return {"schema_version": "radar-diagnostics-export-v1", "analysis_id": view.get("id"),
            "source_view_sha256": view_hash, "scenario": scenario, "phase": phase,
            "analysis_state": view.get("state"), "created_at": view.get("created_at"),
            "analysis_error_code": view.get("error_code"),
            "native_report_status": native.get("status") if isinstance(native, dict) else None,
            "business_acceptance": "not_assessed", "diagnostic_rows": rows,
            "diagnostic_presence": diagnostic_presence, "missing_diagnostic_fields": missing_fields,
            "assessment_axes": axes, "human_review_context": human_review,
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
