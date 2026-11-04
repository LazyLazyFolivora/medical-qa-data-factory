"""Run an experimental entity-to-question pipeline with frozen fact-pair skills."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path

from .evidence_enrichment.pubmed import PubMedClient
from .fact_pair_pilot import FactPair
from .focused_question_pilot import Delivery, Selection, literature_cards
from .graph_question_pilot import (
    Exploration, Search, compact_record, execute, graph_material, invoke, search_round,
)
from .knowledge_graph.providers.primekg import PrimeKGSQLiteProvider
from .llm.registry import build_model_port
from .question_method_pilot import retrieve, save


PROJECT = Path(__file__).resolve().parents[2]
BASELINE = PROJECT / "docs/medical-qa-data-factory/baselines/fact-pair-v1"


def frozen_skills(baseline: Path) -> Path:
    manifest = json.loads((baseline / "manifest.json").read_text(encoding="utf-8"))
    expected = {item["snapshot_path"]: item["sha256"] for item in manifest["files"]}
    for name in ("pilot-fact-pair", "pilot-focused-composition"):
        relative = f"skills/{name}/SKILL.md"
        digest = hashlib.sha256((baseline / relative).read_bytes()).hexdigest()
        if digest != expected[relative]:
            raise ValueError(f"Frozen skill changed: {relative}")
    return baseline / "skills"


def finish(output: Path, status: str, reason: str = "", delivery: Delivery | None = None) -> dict:
    result = {"status": status, "reason": reason,
              "question": delivery.question.model_dump(mode="json") if delivery and delivery.question else None,
              "gap": delivery.gap if delivery else reason}
    save(output / "result.json", result)
    lines = ["# 实验出题结果", f"状态：{status}", reason]
    if result["question"]:
        q = result["question"]
        lines.extend(["## 题目", q["question"], "## 参考答案", q["answer"],
                      "## 难度自述（未独立测量）", q["difficulty"], "## 依据"])
        lines.extend(f"- {e['fact']}\n  {e['source']}\n  > {e['quote']}" for e in q["evidence_needs"])
    lines.extend(["## 缺口", result["gap"], "实验草稿，未独立验证。完整中间结果保存在同目录。"])
    (output / "review.md").write_text("\n\n".join(lines), encoding="utf-8")
    print(f"Result: {output / 'review.md'} ({status})", flush=True)
    return result


def compose_pair(model, output: Path, pair: FactPair, task: str, source_metadata: list[dict],
                 skills_root: Path) -> dict:
    if pair.proposal == "insufficient" or pair.observation is None:
        return finish(output, "needs_material", pair.information_gain)
    if pair.proposal == "pair" and pair.added_fact is None:
        return finish(output, "needs_material", "模型选择 pair，但没有提供第二项事实。")
    # Selection rationale is not evidence. Keep it in the saved selection only.
    evidence = {"relation_evidence": [pair.observation.model_dump(mode="json")]}
    if pair.proposal == "pair":
        evidence["property_evidence"] = [pair.added_fact.model_dump(mode="json")]
        assignment = {"information_need": task}
    else:
        assignment = {"information_need": "围绕保留的观察形成一道单事实查询，不要求补问一般功能。"}
    writing = {"assignment": assignment, "evidence_packet": evidence,
               "source_metadata": source_metadata}
    save(output / "writing-materials.json", writing)
    if pair.proposal == "pair":
        delivery = invoke(model, output, "delivery", "pilot-focused-composition", writing,
                          Delivery, skills_root=skills_root)
    else:
        delivery = invoke(model, output, "delivery", "pilot-single-composition", writing, Delivery)
    return finish(output, "draft" if delivery.question else "needs_material", delivery=delivery)


def run(args, model) -> dict:
    skills_root = frozen_skills(BASELINE)
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    save(output / "run.json", {"entity": args.entity, "baseline": "fact-pair-v1",
        "baseline_path": str(BASELINE), "graph": str(args.graph) if args.graph else None,
        "scope": "Frozen pair selection/composition; exploratory retrieval and opportunity skills are not frozen."})
    try:
        print(f"Retrieving UniProt: {args.entity}", flush=True)
        source = retrieve(args.entity)
        save(output / "source.json", source)
        overview = {**source, "record": compact_record(source["record"])}
        client = PubMedClient(max_attempts=2)
        with PrimeKGSQLiteProvider(args.graph) if args.graph else nullcontext(None) as graph:
            context = {"entity": args.entity, "overview": overview,
                "tools": {"pubmed": "English biomedical query; up to 3 abstracts.",
                          "pubmed_fetch": "Comma-separated numeric PMIDs; up to 3 abstracts."}}
            if graph:
                context["graph"] = graph_material(graph, args.entity)
                context["tools"]["primekg"] = "Expand one exact entity name; graph edges are leads."
            save(output / "exploration-materials.json", context)
            exploration = invoke(model, output, "exploration", "pilot-entity-exploration", context, Exploration)
            if any(s.tool == "primekg" for s in exploration.searches) and graph is None:
                raise ValueError("Model requested graph retrieval, but --graph was not supplied")
            results = search_round(exploration.searches, graph, client, output, "exploration")
            cards = literature_cards({"exploration_results": results})
            if not cards:
                fallback = execute(Search(tool="pubmed", query=f"{args.entity}[Title/Abstract]",
                                          purpose="Seed literature fallback"), graph, client)
                save(output / "fallback.search.json", fallback)
                cards = literature_cards({"exploration_results": [fallback]})
        if not cards:
            return finish(output, "needs_material", "检索未获得可用摘要；请查看保存的检索状态。")
        save(output / "articles.json", {"articles": cards})
        selection = invoke(model, output, "selection", "pilot-focused-opportunity",
                           {"seed_entity": args.entity, "articles": cards}, Selection)
        if selection.assignment is None:
            return finish(output, "needs_material", selection.reason)
        assignment = selection.assignment
        relation = next((card for card in cards if card["pmid"] == assignment.source_pmid), None)
        if relation is None:
            return finish(output, "needs_material", "选定机会引用了未检索到的 PMID。")
        print(f"Retrieving UniProt: {assignment.lookup_entity}", flush=True)
        lookup = source if assignment.lookup_entity.upper() == args.entity.upper() else retrieve(assignment.lookup_entity)
        save(output / "lookup.raw.json", lookup)
        lookup = {**lookup, "record": compact_record(lookup["record"])}
        material = {"information_need": assignment.information_need,
                    "relation_source": relation, "targeted_lookup": lookup}
        save(output / "fact-pair-materials.json", material)
        pair = invoke(model, output, "fact-pair", "pilot-fact-pair", material, FactPair,
                      skills_root=skills_root)
        metadata = [
            {"source": lookup["source_url"], "accession": lookup["record"]["primaryAccession"],
             "organism": lookup["record"]["organism"]},
            {"source": f"PMID:{relation['pmid']}", "title": relation.get("title", ""),
             "scope_note": "Experimental species and conditions must come from this article, not the UniProt entry."},
        ]
        return compose_pair(model, output, pair, assignment.information_need, metadata, skills_root)
    except Exception as exc:
        finish(output, "error", f"{type(exc).__name__}: {exc}")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity", required=True, help="Human gene symbol, e.g. PNMA1")
    parser.add_argument("--output", required=True, type=Path, help="New output directory")
    parser.add_argument("--graph", type=Path, help="Optional PrimeKG SQLite database")
    parser.add_argument("--config", type=Path, default=PROJECT / "config/models.json")
    parser.add_argument("--provider")
    args = parser.parse_args()
    if not args.entity.isalnum():
        parser.error("--entity must be an alphanumeric human gene symbol")
    if args.output.exists():
        parser.error("--output must be a new directory")
    if args.graph and not args.graph.is_file():
        parser.error("--graph must be an existing SQLite file")
    frozen_skills(BASELINE)
    model = build_model_port(args.provider, config_path=args.config)
    run(args, model)


if __name__ == "__main__":
    main()
