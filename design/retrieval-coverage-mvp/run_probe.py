"""Fixed-evidence retrieval-coverage experiment; does not publish candidates."""
import copy
import importlib.util
import json
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", default="v1", choices=("v1", "v2"))
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    prior = project / "data/experiments/fixed-evidence-design-20261111/run_probe.py"
    # The prior harness resolves its saved request relative to the project root.
    import os
    os.chdir(project)
    spec = importlib.util.spec_from_file_location("saved_probe", prior)
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    harness.ROOT = project / "data/experiments/retrieval-coverage-20261111"
    harness.ROOT.mkdir(parents=True, exist_ok=True)
    source_path = project / "data/experiments/hard-question-probe-20261111/full-sources.json"
    source_text = source_path.read_text(encoding="utf8")
    snapshot = harness.ROOT / "sources.json"
    if snapshot.exists() and snapshot.read_text(encoding="utf8") != source_text:
        raise RuntimeError("Existing experiment has different sources; use a new directory")
    snapshot.write_text(source_text, encoding="utf8")
    prompt = Path(__file__).with_name("design.md").read_text(encoding="utf8")
    for intent in ("focused", "parallel", "adaptive"):
        sources = json.loads(source_text)
        if args.revision == "v2" and intent == "focused":
            sources = [sources[0]]
        body = {k: copy.deepcopy(v) for k, v in harness.BASE.items() if k != "messages"}
        body["messages"] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps({
                "target_entity": "EGFR", "retrieval_intent": intent,
                "sources": sources,
            }, ensure_ascii=False)},
        ]
        name = intent if args.revision == "v1" else f"{intent}-{args.revision}"
        harness.call(name, body)


if __name__ == "__main__":
    main()
