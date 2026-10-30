"""Post-run experiment summaries. Never imported by the solver/host environment.

Protocol declarations describe frozen comparisons; reviewed facts are separate
inputs bound to one exact saved view. Neither route coverage nor BioCheck is an
independent fact judgement. Different action spaces are capability comparisons.
"""
from __future__ import annotations

import copy
from collections import Counter
from typing import Literal

from pydantic import Field, model_validator

from ..domain.common import ContractModel
from .kdd_routes import Direction, Provenance, digest


class ExperimentArm(ContractModel):
    analysis_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    action_space: list[str] = Field(min_length=1)
    public_input_sha256: str = Field(min_length=1)
    source_scope_sha256: str = Field(min_length=1)
    model_config_sha256: str = Field(min_length=1)
    prompt_config_sha256: str = Field(min_length=1)
    budget_config_sha256: str = Field(min_length=1)
    host_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def unique_actions(self):
        from .kdd_routes import KINDS
        if len(set(self.action_space)) != len(self.action_space) or set(self.action_space) - KINDS:
            raise ValueError("invalid declared action space")
        self.action_space = sorted(self.action_space)
        return self


class Experiment(ContractModel):
    id: str = Field(min_length=1)
    direction: Direction
    provenance: Provenance
    experiment_kind: Literal["synthetic_messages", "historical_replay", "real_task"]
    # A historical positive case and each false-rumor/prior-season control are
    # separate experiment rows; they may share an experiment_group.
    experiment_group: str | None = None
    control_role: Literal["target", "false_rumor", "prior_season"] = "target"
    judgement_endpoints: list[str] = Field(default_factory=list)
    required_fact_checks: list[str] = Field(min_length=1)
    arms: list[ExperimentArm] = Field(min_length=1)

    @model_validator(mode="after")
    def bindings(self):
        if self.experiment_kind == "synthetic_messages" and self.provenance != "synthetic_fixture":
            raise ValueError("synthetic messages cannot be labelled real_task")
        if self.experiment_kind == "historical_replay" and self.provenance != "historical_replay":
            raise ValueError("historical replay requires real publicly available source material")
        for values in (self.required_fact_checks, self.judgement_endpoints, [a.label for a in self.arms]):
            if len(set(values)) != len(values) or any(not value for value in values):
                raise ValueError("experiment identifiers must be unique and nonempty")
        return self


class ExperimentProtocol(ContractModel):
    schema_version: Literal["kdd-route-experiments-v1"] = "kdd-route-experiments-v1"
    frozen_protocol_ref: str = Field(min_length=1)
    experiments: list[Experiment] = Field(min_length=1)

    @model_validator(mode="after")
    def identities(self):
        ids = [e.id for e in self.experiments]
        analyses = [arm.analysis_id for e in self.experiments for arm in e.arms]
        if len(set(ids)) != len(ids) or len(set(analyses)) != len(analyses):
            raise ValueError("duplicate experiment or analysis identity")
        return self


class FactCheck(ContractModel):
    id: str = Field(min_length=1)
    status: Literal["passed", "failed", "not_evaluable"]
    basis_refs: list[str] = Field(min_length=1)


class JudgementEndpoint(ContractModel):
    id: str = Field(min_length=1)
    status: Literal["supported", "unsupported", "not_evaluable"]
    # Dates are reported as reviewed evidence, never derived from a model's
    # confident wording or equated to a later official announcement date.
    observed_at: str | None = None
    date_precision: Literal["instant", "day", "interval", "unknown"] = "unknown"
    basis_refs: list[str] = Field(min_length=1)


class IndependentFactReview(ContractModel):
    schema_version: Literal["kdd-route-fact-review-v1"] = "kdd-route-fact-review-v1"
    analysis_id: str = Field(min_length=1)
    source_view_sha256: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    review_basis_refs: list[str] = Field(min_length=1)
    fact_checks: list[FactCheck] = Field(default_factory=list)
    judgement_endpoints: list[JudgementEndpoint] = Field(default_factory=list)
    visibility_review_status: Literal["unreviewed", "passed", "failed"] = "unreviewed"
    visibility_basis_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_checks(self):
        for rows in (self.fact_checks, self.judgement_endpoints):
            if len({row.id for row in rows}) != len(rows):
                raise ValueError("duplicate independent review criterion")
        if self.visibility_review_status != "unreviewed" and not self.visibility_basis_refs:
            raise ValueError("historical visibility judgement requires dated source proof references")
        return self


def _review_result(experiment, imported, review):
    result = {"status": "unreviewed", "facts_passed": None, "fact_checks": [],
              "judgement_endpoints": [], "visibility_review_status": "unreviewed"}
    if review is None:
        return result
    if review.source_view_sha256 != imported["source_view_sha256"]:
        raise ValueError("independent_review_view_hash_mismatch")
    checks = {check.id: check for check in review.fact_checks}
    endpoints = {endpoint.id: endpoint for endpoint in review.judgement_endpoints}
    if set(checks) - set(experiment.required_fact_checks) or set(endpoints) - set(experiment.judgement_endpoints):
        raise ValueError("review contains criteria not in frozen experiment protocol")
    complete = set(checks) == set(experiment.required_fact_checks) and all(c.status != "not_evaluable" for c in checks.values())
    facts = all(c.status == "passed" for c in checks.values()) if complete else None
    result.update(status="reviewed" if complete else "partial_review", facts_passed=facts,
                  reviewer=review.reviewer, review_basis_refs=review.review_basis_refs,
                  fact_checks=[c.model_dump() for c in review.fact_checks],
                  judgement_endpoints=[c.model_dump() for c in review.judgement_endpoints],
                  visibility_review_status=review.visibility_review_status,
                  visibility_basis_refs=review.visibility_basis_refs)
    return result


def summarize_experiments(protocol, imported_reports, independent_reviews=()):
    """Read frozen imports and separate reviews; never execute models or tools."""
    protocol = ExperimentProtocol.model_validate(protocol)
    reports = {r["analysis_id"]: r for r in imported_reports}
    reviews_list = [IndependentFactReview.model_validate(r) for r in independent_reviews]
    reviews = {r.analysis_id: r for r in reviews_list}
    if len(reports) != len(imported_reports) or len(reviews) != len(reviews_list):
        raise ValueError("duplicate imported report or independent review")
    expected_ids = {a.analysis_id for e in protocol.experiments for a in e.arms}
    if set(reports) - expected_ids or set(reviews) - expected_ids:
        raise ValueError("undeclared analysis in experiment inputs")
    rows = []
    for experiment in protocol.experiments:
        arms = []
        for arm in experiment.arms:
            imported = reports.get(arm.analysis_id)
            row = {"analysis_id": arm.analysis_id, "label": arm.label, "status": "not_imported",
                   "independent_facts": None, "acceptance_passed": None}
            if imported is not None:
                if imported.get("trajectory_origin") != "persisted_radar_host_trace" or imported.get("executed_new_actions") != 0:
                    raise ValueError("experiment requires actual read-only Radar imports")
                if imported.get("provenance") != experiment.provenance or imported.get("direction") not in {None, experiment.direction}:
                    raise ValueError("experiment provenance or direction mismatch")
                if imported.get("policy", {}).get("version") != arm.policy_version:
                    raise ValueError("experiment policy version differs from actual imported policy")
                review = _review_result(experiment, imported, reviews.get(arm.analysis_id))
                metrics = imported.get("route_metrics") or {}
                terminal = imported.get("assessment_scope") == "terminal_route_trace"
                native_status = imported.get("native_report", {}).get("status")
                acceptance = None
                if terminal and review["facts_passed"] is not None:
                    historical_ready = experiment.experiment_kind != "historical_replay" or review["visibility_review_status"] == "passed"
                    endpoints_ready = set(e["id"] for e in review["judgement_endpoints"]) == set(experiment.judgement_endpoints) and all(e["status"] != "not_evaluable" for e in review["judgement_endpoints"])
                    if historical_ready and endpoints_ready:
                        acceptance = (imported["integrity"]["status"] == "passed" and native_status == "PASS" and review["facts_passed"])
                row.update(status=imported["integrity"]["status"], source_view_sha256=imported["source_view_sha256"],
                           analysis_state=imported.get("analysis_state"), assessment_scope=imported.get("assessment_scope"),
                           native_report_status=native_status, independent_facts=review, acceptance_passed=acceptance,
                           route_metrics=copy.deepcopy(metrics), limitations=copy.deepcopy(imported.get("limitations", [])))
            arms.append(row)
        fields = ("action_space", "public_input_sha256", "source_scope_sha256", "model_config_sha256", "prompt_config_sha256", "budget_config_sha256", "host_version")
        differences = {field: {arm.label: getattr(arm, field) for arm in experiment.arms}
                       for field in fields if len({digest(getattr(arm, field)) for arm in experiment.arms}) > 1}
        rows.append({"experiment_id": experiment.id, "experiment_group": experiment.experiment_group,
                     "experiment_kind": experiment.experiment_kind, "control_role": experiment.control_role,
                     "provenance": experiment.provenance, "direction": experiment.direction, "arms": arms,
                     "comparison": {"kind": "capability_or_configuration_comparison" if differences else "matched_declared_configuration",
                                    "metadata_basis": "frozen_protocol_declaration_actual_policy_versions_verified",
                                    "differences": differences, "policy_superiority_inferred": False,
                                    "original_baseline_equivalence_claimed": False}})
    available = [a for e in rows for a in e["arms"] if a["status"] != "not_imported"]
    usage = [(a.get("route_metrics") or {}).get("actual_cost", {}).get("model_usage") for a in available]
    known = sum(u["known_total_tokens"] for u in usage if u is not None)
    unknown = sum(u["unknown_usage_calls"] for u in usage if u is not None)
    missing = sum(u is None for u in usage)
    return {"schema_version": "kdd-route-experiment-summary-v1", "protocol_sha256": digest(protocol.model_dump()),
            "frozen_protocol_ref": protocol.frozen_protocol_ref, "experiments": rows,
            "counts": {"expected_analyses": len(expected_ids), "imported_analyses": len(available),
                       "integrity": dict(Counter(a["status"] for a in available)),
                       "acceptance_passed": sum(a["acceptance_passed"] is True for a in available),
                       "acceptance_failed": sum(a["acceptance_passed"] is False for a in available),
                       "acceptance_unreviewed": sum(a["acceptance_passed"] is None for a in available)},
            "model_usage": {"known_token_lower_bound": known, "unknown_usage_calls": unknown,
                            "analyses_without_usage_assessment": missing,
                            "total_tokens": known if not unknown and not missing and len(available) == len(expected_ids) else None},
            "scientific_or_business_pass_from_route_integrity": False}
