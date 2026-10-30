"""Synthetic contract tests: no actual task outcome or scientific claim."""
import copy
import json
from types import SimpleNamespace

import pytest

from medical_qa_data_factory.evaluation.route_experiments import summarize_experiments
from medical_qa_data_factory.evaluation.radar_routes import evaluate_radar_view
from medical_qa_data_factory.evaluation.kdd_routes_cli import command
from test_radar_route_import import native_view, validator


def inputs():
    arms, reports = [], []
    for label, schema, action_space in [("new-v2", "v2", ["document_search", "document_read", "source_fetch"]),
                                         ("frozen-baseline", "v1", ["document_search"])]:
        view = native_view(); view["id"] = "analysis-" + label; view["state"] = "needs_review"
        view["query_policy"]["schema_version"] = "query-planner.external." + schema
        view["query_policy"]["version"] = "policy-hash-" + schema
        view["result"]["report"] = {"status": "PASS", "body": "synthetic contract report"}
        result = evaluate_radar_view(view, provenance="synthetic_fixture", trace_validator=validator, validator_version="test-checker")
        reports.append(result)
        arms.append({"analysis_id": view["id"], "label": label, "policy_version": view["query_policy"]["version"],
                     "action_space": action_space, "public_input_sha256": "same-input", "source_scope_sha256": "same-scope",
                     "model_config_sha256": "same-model", "prompt_config_sha256": "same-prompt",
                     "budget_config_sha256": "same-budget", "host_version": "same-host"})
    protocol = {"frozen_protocol_ref": "sealed-protocol-fixture", "experiments": [{"id": "A", "direction": "research",
        "provenance": "synthetic_fixture", "experiment_kind": "synthetic_messages", "required_fact_checks": ["correction", "scope"], "arms": arms}]}
    return protocol, reports


def review(report, **values):
    return {"analysis_id": report["analysis_id"], "source_view_sha256": report["source_view_sha256"],
            "reviewer": "independent-fixture-reviewer", "review_basis_refs": ["sealed-fixture-rubric"],
            "fact_checks": [{"id": "correction", "status": "passed", "basis_refs": ["actual-visible-fixture"]},
                            {"id": "scope", "status": "passed", "basis_refs": ["actual-report-fixture"]}], **values}


def test_integrity_and_biocheck_pass_cannot_approve_independent_facts():
    protocol, reports = inputs()
    before = copy.deepcopy((protocol, reports))
    out = summarize_experiments(protocol, reports)
    assert out["counts"]["integrity"] == {"passed": 2}
    assert out["counts"]["acceptance_passed"] == 0
    assert out["counts"]["acceptance_unreviewed"] == 2
    assert out["experiments"][0]["comparison"]["kind"] == "capability_or_configuration_comparison"
    assert list(out["experiments"][0]["comparison"]["differences"]) == ["action_space"]
    assert out["experiments"][0]["comparison"]["policy_superiority_inferred"] is False
    assert (protocol, reports) == before


def test_independent_facts_are_bound_to_exact_view_and_can_fail_pass_report():
    protocol, reports = inputs(); reviewed = review(reports[0])
    reviewed["fact_checks"][0]["status"] = "failed"
    out = summarize_experiments(protocol, reports, [reviewed])
    assert out["counts"]["acceptance_failed"] == 1
    reviewed["source_view_sha256"] = "different-saved-view"
    with pytest.raises(ValueError, match="view_hash_mismatch"):
        summarize_experiments(protocol, reports, [reviewed])


def test_unreviewed_missing_checks_or_partial_trace_never_becomes_success():
    protocol, reports = inputs(); reviewed = review(reports[0]); reviewed["fact_checks"].pop()
    assert summarize_experiments(protocol, reports, [reviewed])["counts"]["acceptance_passed"] == 0
    reports[0]["assessment_scope"] = "partial_route_trace"
    assert summarize_experiments(protocol, reports, [review(reports[0])])["counts"]["acceptance_unreviewed"] == 2
    reports[0]["policy"]["version"] = "wrong-version"
    with pytest.raises(ValueError, match="policy version"):
        summarize_experiments(protocol, reports)


def test_historical_endpoints_and_visibility_proof_are_separate_from_official_dates():
    protocol, reports = inputs(); experiment = protocol["experiments"][0]
    experiment.update(experiment_kind="historical_replay", provenance="historical_replay", judgement_endpoints=["cluster_risk", "transmission_risk"])
    for report in reports: report["provenance"] = "historical_replay"
    reviewed = review(reports[0], judgement_endpoints=[
        {"id": "cluster_risk", "status": "supported", "observed_at": "2000-01-01", "date_precision": "day", "basis_refs": ["visible-on-first-day-proof"]},
        {"id": "transmission_risk", "status": "unsupported", "basis_refs": ["insufficient-current-source-proof"]}])
    out = summarize_experiments(protocol, reports, [reviewed])
    assert out["counts"]["acceptance_unreviewed"] == 2
    reviewed.update(visibility_review_status="passed", visibility_basis_refs=["frozen-publication-archive-proof"])
    out = summarize_experiments(protocol, reports, [reviewed])
    assert out["counts"]["acceptance_passed"] == 1
    assert len(out["experiments"][0]["arms"][0]["independent_facts"]["judgement_endpoints"]) == 2
    experiment["provenance"] = "synthetic_fixture"
    with pytest.raises(ValueError, match="historical replay"):
        summarize_experiments(protocol, reports)


def test_four_experiment_two_arm_matrix_tracks_missing_and_unknown_usage():
    protocol, reports = inputs(); source = copy.deepcopy(protocol["experiments"][0]); original_reports = copy.deepcopy(reports)
    protocol["experiments"] = []; reports = []
    for case in ["A", "B", "C", "D-contract-only"]:
        experiment = copy.deepcopy(source); experiment["id"] = case
        for arm, report in zip(experiment["arms"], copy.deepcopy(original_reports)):
            arm["analysis_id"] = case + ":" + arm["analysis_id"]; report["analysis_id"] = arm["analysis_id"]; reports.append(report)
        protocol["experiments"].append(experiment)
    out = summarize_experiments(protocol, reports)
    assert out["counts"]["expected_analyses"] == 8
    reports.pop()
    out = summarize_experiments(protocol, reports)
    assert out["counts"]["imported_analyses"] == 7
    assert out["model_usage"]["total_tokens"] is None


def test_summary_cli_reads_imports_only_and_refuses_overwrite(tmp_path):
    protocol, reports = inputs()
    protocol_file = tmp_path / "protocol.json"; protocol_file.write_text(json.dumps(protocol))
    imports = tmp_path / "imports.jsonl"; imports.write_text("\n".join(json.dumps(r) for r in reports))
    args = SimpleNamespace(route_mode="summarize-experiments", protocol=str(protocol_file), imports=[str(imports)], reviews=None, out=str(tmp_path / "summary.json"))
    assert command(args) == 0
    assert json.loads((tmp_path / "summary.json").read_text())["counts"]["imported_analyses"] == 2
    with pytest.raises(FileExistsError): command(args)


@pytest.mark.parametrize("kind,provenance", [
    ("real_task", "historical_replay"), ("real_task", "synthetic_fixture"),
    ("synthetic_messages", "historical_replay"), ("synthetic_messages", "real_task"),
    ("historical_replay", "synthetic_fixture"), ("historical_replay", "real_task"),
])
def test_every_mismatched_kind_and_provenance_is_rejected_before_acceptance(kind, provenance):
    protocol, reports = inputs()
    protocol["experiments"][0].update(experiment_kind=kind, provenance=provenance)
    for report in reports: report["provenance"] = provenance
    reviewed = review(reports[0])
    assert reviewed.get("visibility_review_status", "unreviewed") == "unreviewed"
    with pytest.raises(ValueError, match="kind/provenance"):
        summarize_experiments(protocol, reports, [reviewed])


def test_reassigned_protocol_model_cannot_bypass_historical_proof_requirement():
    from medical_qa_data_factory.evaluation.route_experiments import ExperimentProtocol
    protocol, reports = inputs()
    protocol["experiments"][0].update(experiment_kind="historical_replay", provenance="historical_replay")
    for report in reports: report["provenance"] = "historical_replay"
    model = ExperimentProtocol.model_validate(protocol)
    model.experiments[0].experiment_kind = "real_task"
    with pytest.raises(ValueError, match="kind/provenance"):
        summarize_experiments(model, reports, [review(reports[0])])
