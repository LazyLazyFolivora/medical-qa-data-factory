"""Synthetic route-contract fixtures, not measured real business performance."""
import copy
import json
from pathlib import Path
import tempfile

import pytest

from medical_qa_data_factory.evaluation.kdd_routes import (
    EnvironmentManifest, ObservationReceiptEnvironment, RouteEvaluationTarget,
    RouteTask, SyntheticReceiptWorld, compare_runs, evaluate_run,
    load_public_tasks, run_solver, write_release,
)


def fixture(direction="research", kind="analysis_run"):
    task = RouteTask(qid=direction, question="Which fixed observations are sufficient?", direction=direction,
        environment_ref="public-fixture", environment_version="fixture-v1", provenance="synthetic_fixture",
        budget={"tool_calls": 1}, max_steps=2, split_group=direction)
    manifest = EnvironmentManifest(environment_ref=task.environment_ref, environment_version=task.environment_version,
        provenance=task.provenance, backend="synthetic_branching_receipts", adapter_version="1", supported_kinds=["document_search", kind])
    search = {"id": "a-low-yield", "kind": "document_search", "arguments": {"provider": "fabric", "query": "public fixture question"},
        "covers": ["outcome"], "estimated_cost": {"tool_calls": 1},
        "expected_information": [{"check_id": "outcome", "probability": 0.1, "impact": 1}]}
    compute = {"id": "z-applicable", "kind": kind, "capability_id": "fixture-method",
        "arguments": {"capability_id": "fixture-method", "capability_version": "1", "input_refs": [], "scope": {"direction": direction}, "parameters": {}},
        "covers": ["outcome"], "estimated_cost": {"tool_calls": 1}, "applicability": {"status": "eligible", "scope_match": 1},
        "expected_information": [{"check_id": "outcome", "probability": 0.9, "impact": 1}]}
    request = {"schema_version": "query-planner.external.v2", "question": task.question, "direction": direction,
        "checks": [{"id": "outcome", "status": "unknown", "importance": 1}], "evidence": [], "artifacts": [],
        "capabilities": [{"id": "fixture-method", "version": "1", "kind": kind, "directions": [direction], "input_requirements": []}],
        "history": [], "candidates": [search, compute], "budget": {"limits": dict(task.budget), "remaining": dict(task.budget)}, "max_steps": task.max_steps}
    states = {"start": {"observation": request, "transitions": {}}}
    for action, found in ((search, False), (compute, True)):
        receipt = {"action_id": action["id"], "kind": action["kind"], "arguments": action["arguments"], "status": "completed",
            "new_evidence_ids": ["fixture-outcome"] if found else [], "actual_cost": {"tool_calls": 1},
            "observed_information": {"outcome": int(found)}}
        after = copy.deepcopy(request)
        after["history"] = [receipt]; after["candidates"] = []; after["budget"]["remaining"]["tool_calls"] = 0
        if found:
            after["evidence"] = [{"id": "fixture-outcome"}]
            after["checks"][0].update(status="passed", evidence_ids=["fixture-outcome"])
        states[action["id"]] = {"observation": after, "transitions": {}}
        states["start"]["transitions"][action["id"]] = {"next_state": action["id"], "receipt": receipt}
    target = RouteEvaluationTarget(qid=task.qid, environment_ref=task.environment_ref,
        environment_version=task.environment_version, provenance=task.provenance, review_status="fixture_expected",
        expected_outcome="sufficient", requirements=[{"id": "independent-observation-target", "acceptable_evidence_sets": [["fixture-outcome"]]}])
    return task, manifest, states, target


def test_policy(request):
    """Injected deterministic test solver; never reads target or generator trace."""
    meta = {"version": "test-only-1", "selector": "fixture_test_policy", "training_status": "untrained_test_fixture"}
    if request["checks"][0]["status"] == "passed":
        return {"decision": "complete", "action": None, "policy": meta, "reason": "observed"}
    if not request["candidates"]:
        return {"decision": "insufficient", "action": None, "policy": meta, "reason": "no_action"}
    return {"decision": "continue", "action": request["candidates"][-1], "policy": meta, "reason": "fixture_choice"}


test_policy.__test__ = False


@pytest.mark.parametrize("direction", ["pipeline", "research", "business", "public_health"])
def test_four_direction_independent_branch_runs_and_comparison(direction):
    task, manifest, states, target = fixture(direction)
    good = run_solver(task, SyntheticReceiptWorld(manifest, states, "start"), test_policy)
    def other(request):
        result = test_policy(request)
        result["policy"]["selector"] = "other_fixture_policy"
        if result["decision"] == "continue": result["action"] = request["candidates"][0]
        return result
    weak = run_solver(task, SyntheticReceiptWorld(manifest, states, "start"), other)
    comparison = compare_runs(good, weak, target)
    assert comparison["left"]["observation_coverage"] == 1
    assert comparison["right"]["observation_coverage"] == 0
    assert comparison["left"]["actual_spent"] == {"tool_calls": 1}
    assert not comparison["original_baseline_equivalence_claimed"]
    assert good["trajectory_origin"] == "independent_solver_rollout"
    assert good["oracle_used_for_planning"] is False
    assert good["transitions"][0]["decision"]["action"]["id"] != weak["transitions"][0]["decision"]["action"]["id"]


@pytest.mark.parametrize("kind", ["professional_assess", "source_fetch"])
def test_live_host_callbacks_consume_receipts_without_owning_execution(kind):
    task, manifest, states, target = fixture(kind=kind)
    underlying = SyntheticReceiptWorld(manifest, states, "start")
    called = []
    def execute(action):
        called.append(action["id"])
        return underlying.execute(action)
    env = ObservationReceiptEnvironment(manifest, start=lambda public: underlying.start(RouteTask.model_validate(public)), execute=execute)
    run = run_solver(task, env, test_policy)
    assert called == ["z-applicable"]
    assert run["receipts"][0]["status"] == "completed"
    assert len(run["transitions"]) == 2


def test_public_release_is_allowlisted_and_hidden_targets_stay_separate():
    task, manifest, states, target = fixture()
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "release"
        write_release([task], [target], [manifest], output)
        loaded = load_public_tasks(output / "public/kdd_route_tasks.jsonl")
        assert loaded == [task]
        assert "fixture-outcome" not in (output / "public/kdd_route_tasks.jsonl").read_text()
        assert "hidden" not in (output / "public/dataset_manifest.json").read_text()
        with pytest.raises(FileExistsError): write_release([task], [target], [manifest], output)
        with pytest.raises(ValueError): load_public_tasks(output / "hidden/kdd_route_targets.jsonl")
        leaked = task.model_dump(); leaked["gold_evidence_ids"] = ["fixture-outcome"]
        with pytest.raises(ValueError): RouteTask.model_validate(leaked)


def test_generation_traces_and_changed_world_cannot_be_solver_scores():
    task, manifest, states, target = fixture()
    run = run_solver(task, SyntheticReceiptWorld(manifest, states, "start"), test_policy)
    generated = copy.deepcopy(run); generated["trajectory_origin"] = "generation_exploration"
    with pytest.raises(ValueError, match="independent"): evaluate_run(generated, target)
    other = copy.deepcopy(run); other["environment"]["environment_version"] = "changed"
    with pytest.raises(ValueError, match="not_comparable"): compare_runs(run, other, target)
    with pytest.raises(ValueError, match="mismatch"): evaluate_run(other, target)


def test_unreviewed_real_material_is_not_promoted_to_gold():
    task, manifest, states, target = fixture()
    task.provenance = "real_task"; manifest.provenance = "real_task"
    manifest = EnvironmentManifest.model_validate({**manifest.model_dump(), "source_versions": [{"id": "actual-host-source", "version": "fixed"}]})
    # This tests the callback contract only; it is not a real-source evaluation.
    target.provenance = "real_task"; target.review_status = "unreviewed"
    env = ObservationReceiptEnvironment(manifest.model_dump(), start=lambda public: states["start"]["observation"],
        execute=lambda action: {"receipt": states["start"]["transitions"][action["id"]]["receipt"], "observation": states[action["id"]]["observation"]})
    result = evaluate_run(run_solver(task, env, test_policy), target)
    assert result["observation_coverage"] is None
    assert result["correct_observable_stop"] is None
    assert result["evaluation_status"] == "unreviewed_no_gold_score"
    bad_target = target.model_dump(); bad_target["review_status"] = "fixture_expected"
    with pytest.raises(ValueError): RouteEvaluationTarget.model_validate(bad_target)
    with pytest.raises(ValueError): SyntheticReceiptWorld(manifest, states, "start")


def test_unknown_execution_is_not_retried_and_missing_cost_stays_unknown():
    task, manifest, states, target = fixture()
    edge = states["start"]["transitions"]["z-applicable"]
    edge["receipt"]["status"] = "unknown"; edge["receipt"].pop("actual_cost")
    after = states["z-applicable"]["observation"]
    after["history"] = [edge["receipt"]]; after["checks"][0]["status"] = "unknown"
    run = run_solver(task, SyntheticReceiptWorld(manifest, states, "start"), test_policy)
    assert len(run["receipts"]) == 1
    assert run["actual_spent"]["tool_calls"] is None
    assert evaluate_run(run, target)["pending_receipt_count"] == 1
    after["candidates"] = states["start"]["observation"]["candidates"]
    with pytest.raises(ValueError, match="pending receipt"):
        run_solver(task, SyntheticReceiptWorld(manifest, states, "start"), test_policy)


def test_receipt_mismatch_is_rejected_before_next_policy_turn():
    task, manifest, states, target = fixture()
    states["start"]["transitions"]["z-applicable"]["receipt"]["action_id"] = "wrong"
    with pytest.raises(ValueError, match="receipt does not match"):
        run_solver(task, SyntheticReceiptWorld(manifest, states, "start"), test_policy)


@pytest.mark.parametrize("field,returned,adopted", [
    ("new_evidence_ids", [], ["fixture-outcome"]),
    ("new_artifact_refs", [], [{"id": "result", "version": "1"}]),
    ("check_updates", [{"id": "outcome", "status": "unknown"}],
     [{"id": "outcome", "status": "passed", "evidence_ids": ["fixture-outcome"]}]),
    ("observed_information", {"outcome": 0}, {"outcome": 1}),
    ("actual_cost", {"tool_calls": 7}, {"tool_calls": 1}),
    ("receipt_id", "returned-receipt", "different-receipt"),
])
def test_complete_receipt_must_match_before_another_policy_turn(field, returned, adopted):
    task, manifest, states, target = fixture()
    receipt = states["start"]["transitions"]["z-applicable"]["receipt"]
    history = copy.deepcopy(receipt)
    receipt[field] = returned; history[field] = adopted
    states["z-applicable"]["observation"]["history"] = [history]
    policy_turns = []
    def policy(request):
        policy_turns.append(request)
        return test_policy(request)
    with pytest.raises(ValueError, match="complete latest execution receipt"):
        run_solver(task, SyntheticReceiptWorld(manifest, states, "start"), policy)
    assert len(policy_turns) == 1


def test_receipt_normalizes_empty_fields_reference_order_and_unknown_cost():
    task, manifest, states, target = fixture()
    receipt = states["start"]["transitions"]["z-applicable"]["receipt"]
    receipt.update(new_evidence_ids=["fixture-outcome", "source-b"],
                   new_artifact_refs=[], check_updates=[])
    history = copy.deepcopy(receipt)
    history["new_evidence_ids"].reverse()
    history.pop("new_artifact_refs"); history.pop("check_updates")
    history["actual_cost"]["tokens"] = None  # Absent and null both mean unknown, never zero.
    after = states["z-applicable"]["observation"]
    after["history"] = [history]; after["evidence"].append({"id": "source-b"})
    run = run_solver(task, SyntheticReceiptWorld(manifest, states, "start"), test_policy)
    assert run["final_decision"] == "complete"
    assert run["actual_spent"] == {"tool_calls": 1}


def test_partition_and_environment_mismatch_reject_release():
    task, manifest, states, target = fixture()
    duplicate = task.model_copy(update={"qid": "q2", "partition": "test"})
    duplicate_target = target.model_copy(update={"qid": "q2"})
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(ValueError, match="split_group"):
            write_release([task, duplicate], [target, duplicate_target], [manifest], Path(directory) / "bad")
        target.environment_version = "changed"
        with pytest.raises(ValueError, match="mismatch"):
            write_release([task], [target], [manifest], Path(directory) / "bad2")


def test_historical_replay_requires_real_frozen_versions_and_no_fixture_gold():
    task, manifest, states, target = fixture()
    manifest = {**manifest.model_dump(), "provenance": "historical_replay"}
    with pytest.raises(ValueError, match="source versions"):
        EnvironmentManifest.model_validate(manifest)
    manifest["source_versions"] = [{"id": "published-record", "version": "archived-content-hash"}]
    valid = EnvironmentManifest.model_validate(manifest)
    with pytest.raises(ValueError, match="synthetic_fixture"):
        SyntheticReceiptWorld(valid, states, "start")
    with pytest.raises(ValueError, match="synthetic expectations"):
        RouteEvaluationTarget.model_validate({**target.model_dump(), "provenance": "historical_replay"})
