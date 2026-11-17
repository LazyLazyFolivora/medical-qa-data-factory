"""Replay one saved controller input without changing the original run.

Only local read_artifacts is executed; other chosen actions are recorded, not run.
The model HTTP request/response and local tool outputs are preserved separately.
"""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from medical_qa_data_factory.generation.objective_question_persistence import ObjectiveQuestionPersistence
from medical_qa_data_factory.generation.objective_question_runtime import ObjectiveRuntimeSnapshot
from medical_qa_data_factory.generation.react_main_agent import ReactMainAgent
from medical_qa_data_factory.generation.react_tools import execute_react_tool
from medical_qa_data_factory.llm.registry import build_model_port
from medical_qa_data_factory.llm.run_trace import trace_run


def replay(source, before, output):
    snapshot = ObjectiveRuntimeSnapshot.model_validate_json(before.read_text(encoding="utf-8"))
    providers = json.loads((source / "experiment.json").read_text(encoding="utf-8"))["providers"]
    output.mkdir(parents=True, exist_ok=False)

    def save(name, value):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    save("provenance.json", {"source": str(source.resolve()), "before": str(before.resolve()),
         "scope": "One fresh controller decision; only local read_artifacts may execute; no original runtime writes."})
    model = build_model_port()

    class CaptureModel:
        def __getattr__(self, name):
            return getattr(model, name)

        def generate_structured(self, request, schema):
            if request.role == "objective_react_agent":
                save("agent-input.json", request.payload)
            return model.generate_structured(request, schema)

    def capture_outputs(_snapshot, _decision, _child_id, _result, artifacts, **kwargs):
        save("outputs.json", [a.model_dump(mode="json") for a in artifacts])

    with trace_run(output, snapshot.run_id):
        decision = ReactMainAgent(CaptureModel(), provider_ids=providers, memory_root=output).decide(snapshot)
        save("decision.json", decision.model_dump(mode="json"))
        if decision.action == "read_artifacts":
            loop = SimpleNamespace(providers=dict.fromkeys(providers),
                persistence=ObjectiveQuestionPersistence(source), _commit_child=capture_outputs)
            execute_react_tool(loop, snapshot, decision)
        save("result.json", {"action": decision.action, "executed": decision.action == "read_artifacts"})
    print(json.dumps({"output": str(output), "action": decision.action}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("before", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    replay(args.source, args.before, args.output)
