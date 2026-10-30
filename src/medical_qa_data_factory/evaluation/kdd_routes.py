"""Independent, multi-direction route evaluation from public tasks and host receipts.

No generator trace or hidden target enters the solver. Live tools remain owned
by an injected host environment. The bundled branching environment is explicitly
a synthetic contract fixture, never a claim of an integrated source backend.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from pydantic import Field, model_validator

from ..domain.common import ContractModel

TASK_SCHEMA = "kdd-route-task-v1"
RUN_SCHEMA = "kdd-route-solver-run-v1"
EVALUATOR_VERSION = "kdd-route-evaluator-1.0"
KINDS = {"document_search", "entity_relations", "graph_neighbors", "graph_path", "dataset_prepare", "analysis_run", "professional_assess"}
DIMENSIONS = {"tool_calls", "tokens", "relation_access"}
PENDING = {"prepared", "sent", "unknown", "unconfirmed"}
STATUSES = PENDING | {"completed", "failed", "not_sent"}
Direction = Literal["pipeline", "research", "business", "public_health"]
Provenance = Literal["real_task", "synthetic_fixture"]


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _cost(value: Any, *, nullable: bool = False) -> dict:
    if not isinstance(value, dict) or set(value) - DIMENSIONS:
        raise ValueError("unsupported_budget_or_cost_dimension")
    for item in value.values():
        if nullable and item is None:
            continue
        if type(item) is not int or not 0 <= item <= 10000000:
            raise ValueError("budget and cost must be bounded nonnegative integers")
    return value


class VersionRef(ContractModel):
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class RouteTask(ContractModel):
    """Public task metadata only. Runtime checks are supplied by the host."""
    schema_version: Literal["kdd-route-task-v1"] = TASK_SCHEMA
    qid: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=4000)
    direction: Direction
    as_of: str | None = None
    environment_ref: str = Field(min_length=1)
    environment_version: str = Field(min_length=1)
    provenance: Provenance
    budget: dict[str, int]
    max_steps: int = Field(default=24, ge=1, le=256)
    partition: Literal["train", "development", "test"] = "development"
    split_group: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def supported_budget(cls, data):
        if isinstance(data, dict):
            _cost(data.get("budget"))
        return data


class EnvironmentManifest(ContractModel):
    """Frozen environment identity, independent of a generation episode."""
    schema_version: Literal["kdd-route-environment-v1"] = "kdd-route-environment-v1"
    environment_ref: str = Field(min_length=1)
    environment_version: str = Field(min_length=1)
    provenance: Provenance
    backend: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    supported_kinds: list[str] = Field(min_length=1)
    source_versions: list[VersionRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def declared_support(self):
        if set(self.supported_kinds) - KINDS or len(set(self.supported_kinds)) != len(self.supported_kinds):
            raise ValueError("supported_kinds must be unique registered action kinds")
        if self.provenance == "real_task" and not self.source_versions:
            raise ValueError("real environments require actual frozen source versions")
        return self


class ObservationRequirement(ContractModel):
    id: str = Field(min_length=1)
    weight: float = Field(default=1, gt=0, allow_inf_nan=False)
    # Each inner set is conjunctive; the alternatives are disjunctive.
    acceptable_evidence_sets: list[list[str]] = Field(default_factory=list)
    acceptable_artifact_sets: list[list[VersionRef]] = Field(default_factory=list)

    @model_validator(mode="after")
    def nonempty_alternatives(self):
        sets = [*self.acceptable_evidence_sets, *self.acceptable_artifact_sets]
        if not sets or any(not item for item in sets):
            raise ValueError("requirements need nonempty independent observation alternatives")
        return self


class RouteEvaluationTarget(ContractModel):
    """Hidden post-solve assessment; unreviewed real tasks have no gold score."""
    schema_version: Literal["kdd-route-target-v1"] = "kdd-route-target-v1"
    qid: str = Field(min_length=1)
    environment_ref: str = Field(min_length=1)
    environment_version: str = Field(min_length=1)
    provenance: Provenance
    review_status: Literal["unreviewed", "independent_review", "fixture_expected"]
    reviewer: str | None = None
    review_basis_refs: list[str] = Field(default_factory=list)
    expected_outcome: Literal["sufficient", "insufficient", "unknown"] = "unknown"
    requirements: list[ObservationRequirement] = Field(default_factory=list)

    @model_validator(mode="after")
    def independent_basis(self):
        if self.review_status == "fixture_expected" and self.provenance != "synthetic_fixture":
            raise ValueError("synthetic expectations cannot become real-task gold")
        if self.review_status == "independent_review" and (not self.reviewer or not self.review_basis_refs):
            raise ValueError("independent review requires reviewer and actual basis references")
        if self.expected_outcome == "sufficient" and self.review_status != "unreviewed" and not self.requirements:
            raise ValueError("sufficient targets require independent observation requirements")
        if len({r.id for r in self.requirements}) != len(self.requirements):
            raise ValueError("requirement IDs must be unique")
        return self


def _task(value) -> RouteTask:
    return value if isinstance(value, RouteTask) else RouteTask.model_validate(value)


def load_public_tasks(path: str | Path) -> list[RouteTask]:
    path = Path(path).resolve(strict=True)
    if path.name != "kdd_route_tasks.jsonl" or path.parent.name != "public" or "hidden" in path.parts:
        raise ValueError("solver accepts only public/kdd_route_tasks.jsonl")
    tasks = [RouteTask.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]
    if len({t.qid for t in tasks}) != len(tasks):
        raise ValueError("duplicate public task IDs")
    return tasks


def write_release(tasks, targets, environments, destination: str | Path) -> dict:
    """Export supplied task/review material without manufacturing answers."""
    tasks = [_task(t) for t in tasks]
    targets = [RouteEvaluationTarget.model_validate(t) for t in targets]
    environments = [EnvironmentManifest.model_validate(e) for e in environments]
    if len({t.qid for t in tasks}) != len(tasks) or len({t.qid for t in targets}) != len(targets):
        raise ValueError("duplicate task or target")
    if {t.qid for t in targets} != {t.qid for t in tasks}:
        raise ValueError("one explicit target (possibly unreviewed) per task required")
    envs = {(e.environment_ref, e.environment_version): e for e in environments}
    if len(envs) != len(environments):
        raise ValueError("duplicate environment version")
    by_id = {t.qid: t for t in targets}
    split_groups = {}
    for task in tasks:
        target = by_id[task.qid]
        _same_binding(task, target)
        environment = envs.get((task.environment_ref, task.environment_version))
        if environment is None or environment.provenance != task.provenance:
            raise ValueError("task has no matching frozen environment")
        group = (task.provenance, task.split_group)
        if group in split_groups and split_groups[group] != task.partition:
            raise ValueError("split_group crosses partitions")
        split_groups[group] = task.partition
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    payloads = {
        "public/kdd_route_tasks.jsonl": [t.model_dump(mode="json") for t in tasks],
        "public/environment_manifests.jsonl": [e.model_dump(mode="json") for e in environments],
        "hidden/kdd_route_targets.jsonl": [t.model_dump(mode="json") for t in targets],
    }
    release_id = "kdd-release-" + digest(payloads)[:24]
    for relative, rows in payloads.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    for visibility in ("public", "hidden"):
        files = {Path(name).name: hashlib.sha256((destination / name).read_bytes()).hexdigest()
                 for name in payloads if name.startswith(visibility + "/")}
        manifest = {"schema_version": "kdd-route-release-v1", "release_id": release_id, "visibility": visibility,
                    "artifact_role": "blind_solver_input" if visibility == "public" else "post_solve_evaluation_only", "files": files}
        (destination / visibility / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"release_id": release_id, "task_count": len(tasks), "output": str(destination.resolve())}


def _same_binding(task, other):
    if (task.environment_ref, task.environment_version, task.provenance) != (other.environment_ref, other.environment_version, other.provenance):
        raise ValueError("environment/version/provenance mismatch")


class RouteEnvironment(Protocol):
    manifest: EnvironmentManifest
    def start(self, task: RouteTask) -> dict: ...
    def execute(self, action: dict) -> dict: ...


class ObservationReceiptEnvironment:
    """Host callback bridge. It holds no keys, ledger, retries or hidden agent.

    start(task) returns the current external.v2 request. execute(action) must
    persist the operation and return {receipt, observation}; observation includes
    that latest receipt in history. Exceptions are not retried by this adapter.
    """
    def __init__(self, manifest, *, start: Callable[[dict], dict], execute: Callable[[dict], dict]):
        self.manifest = EnvironmentManifest.model_validate(manifest)
        self._start, self._execute = start, execute

    def start(self, task):
        return self._start(task.model_dump(mode="json"))

    def execute(self, action):
        return self._execute(copy.deepcopy(action))


def _observation(request, task, manifest):
    if not isinstance(request, dict) or request.get("schema_version") != "query-planner.external.v2":
        raise ValueError("environment must return external.v2 observations")
    if request.get("question") != task.question or request.get("direction") != task.direction:
        raise ValueError("environment changed public question or direction")
    if request.get("budget", {}).get("limits") != task.budget or request.get("max_steps", 24) != task.max_steps:
        raise ValueError("environment changed fixed task limits")
    remaining = _cost(request.get("budget", {}).get("remaining"))
    if set(remaining) != set(task.budget) or any(v > task.budget[d] for d, v in remaining.items()):
        raise ValueError("invalid host remaining budget")
    if task.as_of is not None and request.get("scope", {}).get("as_of") != task.as_of:
        raise ValueError("environment changed public as_of")
    if any(c.get("kind") not in manifest.supported_kinds for c in request.get("candidates", [])):
        raise ValueError("environment advertised undeclared action kind")
    # A host observation is public/runtime material. Hidden task fields have no
    # role here, even when a callback can physically access other files.
    if {"evaluation_target", "gold", "gold_evidence_ids", "generation_traces", "oracle"} & set(request):
        raise ValueError("private evaluation material in solver observation")
    return copy.deepcopy(request)


def _normalized_receipt(value):
    # Compare the complete durable receipt, including future fields, so the
    # solver and its cost report cannot consume two different execution truths.
    # Only documented empty/set-like fields and unknown costs are normalized.
    normalized = copy.deepcopy(value)
    costs = _cost(normalized.get("actual_cost", {}), nullable=True)
    normalized["actual_cost"] = {dimension: costs.get(dimension) for dimension in sorted(DIMENSIONS)}
    normalized.setdefault("observed_information", {})
    for name in ("new_evidence_ids", "new_artifact_refs", "check_updates"):
        items = normalized.setdefault(name, [])
        if not isinstance(items, list):
            raise ValueError("receipt " + name + " must be a list")
        if name == "check_updates":
            for update in items:
                if not isinstance(update, dict):
                    raise ValueError("receipt check_updates must contain objects")
                for refs in ("evidence_ids", "artifact_refs"):
                    if not isinstance(update.setdefault(refs, []), list):
                        raise ValueError("receipt check update references must be lists")
                    update[refs] = sorted(update[refs], key=digest)
        normalized[name] = sorted(items, key=digest)
    return normalized


def _receipt(value, action, after):
    if not isinstance(value, dict) or value.get("status") not in STATUSES:
        raise ValueError("execution requires a durable valid receipt")
    if value.get("action_id") != action["id"] or value.get("kind") != action["kind"] or value.get("arguments") != action["arguments"]:
        raise ValueError("receipt does not match selected action")
    current = [r for r in after.get("history", []) if r.get("action_id") == action["id"]]
    if len(current) != 1 or _normalized_receipt(current[0]) != _normalized_receipt(value):
        raise ValueError("host must adopt the complete latest execution receipt before next decision")
    return copy.deepcopy(value)


def run_solver(task, environment: RouteEnvironment, policy: Callable[[dict], dict]) -> dict:
    """A fresh solver chooses each action from each actual new observation."""
    task = _task(task)
    manifest = EnvironmentManifest.model_validate(environment.manifest)
    _same_binding(task, manifest)
    observation = _observation(environment.start(task), task, manifest)
    initial_digest = digest(observation)
    steps, receipts, policy_meta, seen = [], [], None, set()
    final = {"decision": "insufficient", "reason": "solver_step_bound"}
    for _ in range(task.max_steps + 1):
        decision = policy(copy.deepcopy(observation))
        if decision.get("decision") not in {"continue", "complete", "insufficient"}:
            raise ValueError("invalid solver decision")
        if not isinstance(decision.get("policy"), dict) or not decision["policy"].get("version"):
            raise ValueError("solver must declare actual policy version")
        if policy_meta is not None and decision["policy"] != policy_meta:
            raise ValueError("policy changed during frozen run")
        policy_meta = copy.deepcopy(decision["policy"])
        step = {"observation_sha256": digest(observation), "candidate_space_sha256": digest(observation.get("candidates", [])), "decision": copy.deepcopy(decision)}
        steps.append(step)
        final = decision
        if decision["decision"] != "continue":
            if decision.get("action") is not None:
                raise ValueError("stopped solver must not propose execution")
            break
        if len(receipts) >= task.max_steps:
            final = {"decision": "insufficient", "reason": "solver_step_bound"}
            break
        if any(r.get("status") in PENDING for r in observation.get("history", [])):
            raise ValueError("solver tried to execute before pending receipt reconciliation")
        action = decision.get("action")
        if action not in observation.get("candidates", []):
            raise ValueError("solver selected an action outside current legal candidates")
        signature = digest([action["kind"], action["arguments"]])
        if action["id"] in seen or signature in seen:
            raise ValueError("solver tried to repeat a completed or unknown action")
        seen.update((action["id"], signature))
        result = environment.execute(copy.deepcopy(action))
        if not isinstance(result, dict):
            raise ValueError("execution must return receipt and new observation")
        after = _observation(result.get("observation"), task, manifest)
        receipt = _receipt(result.get("receipt"), action, after)
        receipts.append(receipt)
        step["receipt"] = receipt
        step["next_observation_sha256"] = digest(after)
        observation = after
    spent, unknown_cost = {}, []
    for dimension in task.budget:
        values = [r.get("actual_cost", {}).get(dimension) for r in receipts]
        if any(v is None for v in values):
            spent[dimension] = None
            unknown_cost.append(dimension)
        else:
            spent[dimension] = sum(values)
    return {"schema_version": RUN_SCHEMA, "trajectory_origin": "independent_solver_rollout",
            "qid": task.qid, "direction": task.direction, "provenance": task.provenance,
            "public_task_sha256": digest(task.model_dump(mode="json")), "environment": manifest.model_dump(mode="json"),
            "initial_observation_sha256": initial_digest, "policy": policy_meta,
            "oracle_used_for_planning": False, "transitions": steps, "receipts": receipts,
            "final_decision": final["decision"], "stop_reason": final.get("reason"),
            "final_observation": observation, "actual_spent": spent, "unknown_cost_dimensions": unknown_cost,
            "scope": "route_observations_and_reliability_not_scientific_or_business_validation"}


class SyntheticReceiptWorld:
    """Branchable public fixture. It cannot be declared a real backend.

    states maps public state IDs to {observation, transitions}; transitions map
    candidate IDs to {next_state, receipt}. Unlike a generator trajectory, every
    state exposes legal alternatives and the independent policy chooses a branch.
    """
    def __init__(self, manifest, states, initial_state):
        self.manifest = EnvironmentManifest.model_validate(manifest)
        if self.manifest.provenance != "synthetic_fixture":
            raise ValueError("scripted receipt worlds must remain synthetic_fixture")
        self._states, self._initial = copy.deepcopy(states), initial_state
        self._current = None

    def start(self, task):
        self._current = self._initial
        return copy.deepcopy(self._states[self._current]["observation"])

    def execute(self, action):
        node = self._states[self._current]
        if action not in node["observation"].get("candidates", []):
            raise ValueError("fixture action is not legal")
        edge = node["transitions"].get(action["id"])
        if edge is None:
            raise ValueError("fixture has no outcome for this branch; cannot invent a receipt")
        self._current = edge["next_state"]
        return {"receipt": copy.deepcopy(edge["receipt"]), "observation": copy.deepcopy(self._states[self._current]["observation"])}


def _independent_run(run):
    if run.get("schema_version") != RUN_SCHEMA or run.get("trajectory_origin") != "independent_solver_rollout" or run.get("oracle_used_for_planning") is not False:
        raise ValueError("only independent public-only solver runs can be evaluated")
    if not run.get("policy", {}).get("version"):
        raise ValueError("missing actual policy version")


def evaluate_run(run: dict, target) -> dict:
    """Called only after solving, by a process permitted to read hidden targets."""
    _independent_run(run)
    target = RouteEvaluationTarget.model_validate(target)
    env = EnvironmentManifest.model_validate(run["environment"])
    _same_binding(target, env)
    if run["qid"] != target.qid or run["provenance"] != target.provenance:
        raise ValueError("run/target identity mismatch")
    observation = run["final_observation"]
    evidence = {e["id"] for e in observation.get("evidence", [])}
    artifacts = {(a["id"], a["version"]) for a in observation.get("artifacts", [])}
    observations, coverage, correct_stop, false_complete = [], None, None, None
    if target.review_status != "unreviewed":
        for requirement in target.requirements:
            passed = any(set(group).issubset(evidence) for group in requirement.acceptable_evidence_sets)
            passed = passed or any({(r.id, r.version) for r in group}.issubset(artifacts) for group in requirement.acceptable_artifact_sets)
            observations.append({"id": requirement.id, "passed": passed, "weight": requirement.weight})
        if observations:
            coverage = sum(r["weight"] for r in observations if r["passed"]) / sum(r["weight"] for r in observations)
        if target.expected_outcome == "sufficient":
            correct_stop = run["final_decision"] == "complete" and coverage == 1
            false_complete = run["final_decision"] == "complete" and coverage != 1
        elif target.expected_outcome == "insufficient":
            correct_stop = run["final_decision"] == "insufficient"
            false_complete = run["final_decision"] == "complete"
    return {"evaluator_version": EVALUATOR_VERSION, "qid": run["qid"], "direction": run["direction"],
            "provenance": run["provenance"], "review_status": target.review_status, "policy": run["policy"],
            "evaluation_status": "unreviewed_no_gold_score" if target.review_status == "unreviewed" else "evaluated_observation_targets",
            "observation_coverage": coverage, "requirements": observations,
            "correct_observable_stop": correct_stop, "false_observable_completion": false_complete,
            "action_count": len(run["receipts"]), "actual_spent": run["actual_spent"],
            "pending_receipt_count": sum(r["status"] in PENDING for r in run["receipts"]),
            "failed_receipt_count": sum(r["status"] == "failed" for r in run["receipts"]),
            "scope": "retrieval_analysis_observations_not_scientific_or_business_gold"}


def compare_runs(left: dict, right: dict, target) -> dict:
    """Same frozen public task and action capabilities only; no baseline renaming."""
    for run in (left, right):
        _independent_run(run)
    for field in ("public_task_sha256", "environment", "initial_observation_sha256"):
        if left.get(field) != right.get(field):
            raise ValueError("not_comparable: different " + field)
    return {"evaluator_version": EVALUATOR_VERSION, "comparison": "same_frozen_task_and_action_space",
            "left": evaluate_run(left, target), "right": evaluate_run(right, target),
            "original_baseline_equivalence_claimed": False}
