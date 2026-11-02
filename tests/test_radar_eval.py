"""radar_eval on tiny synthetic reports and answers (no network, no real data)."""
from __future__ import annotations

import json

import pytest

from medical_qa_data_factory.cli import main
from medical_qa_data_factory.evaluation.radar_eval import NOTICE, evaluate


def _report(draft, *, status="REVIEW", evidence=None, computations=None, usage=None, calls=None):
    evidence = evidence if evidence is not None else [
        {"ref": "E1", "record_id": "SIM-B-11", "version_id": "v1", "title": "t1"},
        {"ref": "E2", "record_id": "SIM-B-12", "version_id": "v2", "title": "t2"},
        {"ref": "E3", "record_id": "SIM-B-13", "version_id": "v3", "title": "t3"},
    ]
    return {
        "id": "r1", "method": "planner", "question": "q", "state": "completed",
        "result": {"report_status": status, "draft": draft,
                   "assessment": {"alert": "important", "judgments": []}, "checks": [],
                   "computations": computations or [],
                   "usage": usage if usage is not None else {"llm_calls": 3, "tokens": 1000}},
        "evidence": evidence, "calls": calls or [],
    }


ANSWERS = {"cases": [{"case_id": "B-main", "independent_expected": {
    "critical_facts": {
        "corrected_hr": 0.69, "hr_ci": [0.51, 0.93], "trial_n": 240, "early_n": 12,
        "orders_cny_10k": 6400, "growth_fraction": 0.3333333, "h1_prior_orders": 4800,
        "confirmed_pathogen": None,
    },
    "wave_expectations": {"W4": "PFS HR 0.69 取代.62；非新试验"},
}}]}
KEYS = {"cases": [{"case_id": "B-main", "required_message_ids": ["SIM-B-11", "SIM-B-12", "SIM-B-13", "SIM-B-21"]}]}


@pytest.fixture
def dirs(tmp_path):
    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / "B-answers.json").write_text(json.dumps(ANSWERS), encoding="utf-8")
    (answers / "B-key-messages.json").write_text(json.dumps(KEYS), encoding="utf-8")
    run = tmp_path / "run"
    (run / "reports").mkdir(parents=True)
    return run, answers


def _write(run, name, report):
    (run / "reports" / name).write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


def _only(run, answers):
    result = evaluate(run, answers)
    assert len(result["reports"]) == 1
    return result, result["reports"][0]


def test_key_messages_map_citations_to_record_ids(dirs):
    run, answers = dirs
    # grouped, adjacent and unresolved citations; SIM-B-21 is required but never present
    _write(run, "B-baseline.json", _report("甲[E1, E2]，乙[E3][E9]，丙[A1]。"))
    result, row = _only(run, answers)
    km = row["key_messages"]
    assert row["scenario"] == "B" and row["variant"] == "baseline"
    assert km["required"] == 4 and km["cited"] == ["SIM-B-11", "SIM-B-12", "SIM-B-13"]
    assert km["missing"] == ["SIM-B-21"] and km["missing_but_in_evidence"] == []
    assert row["unresolved_refs"] == ["E9"]
    assert NOTICE in result["markdown"] and "not a PASS judge" in result["notice"]


def test_evidence_present_but_not_cited_is_distinguished(dirs):
    run, answers = dirs
    _write(run, "B-planner.json", _report("只引用[E1]。"))
    _, row = _only(run, answers)
    assert row["key_messages"]["missing"] == ["SIM-B-12", "SIM-B-13", "SIM-B-21"]
    assert row["key_messages"]["missing_but_in_evidence"] == ["SIM-B-12", "SIM-B-13"]


def test_numeric_facts_match_whole_numbers_only(dirs):
    run, answers = dirs
    draft = "校正后HR 0.69（95%CI 0.51–0.93），共1200例，早期120例，订单6,400万元，增长33.3%[E1]。"
    _write(run, "B-planner.json", _report(draft))
    _, row = _only(run, answers)
    facts = row["critical_facts"]
    assert facts["corrected_hr"]["found"] == [0.69]
    assert facts["hr_ci"]["found"] == [0.51, 0.93]
    assert facts["trial_n"]["found"] == []      # 240 is not in "1200"
    assert facts["early_n"]["found"] == []      # 12 is not in "120" or "1200"
    assert facts["orders_cny_10k"]["found"] == [6400]
    assert facts["growth_fraction"]["found"] == [0.3333333]
    assert "confirmed_pathogen" in row["non_numeric_facts_not_checked"]
    assert "h1_prior_orders" not in facts       # prior values are not required facts


def test_citation_numbers_do_not_count_as_facts(dirs):
    run, answers = dirs
    _write(run, "B-planner.json", _report("无数值[E12]"))
    _, row = _only(run, answers)
    assert row["critical_facts"]["early_n"]["found"] == []


def test_superseded_values_are_flagged_not_judged(dirs):
    run, answers = dirs
    _write(run, "B-baseline.json", _report("HR由.62更正为0.69，原订单4800万元[E1]。"))
    _, row = _only(run, answers)
    flagged = {(f["key"], f["value"]) for f in row["superseded_in_draft_for_review"]}
    assert flagged == {("h1_prior_orders", 4800), ("text:取代.62", 0.62)}
    assert "PASS" not in json.dumps(row)


def test_clean_draft_flags_nothing(dirs):
    run, answers = dirs
    _write(run, "B-baseline.json", _report("HR 0.69[E1]"))
    _, row = _only(run, answers)
    assert row["superseded_in_draft_for_review"] == []


def test_usage_computations_and_fallbacks(dirs):
    run, answers = dirs
    comps = [{"label": "增长", "value": 0.3333333, "unit": ""}, {"label": "n", "value": 240, "unit": "人"}]
    _write(run, "B-planner.json", _report("x", computations=comps, usage={"tokens": 5, "llm_calls": 2}))
    _write(run, "B-baseline.json", _report("x", usage={}, calls=[{"tokens": 7}, {"tokens": None}, {"tokens": 3}]))
    result = evaluate(run, answers)
    planner, baseline = result["reports"][1], result["reports"][0]
    assert (baseline["tokens"], baseline["llm_calls"], baseline["calls_logged"]) == (10, 3, 3)
    assert (planner["tokens"], planner["llm_calls"]) == (5, 2)
    assert planner["critical_facts"]["trial_n"]["in_computations"] is True
    assert planner["critical_facts"]["early_n"]["in_computations"] is False
    assert "增长=0.3333333" in result["markdown"]


def test_scenario_without_answers_still_reports_status(dirs):
    run, answers = dirs
    _write(run, "D-planner.json", _report("x", status="PASS"))
    _, row = _only(run, answers)
    assert row["scenario"] is None and row["report_status"] == "PASS"
    assert "critical_facts" not in row and len(row["notes"]) == 2


def test_cli_writes_json_and_prints_table(dirs, tmp_path, capsys):
    run, answers = dirs
    _write(run, "B-planner.json", _report("HR 0.69[E1]"))
    out = tmp_path / "out" / "eval.json"
    assert main(["eval", "radar", "--reports", str(run), "--answers", str(answers), "--out", str(out)]) == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["reports"][0]["file"] == "B-planner.json" and NOTICE in saved["notice"]
    assert "| B-planner.json |" in capsys.readouterr().out


def test_cli_fails_when_no_reports(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    code = main(["eval", "radar", "--reports", str(tmp_path / "empty"), "--answers", str(tmp_path), "--out", str(tmp_path / "o.json")])
    assert code == 2 and not (tmp_path / "o.json").exists()
