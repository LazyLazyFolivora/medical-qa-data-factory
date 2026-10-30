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
    imported = modes.add_parser("import-radar", help="Independently assess existing native Radar v1/v2 analysis views; execute nothing")
    imported.add_argument("--views", nargs="+", required=True)
    imported.add_argument("--provenance", choices=("real_task", "synthetic_fixture", "historical_replay"), required=True)
    imported.add_argument("--trace-validator", required=True, help="trusted native checker module exposing assert_query_policy")
    imported.add_argument("--targets", default=None, help="optional independently reviewed hidden targets; never solver input")
    imported.add_argument("--out", required=True)
    summary = modes.add_parser("summarize-experiments", help="Separate route integrity, native report status and independent fact review across frozen experiment arms")
    summary.add_argument("--protocol", required=True, help="frozen post-run experiment protocol; never solver input")
    summary.add_argument("--imports", nargs="+", required=True, help="import-radar output JSONL files")
    summary.add_argument("--reviews", default=None, help="optional post-run independent fact-review JSONL bound to exact source view hashes")
    summary.add_argument("--out", required=True)




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
        if args.route_mode == "summarize-experiments":
            from .route_experiments import summarize_experiments
            reports = [json.loads(line) for source in args.imports for line in Path(source).read_text().splitlines() if line.strip()]
            reviews = [json.loads(line) for line in Path(args.reviews).read_text().splitlines() if line.strip()] if args.reviews else []
            result = summarize_experiments(json.loads(Path(args.protocol).read_text()), reports, reviews)
            stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            print(json.dumps({"counts": result["counts"], "model_usage": result["model_usage"]}, ensure_ascii=False))
            return 0
        if args.route_mode == "import-radar":
            from .radar_routes import evaluate_radar_view
            validator_path = Path(args.trace_validator).resolve(strict=True)
            validator = _module(validator_path).assert_query_policy
            version = hashlib.sha256(validator_path.read_bytes()).hexdigest()
            targets = _rows(args.targets) if args.targets else {}
            failed = 0
            for source in args.views:
                path = Path(source).resolve(strict=True)
                # A live export may be replaced between reads; hash the exact bytes assessed.
                source_bytes = path.read_bytes()
                view = json.loads(source_bytes)
                result = evaluate_radar_view(view, provenance=args.provenance, trace_validator=validator,
                                            validator_version=version, target=targets.get(view.get("id")))
                result["source_file"] = str(path)
                result["source_file_sha256"] = hashlib.sha256(source_bytes).hexdigest()
                stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                failed += result["integrity"]["status"] != "passed"
                print(json.dumps({"analysis_id": result["analysis_id"], "direction": result.get("direction"),
                                  "integrity": result["integrity"]["status"], "metrics": result["route_metrics"]}, ensure_ascii=False))
            return 1 if failed else 0
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
