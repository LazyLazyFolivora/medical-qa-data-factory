"""CLI bridge for public-only solving and separate post-solve evaluation."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

from .kdd_routes import compare_runs, evaluate_run, load_public_tasks, run_solver


def add_commands(evaluation_commands):
    routes = evaluation_commands.add_parser("routes", help="Independent multi-direction KDD route runs")
    modes = routes.add_subparsers(dest="route_mode", required=True)
    solve = modes.add_parser("solve", help="Read only public tasks and run a fresh host environment")
    solve.add_argument("--tasks", required=True)
    solve.add_argument("--policy-module", required=True, help="trusted local module exposing decide(request)")
    solve.add_argument("--selector", default=None, help="optional decide_kdd selector; exact method is recorded")
    solve.add_argument("--environment-module", required=True, help="trusted local module exposing create_environment(public_task)")
    solve.add_argument("--out", required=True)
    evaluate = modes.add_parser("evaluate", help="Read hidden targets only after solver runs complete")
    evaluate.add_argument("--runs", required=True)
    evaluate.add_argument("--targets", required=True)
    evaluate.add_argument("--out", required=True)
    compare = modes.add_parser("compare", help="Compare runs on identical frozen public task and action space")
    compare.add_argument("--left", required=True)
    compare.add_argument("--right", required=True)
    compare.add_argument("--targets", required=True)
    compare.add_argument("--out", required=True)


def _module(path):
    path = Path(path).resolve(strict=True)
    name = "_kdd_route_plugin_" + hashlib.sha256(str(path).encode() + path.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("module must be a readable local Python module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _rows(path):
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    indexed = {row["qid"]: row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError("duplicate task IDs in input")
    return indexed


def command(args):
    destination = Path(args.out)
    if destination.exists():
        raise FileExistsError(destination)
    # Opening before execution guarantees an existing result cannot be overwritten.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x") as stream:
        if args.route_mode == "solve":
            tasks = load_public_tasks(args.tasks)
            module = _module(args.policy_module)
            environment_module = _module(args.environment_module)
            policy = module.decide if args.selector is None else lambda request: module.decide_kdd(request, selector=args.selector)
            for task in tasks:
                environment = environment_module.create_environment(task.model_dump(mode="json"))
                result = run_solver(task, environment, policy)
                stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                print(json.dumps({"qid": task.qid, "provenance": task.provenance,
                                  "decision": result["final_decision"], "actions": len(result["receipts"]),
                                  "actual_spent": result["actual_spent"]}, ensure_ascii=False))
        else:
            targets = _rows(args.targets)
            left = _rows(args.runs if args.route_mode == "evaluate" else args.left)
            right = _rows(args.right) if args.route_mode == "compare" else None
            if set(left) != set(targets) or (right is not None and set(right) != set(left)):
                raise ValueError("runs and targets must contain the same task IDs")
            for qid in sorted(left):
                result = evaluate_run(left[qid], targets[qid]) if right is None else compare_runs(left[qid], right[qid], targets[qid])
                stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
            print(json.dumps({"tasks": len(left), "mode": args.route_mode, "out": str(destination.resolve())}))
    return 0
