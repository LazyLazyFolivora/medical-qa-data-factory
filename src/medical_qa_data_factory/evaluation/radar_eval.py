"""Reviewer helper: tabulate new-radar scenario reports against private answers.

Reads DIR/reports/*.json (the analysis view written by the radar's
scripts/scenarios.py) and {A,B,C}-answers.json / {A,B,C}-key-messages.json.
It only counts and string-matches. It is NOT a PASS judge: a value appearing in
a draft does not show it was used correctly, and a flagged superseded value may
be a correctly written "old value". The human or independent reviewer decides.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

NOTICE = (
    "Helper for the human/independent reviewer, not a PASS judge. String matching "
    "only: found != correct, flagged superseded values may be legitimately written "
    "as the corrected old value, nothing here replaces reading the draft."
)
_SUPERSEDED_KEY = re.compile(r"(?:^|_)(?:prior|old|superseded|previous|original|uncorrected)(?:_|$)")
_RATIO_KEY = re.compile(r"fraction|util|growth|rate|share|ratio|pct")
_CITATION = re.compile(r"\[[A-Za-z]+\d+(?:[,，;；、\s]+[A-Za-z]+\d+)*\]")
_REF = re.compile(r"E\d+")


def _fmt(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


def _pat(text: str, percent: bool = False) -> re.Pattern[str]:
    tail = r"\s*(?:%|％|个百分点|百分点|pp)" if percent else ""
    return re.compile(r"(?<!\d,)(?<![\d.])" + re.escape(text) + r"(?!\d)(?!\.\d)" + tail)


def _appears(value: float, key: str, text: str) -> bool:
    """Whether the number is written in the text (sign ignored, separators/percent tolerated)."""
    v = abs(float(value))
    s = _fmt(v)
    pats = [_pat(s)]
    if s.startswith("0."):
        pats.append(_pat(s[1:]))
    if v >= 1000 and v == int(v):
        pats.append(_pat(f"{int(v):,}"))
    if 0 < v < 1 and _RATIO_KEY.search(key):
        pats += [_pat(_fmt(round(v * 100, n)), True) for n in (0, 1, 2)]
    return any(p.search(text) for p in pats)


def _numbers(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, list):
        return [n for item in value for n in _numbers(item)]
    return []


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _split_facts(expected: dict) -> tuple[dict, dict, list[str]]:
    """Current numeric facts, superseded numeric values, and non-numeric fact keys."""
    current: dict[str, list[float]] = {}
    superseded: dict[str, list[float]] = {}
    skipped: list[str] = []
    for key, value in (expected.get("critical_facts") or {}).items():
        numbers = _numbers(value)
        if not numbers:
            skipped.append(key)
        else:
            (superseded if _SUPERSEDED_KEY.search(key) else current)[key] = numbers
    for key, value in (expected.get("superseded") or {}).items():
        superseded[f"superseded.{key}"] = _numbers(value)
    for text in _strings(expected):  # prose such as "取代.62" names an old value too
        for match in re.findall(r"取代\s*(\.?\d+(?:\.\d+)?)", text):
            superseded.setdefault(f"text:取代{match}", [float(match)])
    return current, superseded, skipped


def _in_computations(numbers: list[float], computations: list[dict]) -> bool:
    values = [abs(c["value"]) for c in computations if isinstance(c.get("value"), (int, float))]
    return bool(numbers) and all(
        any(math.isclose(abs(n) * k, c, rel_tol=1e-4, abs_tol=1e-9) for c in values for k in (1, 100))
        for n in numbers
    )


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_report(path: Path, answers: dict | None, key_messages: dict | None) -> dict:
    view = _load(path)
    result = view.get("result") or {}
    draft = result.get("draft") or ""
    text = _CITATION.sub(" ", draft)
    usage = result.get("usage") or {}
    calls = view.get("calls") or []
    computations = result.get("computations") or []
    scenario = path.stem[:1] if path.stem[:2] in {"A-", "B-", "C-"} else None
    row: dict[str, Any] = {
        "file": path.name, "id": view.get("id"), "scenario": scenario,
        "variant": path.stem.split("-", 1)[-1], "method": view.get("method"),
        "state": view.get("state"), "report_status": result.get("report_status"),
        "alert": (result.get("assessment") or {}).get("alert"),
        "computations": computations,
        "tokens": usage.get("tokens", sum(c.get("tokens") or 0 for c in calls)),
        "llm_calls": usage.get("llm_calls", len(calls)),
        "calls_logged": len(calls), "notes": [],
    }
    by_ref = {e.get("ref"): e.get("record_id") for e in view.get("evidence") or []}
    cited_refs = sorted({r for group in re.findall(r"\[([^\]]*)\]", draft) for r in _REF.findall(group)})
    cited = {by_ref[r] for r in cited_refs if by_ref.get(r)}
    row["unresolved_refs"] = [r for r in cited_refs if not by_ref.get(r)]
    if key_messages:
        required = (key_messages.get("cases") or [{}])[0].get("required_message_ids") or []
        row["key_messages"] = {
            "required": len(required), "cited": sorted(cited & set(required)),
            "missing": [m for m in required if m not in cited],
            "missing_but_in_evidence": [m for m in required if m not in cited and m in by_ref.values()],
        }
    else:
        row["notes"].append("no key-messages file for this scenario")
    expected = ((answers or {}).get("cases") or [{}])[0].get("independent_expected")
    if expected is None:
        row["notes"].append("no answers file for this scenario; fact checks skipped")
        return row
    current, superseded, skipped = _split_facts(expected)
    row["critical_facts"] = {
        key: {"values": nums, "found": [n for n in nums if _appears(n, key, text)],
              "in_computations": _in_computations(nums, computations)}
        for key, nums in current.items()
    }
    row["non_numeric_facts_not_checked"] = skipped
    row["superseded_in_draft_for_review"] = [
        {"key": key, "value": n} for key, nums in superseded.items() for n in nums if _appears(n, key, text)
    ]
    return row


def evaluate(reports: str | Path, answers: str | Path) -> dict:
    root = Path(reports)
    root = root / "reports" if (root / "reports").is_dir() else root
    answer_dir = Path(answers)
    cache: dict[str, tuple[dict | None, dict | None]] = {}

    def side(letter: str | None) -> tuple[dict | None, dict | None]:
        if letter not in cache:
            files = [answer_dir / f"{letter}-{n}.json" for n in ("answers", "key-messages")]
            cache[letter] = tuple(_load(f) if f.is_file() else None for f in files)  # type: ignore[assignment]
        return cache[letter]

    rows = [summarize_report(p, *side(p.stem[:1] if p.stem[:2] in {"A-", "B-", "C-"} else None))
            for p in sorted(root.glob("*.json"))]
    return {"notice": NOTICE, "reports": rows, "markdown": to_markdown(rows)}


def to_markdown(rows: list[dict]) -> str:
    head = ("| report | method | status | key msgs | missing | facts | superseded to review | computations | tokens | calls |\n"
            "|---|---|---|---|---|---|---|---|---|---|")
    lines = [f"> {NOTICE}", "", head]
    for r in rows:
        km, facts = r.get("key_messages"), r.get("critical_facts")
        fact_text = "n/a" if facts is None else f"{sum(len(f['found']) == len(f['values']) for f in facts.values())}/{len(facts)}"
        absent = [k for k, f in (facts or {}).items() if len(f["found"]) < len(f["values"])]
        flagged = ", ".join(f"{s['key']}={s['value']}" for s in r.get("superseded_in_draft_for_review", [])) or "-"
        comps = "; ".join(f"{c.get('label')}={c.get('value')}{c.get('unit') or ''}" for c in r["computations"]) or "-"
        lines.append("| " + " | ".join([
            r["file"], str(r["method"]), str(r["report_status"]),
            "n/a" if km is None else f"{len(km['cited'])}/{km['required']}",
            "-" if not km or not km["missing"] else ", ".join(km["missing"]),
            fact_text + (f" (absent: {', '.join(absent)})" if absent else ""), flagged, comps,
            str(r["tokens"]), str(r["llm_calls"]),
        ]).replace("\n", " ") + " |")
    return "\n".join(lines)


def command(args) -> int:
    result = evaluate(args.reports, args.answers)
    if not result["reports"]:
        print(f"no report JSON files under {args.reports}")
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(result["markdown"])
    return 0
