"""Regression coverage for lightweight task summaries and cached chronology."""
import json
from types import SimpleNamespace

from medical_qa_data_factory.web.objective_projection import (
    _commit_decision_ids, _latest_decision, objective_decision_order,
)


def test_summary_reads_only_recent_commits_and_preserves_order(tmp_path):
    commits = tmp_path / "objective_runs" / "run" / "objective_runtime" / "commits"
    commits.mkdir(parents=True)
    def write(number, ids):
        path = commits / f"{number:08d}.json"
        path.write_text(json.dumps({"snapshot": {"decisions": dict.fromkeys(ids, {})}}), encoding="utf-8")
    for number in range(1, 20):
        write(number, ["z-old"])
    write(20, ["a-new", "z-old"])
    write(21, ["a-new", "z-old"])
    snapshot = SimpleNamespace(decisions={"a-new": "new", "z-old": "old"})
    _commit_decision_ids.cache_clear()
    assert _latest_decision(tmp_path, "run", snapshot) == "new"
    assert _commit_decision_ids.cache_info().misses == 3
    assert objective_decision_order(tmp_path, "run", snapshot) == ["old", "new"]
    misses = _commit_decision_ids.cache_info().misses
    assert objective_decision_order(tmp_path, "run", snapshot) == ["old", "new"]
    assert _commit_decision_ids.cache_info().misses == misses
    write(22, ["a-new", "b-newest", "z-old"])
    snapshot.decisions["b-newest"] = "newest"
    assert _latest_decision(tmp_path, "run", snapshot) == "newest"
    assert objective_decision_order(tmp_path, "run", snapshot) == ["old", "new", "newest"]


def test_changed_commit_invalidates_ids_cache(tmp_path):
    commits = tmp_path / "objective_runs" / "run" / "objective_runtime" / "commits"
    commits.mkdir(parents=True)
    path = commits / "00000001.json"
    path.write_text(json.dumps({"snapshot": {"decisions": {"old": {}}}}), encoding="utf-8")
    snapshot = SimpleNamespace(decisions={"old": "old", "new": "new"})
    assert objective_decision_order(tmp_path, "run", snapshot) == ["old", "new"]
    path.write_text(json.dumps({"snapshot": {"decisions": {"new": {}, "old": {}}}}), encoding="utf-8")
    assert objective_decision_order(tmp_path, "run", snapshot) == ["new", "old"]
