"""Summarize committed publications and their supporting evidence from real runs."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from hashlib import sha256
from pathlib import Path


def artifact_payload(artifacts: dict, artifact_id: str | None, kind: str) -> dict:
    """Resolve an artifact only when its recorded type matches the expected link."""
    envelope = artifacts.get(artifact_id, {})
    return envelope["payload"] if envelope.get("artifact_type") == kind else {}


def source_record(run: Path, material: dict) -> dict:
    """Return a citation and its path relative to the run, when available locally."""
    raw_ref = material.get("raw_content_ref")
    title = None
    relative = Path(raw_ref) if raw_ref else None
    raw_path = (run / relative).resolve() if relative and not relative.anchor else None
    if raw_path and raw_path.is_relative_to(run) and raw_path.is_file():
        with raw_path.open(encoding="utf-8", errors="replace") as source:
            title = source.readline(512).strip() or None
            if title == "[TITLE]":
                title = source.readline(512).strip() or None
    else:
        raw_ref = None
    return {
        "material_id": material.get("material_id"),
        "provider_id": material.get("provider_id"),
        "source_record_id": material.get("source_record_id"),
        "material_type": material.get("material_type"),
        "raw_content_ref": raw_ref,
        "title": title,
    }


def supporting_evidence(
    run: Path, artifacts: dict, resolution: dict
) -> tuple[list[dict], list[str]]:
    """Follow the verified resolution's anchor IDs back to recorded sources."""
    sources = {}
    source_texts: dict[str, str] = {}
    gaps = []
    for anchor_id in resolution.get("supporting_anchor_ids", []):
        anchor = artifact_payload(artifacts, anchor_id, "evidence_anchor")
        material_id = anchor.get("material_id")
        material = artifact_payload(artifacts, material_id, "evidence_material")
        if not material:
            gaps.append(f"Missing evidence anchor or material for {anchor_id}")
            continue
        if material_id not in sources:
            source = source_record(run, material)
            sources[material_id] = {**source, "anchors": []}
            if not source["raw_content_ref"]:
                gaps.append(f"Missing local source content for {material_id}")
            else:
                try:
                    raw = (run / source["raw_content_ref"]).read_bytes()
                    source_texts[material_id] = raw.decode("utf-8")
                except (OSError, UnicodeError):
                    gaps.append(f"Cannot read UTF-8 source content for {material_id}")
                else:
                    if sha256(raw).hexdigest() != material.get("content_hash"):
                        gaps.append(f"Local source content hash does not match {material_id}")
        if material_id in source_texts:
            start, end = anchor.get("start_offset"), anchor.get("end_offset")
            text = source_texts[material_id]
            quote = anchor.get("quoted_text")
            if (not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(text)
                    or not isinstance(quote, str) or text[start:end] != quote
                    or sha256(quote.encode("utf-8")).hexdigest() != anchor.get("quote_hash")):
                gaps.append(f"Source text does not match published evidence anchor {anchor_id}")
        anchor_fields = (
            "anchor_id", "document_id", "section_or_field", "start_offset", "end_offset",
            "quote_hash",
        )
        sources[material_id]["anchors"].append({key: anchor.get(key) for key in anchor_fields})
    if not sources:
        gaps.append("No supporting evidence material resolved from the published answer")
    return list(sources.values()), gaps


def committed_publications(state: dict) -> tuple[dict, list[str]]:
    """Match allowed release reports through their draft, contract and resolution."""
    artifacts = state["artifacts"]
    published = {hid for hid, status in state["hypotheses"].items() if status == "PUBLISHED"}
    by_hypothesis: dict[str, list[tuple[dict, dict, dict]]] = {}
    gaps = []
    for envelope in artifacts.values():
        if envelope.get("artifact_type") != "objective_validation_report":
            continue
        report = envelope["payload"]
        if not report.get("release_allowed") or report.get("verdict") != "PASS":
            continue
        draft = artifact_payload(artifacts, report.get("question_id"), "objective_question_draft")
        contract = artifact_payload(artifacts, draft.get("contract_id"), "frozen_question_contract")
        resolution = artifact_payload(artifacts, contract.get("resolution_id"), "answer_resolution")
        hid = resolution.get("hypothesis_id")
        if hid not in published:
            report_id = envelope["artifact_id"]
            gaps.append(f"Release report {report_id} has no linked PUBLISHED hypothesis")
            continue
        by_hypothesis.setdefault(hid, []).append((report, draft, resolution))

    matched = {}
    for hid in sorted(published):
        records = by_hypothesis.get(hid, [])
        if len(records) == 1:
            matched[hid] = records[0]
        else:
            gaps.append(f"PUBLISHED hypothesis {hid} has {len(records)} linked release reports")
    return matched, gaps


def answer_review(artifacts: dict, report: dict, hypothesis_id: str) -> dict | None:
    """Return only the review authorized by the publication report."""
    review_id = report.get("answer_audit_id")
    audit = artifact_payload(artifacts, review_id, "final_question_review")
    if audit.get("hypothesis_id") != hypothesis_id or not audit.get("review"):
        return None
    review = audit["review"]
    review_fields = (
        "recommended_action", "support_directly_entails_answer", "answer_matches_blind", "explanation",
    )
    return {"artifact_id": review_id, **{key: review.get(key) for key in review_fields}}


def summarize_publication(
    run: Path, artifacts: dict, hid: str, record: tuple[dict, dict, dict], assessment: dict
) -> dict:
    """Connect one published question and answer to review, evidence and annotation."""
    report, draft, resolution = record
    proposed_answer = artifact_payload(artifacts, hid, "question_hypothesis").get(
        "proposed_answer_values"
    )
    sources, gaps = supporting_evidence(run, artifacts, resolution)
    review = answer_review(artifacts, report, hid)
    if review is None:
        gaps.append(f"Missing or mismatched final review {report.get('answer_audit_id')}")
    if not draft.get("question_text") or not draft.get("answer_values"):
        gaps.append("Published draft is missing question text or answer values")
    # A retrieval-backed report may identify a provider instead of a document.
    linked_references = {source["provider_id"] for source in sources}
    linked_references.update(
        anchor["document_id"] for source in sources for anchor in source["anchors"]
    )
    if set(report.get("evidence_used", [])) - linked_references:
        gaps.append("Some release-report evidence references do not resolve to supporting anchors")
    input_digest = report.get("input_digest")
    assessment_matches = input_digest and assessment.get("publication_input_digest") == input_digest
    return {
        "hypothesis_id": hid,
        "draft_id": draft.get("draft_id"),
        "publication_report_id": report.get("report_id"),
        "publication_input_digest": input_digest,
        "question": draft.get("question_text"),
        "answer": draft.get("answer_values", []),
        "proposal_answer_differs": proposed_answer != draft.get("answer_values"),
        "sources": sources,
        "evidence_used": report.get("evidence_used", []),
        "final_reviews": [review] if review else [],
        "traceability_gaps": gaps,
        "manual_assessment": assessment if assessment_matches else None,
        "unbound_assessment": assessment if assessment and not assessment_matches else None,
    }


def summarize_run(run: Path, assessments: dict) -> dict:
    """Read a saved run and distinguish requested slots, drafts and publication records."""
    run = run.resolve()
    state = json.loads((run / "after.json").read_text(encoding="utf-8"))
    provenance_path = run / "provenance.json"
    provenance = {}
    if provenance_path.is_file():
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    replay = bool(provenance.get("replay") or provenance.get("checkpoint"))
    inherited = {}
    before_path = run / "step-01-before.json"
    if replay and before_path.is_file():
        before = json.loads(before_path.read_text(encoding="utf-8"))
        baseline_records, _ = committed_publications(before)
        inherited = {hid: (report.get("report_id"), report.get("input_digest"))
                     for hid, (report, _, _) in baseline_records.items()}
    records, gaps = committed_publications(state)
    questions = [
        summarize_publication(
            run, state["artifacts"], hid, record, assessments.get(f"{state['run_id']}|{hid}", {})
        )
        for hid, record in records.items()
    ]
    for question in questions:
        hid = question["hypothesis_id"]
        question["inherited_from_checkpoint"] = inherited.get(hid) == (
            question["publication_report_id"], question["publication_input_digest"],
        )
    state_counts = Counter(state["hypotheses"].values())
    candidate_texts = {
        envelope["payload"].get("question_intent")
        for envelope in state["artifacts"].values()
        if envelope.get("artifact_type") == "question_hypothesis"
    }
    trace = run / "debug_trace"
    trace_files = list(trace.glob("*.json"))
    model_requests = sum(item.name.endswith("-model_request.json") for item in trace_files)
    trace_times = [int(item.name.split("-", 1)[0]) for item in trace_files
                   if item.name.split("-", 1)[0].isdigit()]
    published_texts = {question["question"] for question in questions if question["question"]}
    return {
        "run_dir": str(run),
        "run_id": state["run_id"],
        "target_entity": state["goal"]["target_entity"],
        "checkpoint": provenance.get("checkpoint"),
        "mode": "replay" if replay else "fresh" if provenance else "unknown",
        "run_state": state.get("run_state"),
        "stop_reason": state.get("stop_reason"),
        "target_question_count": state["goal"].get("question_count_upper_bound"),
        "candidate_version_count": len(state["hypotheses"]),
        "unique_candidate_question_text_count": len(candidate_texts - {None, ""}),
        "hypothesis_state_counts": dict(state_counts),
        "published_question_count": len(questions),
        "newly_published_question_count": sum(not item["inherited_from_checkpoint"] for item in questions),
        "unique_published_question_text_count": len(published_texts),
        "recorded_model_request_count": model_requests if trace.is_dir() else None,
        "recorded_elapsed_seconds": round((max(trace_times) - min(trace_times)) / 1e9, 1)
        if len(trace_times) >= 2 else None,
        "traceability_gaps": gaps,
        "questions": questions,
    }


def build_manifest(runs: list[dict]) -> dict:
    """Count release yields and ratings bound to the published content."""
    questions = [question for run in runs for question in run["questions"]
                 if not question.get("inherited_from_checkpoint")]
    ratings = [question["manual_assessment"] or {} for question in questions]
    quality_by_mode = {}
    labels = ("not_usable", "borderline_basic", "basic_usable")
    for run in runs:
        counts = quality_by_mode.setdefault(run["mode"], Counter())
        counts["runs"] += 1
        for question in run["questions"]:
            if question.get("inherited_from_checkpoint"):
                continue
            assessment = question["manual_assessment"] or {}
            pair = [assessment.get(dimension, {}).get("label") for dimension in ("question", "answer")]
            counts["published"] += 1
            if all(label in labels for label in pair):
                counts[min(pair, key=labels.index)] += 1
            else:
                counts["unassessed"] += 1
    return {
        "schema_version": "multientity-quality-summary-v4",
        "target_question_count": sum(run["target_question_count"] or 0 for run in runs),
        "published_question_count": len(questions),
        "assessment_counts": {
            dimension: dict(Counter(
                rating.get(dimension, {}).get("label") or "unassessed" for rating in ratings
            )) for dimension in ("question", "answer")
        },
        "unbound_assessment_count": sum(1 for question in questions if question["unbound_assessment"]),
        "pair_quality_by_run_mode": {mode: dict(counts) for mode, counts in quality_by_mode.items()},
        "runs": runs,
    }


def _markdown_question(index: int, item: dict) -> list[str]:
    lines = [f"### {index}. {item['question']}", ""]
    if item.get("inherited_from_checkpoint"):
        lines.append("- 从起始快照继承的发布")
    lines.append("- 答案")
    for part in item["answer"]:
        answer = "\n    ".join(part.splitlines())
        lines.append(f"  - {answer}")
    assessment = item["manual_assessment"] or {}
    for dimension, name in (("question", "题干"), ("answer", "答案")):
        rating = assessment.get(dimension, {})
        detail = f"{rating.get('label', '待审')} {rating.get('note', '')}".rstrip()
        lines.append(f"- {name}评分 · {detail}")
    legacy = item["unbound_assessment"]
    if legacy:
        detail = f"{legacy.get('label', '待审')} {legacy.get('note', '')}".rstrip()
        lines.append(f"- 旧评语（待复核）· {detail}")
    for source in item["sources"]:
        title = source["title"] or source["material_id"]
        lines.append(f"- 来源 · {source['source_record_id']} — {title}")
    actions = [str(review["recommended_action"]) for review in item["final_reviews"]]
    lines.append(f"- 发布所用答案评审 · {', '.join(actions) or '缺失'}")
    lines.extend(f"- 追溯缺口 · {gap}" for gap in item["traceability_gaps"])
    return lines + [""]


def markdown(manifest: dict) -> str:
    """Format linked publication records as a Chinese review sheet."""
    lines = ["# 多实体真实问题证据清单", "", "题干和答案评分按发布版本的摘要校验值绑定。", ""]
    for run in manifest["runs"]:
        lines.extend([
            f"## {run['target_entity']} — {Path(run['run_dir']).name}",
            "",
            f"发布 {run['published_question_count']}/{run['target_question_count']} 题，"
            f"共记录 {run['candidate_version_count']} 个候选版本，涉及"
            f" {run['unique_candidate_question_text_count']} 种不同的题干文本。",
            f"运行类型 `{run['mode']}`。停止记录 `{run['stop_reason']}`。",
            "",
        ])
        if run["newly_published_question_count"] != run["published_question_count"]:
            lines.extend([f"本次新增发布 {run['newly_published_question_count']} 题。", ""])
        if run["recorded_model_request_count"] is not None:
            elapsed = run["recorded_elapsed_seconds"]
            duration = f"，首尾事件相隔 {elapsed} 秒" if elapsed is not None else ""
            lines.extend([f"运行轨迹记录了 {run['recorded_model_request_count']} 次模型请求{duration}。", ""])
        for gap in run["traceability_gaps"]:
            lines.append(f"- 追溯缺口 · {gap}")
        if run["traceability_gaps"]:
            lines.append("")
        for index, item in enumerate(run["questions"], 1):
            lines.extend(_markdown_question(index, item))
    lines.extend([
        "## 汇总", "",
        f"- 各运行新增发布合计 {manifest['published_question_count']} 题",
        f"- 题干评分 · {manifest['assessment_counts']['question']}",
        f"- 答案评分 · {manifest['assessment_counts']['answer']}",
        f"- 待复核旧评语 · {manifest['unbound_assessment_count']} 条", "",
    ])
    mode_labels = {"fresh": "从头运行", "replay": "检查点重放", "unknown": "来源模式未知"}
    lines.extend(["### 按运行模式核对题干和答案", "",
                  "每题取两项人工评分中较低的等级。缺少任一评分时记为待审。", "",
                  "| 运行模式 | 运行数 | 新增发布 | 基本可用 | 需局部修改 | 不可用 | 待审 |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for mode, counts in manifest["pair_quality_by_run_mode"].items():
        cells = [mode_labels.get(mode, mode), *(str(counts.get(key, 0)) for key in (
            "runs", "published", "basic_usable", "borderline_basic", "not_usable", "unassessed",
        ))]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    """Produce both JSON and Markdown summaries for explicitly selected runs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument(
        "--assessments", type=Path,
        help="JSON keyed by run_id|hypothesis_id. Each rating has publication_input_digest, "
             "question and answer (each with label and optional note).",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    assessments = {}
    if args.assessments:
        assessments = json.loads(args.assessments.read_text(encoding="utf-8"))
    runs = list(dict.fromkeys(run.resolve() for run in args.runs))
    manifest = build_manifest([summarize_run(run, assessments) for run in runs])
    args.output.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(manifest, ensure_ascii=False, indent=2)
    (args.output / "manifest.json").write_text(serialized + "\n", encoding="utf-8")
    (args.output / "report.md").write_text(markdown(manifest), encoding="utf-8")


if __name__ == "__main__":
    main()
