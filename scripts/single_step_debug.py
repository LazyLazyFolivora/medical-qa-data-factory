"""Pause a production ReAct run after each action, preserving all inputs/outputs.

bootstrap replays a saved successful retrieval into the original pre-tool checkpoint.
step resumes that same debug checkpoint for exactly one additional action.
"""
import argparse
import json
import shutil
from pathlib import Path

from medical_qa_data_factory.generation.agent_loop import ObjectiveQuestionAgentLoop
from medical_qa_data_factory.generation.evidence_provider import EvidenceProviderCapability, EvidenceProviderResult
from medical_qa_data_factory.generation.objective_question_persistence import ObjectiveQuestionPersistence
from medical_qa_data_factory.generation.objective_question_runtime import ObjectiveRuntimeRepository
from medical_qa_data_factory.generation.react_main_agent import ReactMainAgent
from medical_qa_data_factory.llm.registry import build_model_port
from medical_qa_data_factory.web.app import _objective_provider_map


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def repository(root):
    first = sorted((root / "objective_runtime" / "commits").glob("*.json"))[0]
    run_id = json.loads(first.read_text(encoding="utf-8"))["snapshot"]["run_id"]
    return ObjectiveRuntimeRepository(root, run_id=run_id)


def capture(root, runtime, execute):
    before = runtime.load()
    number = len([a for a in before.artifacts.values() if a.artifact_type == "react_step"]) + 1
    folder = root / "single_steps" / f"{number:03d}"
    attempt = 1
    while folder.exists():
        attempt += 1
        folder = root / "single_steps" / f"{number:03d}-attempt-{attempt}"
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / "before.json", before.model_dump(mode="json"))
    old_trace = set((root / "debug_trace").glob("*.json"))
    try:
        result = execute()
    except Exception as exc:
        save(folder / "error.json", {"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        save(folder / "after.json", runtime.load().model_dump(mode="json"))
        traces = sorted(set((root / "debug_trace").glob("*.json")) - old_trace)
        if traces:
            trace_folder = folder / "trace"
            trace_folder.mkdir()
            for path in traces:
                shutil.copy2(path, trace_folder / path.name)
    after = result.runtime
    new_artifacts = [a.model_dump(mode="json") for key, a in after.artifacts.items() if key not in before.artifacts]
    save(folder / "outputs.json", new_artifacts)
    save(folder / "decisions.json", [d.model_dump(mode="json") for d in runtime.ordered_decisions()
                                     if d.decision_id not in before.decisions or d == runtime_before_pending(before)])
    print(json.dumps({"step": number, "folder": str(folder), "stop": result.stop_reason,
                      "tools": [a["payload"]["action"] for a in new_artifacts if a["artifact_type"] == "react_step"]}, ensure_ascii=False))
    return result


def runtime_before_pending(snapshot):
    # Bootstrap has one recorded decision and no executed artifacts.
    return next(iter(snapshot.decisions.values())) if not snapshot.artifacts and len(snapshot.decisions) == 1 else None


def bootstrap(source, saved_result, root):
    source_repo = repository(source)
    decisions = source_repo.ordered_decisions()
    decision = decisions[0]
    result = EvidenceProviderResult.model_validate_json(saved_result.read_text(encoding="utf-8"))
    params = decision.action_parameters
    if (decision.action != "retrieve_materials" or "searches" in params
            or params.get("fetch_full_text") or result.terminal_status.value != "SUCCEEDED"
            or params.get("provider_id") != result.request.provider_id or params.get("query") != result.request.query):
        raise ValueError("Bootstrap requires the matching successful single-query retrieval result")
    prefix = []
    for path in sorted((source / "objective_runtime" / "commits").glob("*.json")):
        state = json.loads(path.read_text(encoding="utf-8"))["snapshot"]
        prefix.append(path)
        if decision.decision_id in state["decisions"]:
            if state["artifacts"]:
                raise ValueError("Expected the original checkpoint before the first tool")
            break
    root.mkdir(parents=True, exist_ok=False)
    commits = root / "objective_runtime" / "commits"
    commits.mkdir(parents=True)
    for path in prefix:
        shutil.copy2(path, commits / path.name)
    save(root / "bootstrap.json", {"original_run": str(source.resolve()), "successful_result": str(saved_result.resolve()),
                                  "providers": json.loads((source / "experiment.json").read_text(encoding="utf-8"))["providers"],
                                  "meaning": "Same recorded first decision and successful retrieval; network failure excluded from debug context only."})
    runtime = repository(root)

    class SavedProvider:
        capability = EvidenceProviderCapability(provider_id=result.request.provider_id,
            provider_version=result.request.provider_version, side_effect_class="READ_ONLY",
            supports_idempotency=True, supports_status_lookup=False, safe_to_repeat=True)

        def retrieve(self, request):
            if request.query != result.request.query or request.operation_key != result.request.operation_key:
                raise ValueError("Replay request differs from saved request")
            return result.model_copy(update={"request": request})

    class NoNewDecision:
        def decide(self, snapshot):
            raise AssertionError("Bootstrap must reuse the original recorded decision")

    capture(root, runtime, lambda: ObjectiveQuestionAgentLoop(
        runtime=runtime, persistence=ObjectiveQuestionPersistence(root), goal=runtime.load().goal,
        main_agent=NoNewDecision(), model=None,
        providers={result.request.provider_id: SavedProvider()}, max_actions=1).run())
    for path in (source / "debug_trace").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("role") == "objective_react_agent":
            destination = root / "single_steps" / "001" / "original_model_trace"
            destination.mkdir(exist_ok=True)
            shutil.copy2(path, destination / path.name)


def step(root):
    runtime = repository(root)
    provenance_path = root / "bootstrap.json"
    if not provenance_path.exists():
        provenance_path = root / "experiment.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provider_ids = provenance.get("providers")
    if provider_ids is None:
        provider_ids = json.loads((Path(provenance["original_run"]) / "experiment.json").read_text(encoding="utf-8"))["providers"]
    providers, graph = _objective_provider_map(provider_ids, None)
    try:
        model = build_model_port()
        # A preflight exception may leave an active checkpoint without a new decision.
        # Resume paused checkpoints, but do not resume an already-active one twice.
        state = runtime.load().run_state.value
        if state == "RECOVERY_REQUIRED":
            runtime.resume_from_recovery()
        elif state not in {"CREATED", "MATERIALS_COLLECTED", "CLAIMS_PROPOSED", "HYPOTHESES_PROPOSED", "CANDIDATES_PROCESSING"}:
            raise ValueError(f"Cannot single-step terminal run: {state}")
        capture(root, runtime, lambda: ObjectiveQuestionAgentLoop(
            runtime=runtime, persistence=ObjectiveQuestionPersistence(root), goal=runtime.load().goal,
            main_agent=ReactMainAgent(model, provider_ids=sorted(providers), memory_root=root),
            model=model, providers=providers, max_actions=1).run())
    finally:
        if graph is not None:
            graph.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["bootstrap", "step"])
    parser.add_argument("root", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    if args.mode == "bootstrap":
        if args.source is None or args.result is None:
            parser.error("bootstrap requires --source and --result")
        bootstrap(args.source, args.result, args.root)
    else:
        step(args.root)
