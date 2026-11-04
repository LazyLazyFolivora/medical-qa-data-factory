"""Three-stage skill experiment with bounded graph and literature exploration."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from .domain.common import ContractModel
from .evidence_enrichment.pubmed import PubMedClient
from .knowledge_graph.contracts import EntityLookup, NeighborQuery
from .knowledge_graph.providers.primekg import PrimeKGSQLiteProvider
from .llm.contracts import StructuredGenerationRequest
from .llm.registry import build_model_port
from .llm.errors import ModelError
from .question_method_pilot import retrieve, save


SKILLS = Path(__file__).resolve().parents[2] / "skills"
RELATIONSHIP_EXPERIMENT = (
    "本轮是关系连接题的方法实验。选择一个已由材料明确支持的关系，"
    "找出其中值得补查的对象，再查它的相关功能或属性，尝试设计一道"
    "需要连接这两项事实的自然问题。可使用明确举例的答案要求。"
    "已有直接事实题只作为对照，不能替代本轮关系连接目标；"
    "找不到合适连接时明确保留缺口，不能制造关系或机制。"
    "最多交付一道关系题及一道简单对照题。"
)


class Search(ContractModel):
    tool: Literal["primekg", "pubmed", "pubmed_fetch", "uniprot"]
    query: str
    purpose: str


class Exploration(ContractModel):
    directions: list[str]
    searches: list[Search] = Field(default_factory=list, max_length=2)


class Opportunity(ContractModel):
    need: str
    operation: str
    bridge_fact: str = ""
    next_entity: str = ""
    lookup_property: str = ""
    needed_facts: list[str]
    dependency: str
    existing_evidence: list[str]
    gap: str


class Opportunities(ContractModel):
    opportunities: list[Opportunity] = Field(default_factory=list, max_length=4)
    searches: list[Search] = Field(default_factory=list, max_length=2)


class EvidenceNeed(ContractModel):
    fact: str
    source: str
    quote: str


class Question(ContractModel):
    question: str
    answer: str
    operation: str
    evidence_needs: list[EvidenceNeed]
    dependency: str
    completion: str
    difficulty: str
    shortcut: str


class Questions(ContractModel):
    questions: list[Question] = Field(default_factory=list, max_length=3)
    remaining_opportunities: list[str]


def graph_material(graph, name: str) -> dict:
    resolution = graph.resolve_entity(EntityLookup(mention=name))
    if resolution.selected is None:
        return {"query": name, "resolution": resolution.model_dump(mode="json")}
    result = graph.neighbors(NeighborQuery(entity=resolution.selected, limit=1000))
    # Reverse copies of a relation are exploration duplicates, not new evidence.
    seen = set()
    neighbors = []
    for edge in result.edges:
        other = edge.tail if edge.head.canonical_id == resolution.selected.canonical_id else edge.head
        key = (edge.relation, other.canonical_id)
        if key in seen:
            continue
        seen.add(key)
        neighbors.append({"relation": edge.relation,
                          "head": edge.head.name, "tail": edge.tail.name})
    return {"query": name, "entity": resolution.selected.model_dump(mode="json"),
            "snapshot": result.snapshot.model_dump(mode="json"),
            "trace": result.trace.model_dump(mode="json"),
            "truncated": result.truncated, "neighbors": neighbors,
            "meaning": "Graph relations are exploration leads; no causal or completeness claim."}


def execute(search: Search, graph, client) -> dict:
    if search.tool == "primekg":
        material = graph_material(graph, search.query)
    elif search.tool == "uniprot":
        material = retrieve(search.query)
        material["record"] = compact_record(material["record"])
    elif search.tool == "pubmed_fetch":
        pmids = [value.strip() for value in search.query.split(",")]
        if not pmids or any(not value.isdigit() for value in pmids):
            raise ValueError("pubmed_fetch requires comma-separated numeric PMIDs")
        material = {"fetch": client.fetch_result(pmids, limit=3).model_dump(mode="json")}
    else:
        found = client.search_result(search.query, limit=3)
        fetched = client.fetch_result(found.pmids, limit=3) if found.pmids else None
        material = {"search": found.model_dump(mode="json"),
                    "fetch": fetched.model_dump(mode="json") if fetched else None}
    return {"request": search.model_dump(), "material": material}


def compact_record(record: dict) -> dict:
    # Keep annotation text and its evidence links. Long interaction experiment
    # listings duplicate the graph overview and overwhelm this small experiment.
    return {"primaryAccession": record.get("primaryAccession"),
            "proteinDescription": record.get("proteinDescription"),
            "genes": record.get("genes"),
            "organism": record.get("organism", {}).get("scientificName"),
            "comments": [item for item in record.get("comments", [])
                         if item.get("commentType") != "INTERACTION"],
            "omitted_sections": ["INTERACTION", "references"]}


def invoke(model, output: Path, stage: str, skill: str, payload: dict, schema, *, skills_root: Path = SKILLS):
    payload = present_materials(payload)
    instruction = (skills_root / skill / "SKILL.md").read_text(encoding="utf-8")
    version = hashlib.sha256(instruction.encode()).hexdigest()
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    request = StructuredGenerationRequest(
        invocation_id=f"graph-pilot.{stage}.{digest[:16]}",
        role="question_method_pilot", system_prompt_version="question-method-pilot-v1",
        skill_id=skill, skill_version=version, skill_instruction=instruction,
        payload={"stage": stage, **payload},
        idempotency_key=f"graph-pilot|{stage}|{version}|{digest}",
    )
    save(output / f"{stage}.request.json", request.model_dump(mode="json"))
    try:
        response = model.generate_structured(request, schema)
    except ModelError as exc:
        save(output / f"{stage}.error.json", exc.as_dict())
        raise
    save(output / f"{stage}.result.json", response.model_dump(mode="json"))
    print(f"Completed {stage}", flush=True)
    return schema.model_validate(response.parsed)


def present_materials(value):
    """Bound neighborhood presentation; full retrievals remain in saved files."""
    if isinstance(value, list):
        return [present_materials(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: present_materials(item) for key, item in value.items()}
    if "neighbors" in value:
        groups = {}
        for edge in value["neighbors"]:
            groups.setdefault(edge["relation"], []).append(
                f"{edge['head']} -> {edge['tail']}"
            )
        result.pop("neighbors")
        result["neighbor_samples"] = {
            relation: {"returned_count": len(edges), "examples": edges[:24],
                       "omitted_from_prompt": max(0, len(edges) - 24)}
            for relation, edges in groups.items()
        }
        result["presentation_note"] = "Per-relation samples, not complete candidate sets."
    return result


def search_round(searches, graph, client, output: Path, stage: str) -> list[dict]:
    results = []
    for index, search in enumerate(searches):
        print(f"Retrieving {search.tool}: {search.query}", flush=True)
        # Save each completed lookup immediately so partial runs are inspectable.
        result = execute(search, graph, client)
        save(output / f"{stage}.search-{index + 1}.json", result)
        results.append(result)
    return results


def write_review(output: Path, context: dict, exploration, opportunities, result) -> None:
    lines = [f"# {context['entity']} 图谱探索出题实验", "实验草稿，供人工审阅。",
             "## 探索方向", *exploration.directions]
    for index, item in enumerate(opportunities.opportunities, 1):
        lines += [f"## 机会 {index}：{item.need}", f"操作：{item.operation}",
                  f"已知关系：{item.bridge_fact}",
                  f"补查对象与属性：{item.next_entity} / {item.lookup_property}",
                  "需要的事实：" + "；".join(item.needed_facts),
                  f"依赖：{item.dependency}", f"缺口：{item.gap}"]
    lines += ["## 实际查询"]
    for stage in ("exploration_results", "follow_up_results"):
        for lookup in context.get(stage, []):
            request = lookup["request"]
            lines += [f"- {request['tool']}：{request['query']} — {request['purpose']}"]
    for index, item in enumerate(result.questions, 1):
        lines += [f"## 题目 {index}", item.question, f"答案：{item.answer}",
                  f"操作：{item.operation}", f"依赖：{item.dependency}",
                  f"完成条件：{item.completion}", f"难度：{item.difficulty}",
                  f"短解：{item.shortcut}"]
        for need in item.evidence_needs:
            lines += [f"事实：{need.fact}", f"来源：{need.source}", f"依据：{need.quote}"]
    lines += ["## 保留的探索方向", *result.remaining_opportunities]
    (output / "review.md").write_text("\n\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, help="UniProt source.json from first pilot")
    parser.add_argument("--graph", type=Path)
    parser.add_argument("--replay-run", type=Path, help="Redesign from saved materials without retrieval")
    parser.add_argument("--replan-run", type=Path, help="Reuse exploration, then choose new opportunities and follow-up searches")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--provider")
    parser.add_argument("--focus", choices=["open", "relationship"], default="open")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if args.replay_run and args.replan_run:
        parser.error("Choose either --replay-run or --replan-run")
    if args.replan_run and args.graph is None:
        parser.error("--replan-run requires --graph for follow-up lookups")
    if args.replay_run or args.replan_run:
        previous = args.replay_run or args.replan_run
        context = json.loads((previous / "materials.json").read_text(encoding="utf-8"))
        context["experiment_goal"] = (RELATIONSHIP_EXPERIMENT if args.focus == "relationship"
                                      else "探索材料能支持的自然问题，不要求特定题型。")
        exploration_result = json.loads(
            (previous / "exploration.result.json").read_text(encoding="utf-8"))
        exploration = Exploration.model_validate(exploration_result["parsed"])
        args.output.mkdir(parents=True, exist_ok=False)
        save(args.output / "replay.json", {"source_run": str(previous.resolve()),
                                          "replan": bool(args.replan_run)})
        save(args.output / "exploration.result.json", exploration_result)
        model = build_model_port(args.provider, config_path=args.config)
        if args.replan_run:
            context.pop("opportunities", None)
            context.pop("follow_up_results", None)
            opportunities = invoke(model, args.output, "opportunities", "pilot-question-opportunities", context, Opportunities)
            context["opportunities"] = opportunities.model_dump(mode="json")
            with PrimeKGSQLiteProvider(args.graph) as graph:
                context["follow_up_results"] = search_round(
                    opportunities.searches, graph, PubMedClient(max_attempts=2), args.output, "follow-up")
        else:
            opportunities = Opportunities.model_validate(context["opportunities"])
        save(args.output / "materials.json", context)
        result = invoke(model, args.output, "questions", "pilot-question-composition", context, Questions)
        write_review(args.output, context, exploration, opportunities, result)
        print(f"Review: {args.output / 'review.md'}")
        return
    if args.snapshot is None or args.graph is None:
        parser.error("--snapshot and --graph are required unless --replay-run is used")
    source = json.loads(args.snapshot.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=False)
    with PrimeKGSQLiteProvider(args.graph) as graph:
        context = {"entity": source["entity"],
                   "experiment_goal": (RELATIONSHIP_EXPERIMENT if args.focus == "relationship"
                                       else "探索材料能支持的自然问题，不要求特定题型。"),
                   "overview": {**source, "record": compact_record(source["record"])},
                   "graph": graph_material(graph, source["entity"]),
                   "tools": {"primekg": "Expand a named entity's neighbors (bounded).",
                             "uniprot": "query is exactly one human gene symbol, e.g. BRAF. Returns overview annotations.",
                             "pubmed": "Search an English biomedical query; retrieve at most 3 abstracts.",
                             "pubmed_fetch": "Read up to 3 cited abstracts by comma-separated numeric PubMed IDs."}}
        save(args.output / "initial-materials.json", context)
        if args.prepare_only:
            print(f"Prepared graph and overview: {args.output}")
            return
        model = build_model_port(args.provider, config_path=args.config)
        client = PubMedClient(max_attempts=2)
        exploration = invoke(model, args.output, "exploration", "pilot-entity-exploration", context, Exploration)
        context["exploration_results"] = search_round(exploration.searches, graph, client, args.output, "exploration")
        opportunities = invoke(model, args.output, "opportunities", "pilot-question-opportunities", context, Opportunities)
        context["opportunities"] = opportunities.model_dump(mode="json")
        context["follow_up_results"] = search_round(opportunities.searches, graph, client, args.output, "follow-up")
        save(args.output / "materials.json", context)
        result = invoke(model, args.output, "questions", "pilot-question-composition", context, Questions)
        write_review(args.output, context, exploration, opportunities, result)
        print(f"Review: {args.output / 'review.md'}")


if __name__ == "__main__":
    main()
