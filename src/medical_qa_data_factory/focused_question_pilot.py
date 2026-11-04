"""One opportunity, one targeted lookup, one independently readable question."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .domain.common import ContractModel
from .graph_question_pilot import EvidenceNeed, Question, compact_record, invoke
from .llm.registry import build_model_port
from .question_method_pilot import retrieve, save


class Assignment(ContractModel):
    information_need: str
    relation: EvidenceNeed
    source_pmid: str
    lookup_entity: str
    lookup_property: str
    connection_value: str
    question_outline: str


class Selection(ContractModel):
    assignment: Assignment | None
    reason: str


class Delivery(ContractModel):
    question: Question | None
    gap: str


def literature_cards(materials: dict) -> list[dict]:
    """Reuse original retrieved abstracts, without previous model interpretations."""
    records = {}
    for result in materials.get("exploration_results", []):
        fetched = result.get("material", {}).get("fetch") or {}
        for record in fetched.get("records", []):
            if record.get("abstract"):
                records[record["pmid"]] = record
    return list(records.values())


def writing_payload(assignment: Assignment, cards: list[dict], lookup: dict) -> dict:
    # Membership is a source routing check, not a semantic quality validator.
    source = next(card for card in cards if card["pmid"] == assignment.source_pmid)
    return {"assignment": assignment.model_dump(mode="json"),
            "relation_source": source, "targeted_lookup": lookup}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--provider")
    args = parser.parse_args()
    materials = json.loads((args.source_run / "materials.json").read_text(encoding="utf-8"))
    cards = literature_cards(materials)
    args.output.mkdir(parents=True, exist_ok=False)
    save(args.output / "origin.json", {"source_run": str(args.source_run.resolve()),
                                      "method": "automatic single-opportunity selection"})
    model = build_model_port(args.provider, config_path=args.config)
    selection = invoke(model, args.output, "selection", "pilot-focused-opportunity",
                       {"seed_entity": materials["entity"], "articles": cards}, Selection)
    if selection.assignment is None:
        (args.output / "review.md").write_text(selection.reason, encoding="utf-8")
        return
    assignment = selection.assignment
    if assignment.source_pmid not in {card["pmid"] for card in cards}:
        raise ValueError("Selected source PMID is not in supplied literature")
    print(f"Retrieving UniProt: {assignment.lookup_entity}", flush=True)
    lookup = retrieve(assignment.lookup_entity)
    save(args.output / "lookup.raw.json", lookup)
    lookup = {**lookup, "record": compact_record(lookup["record"])}
    payload = writing_payload(assignment, cards, lookup)
    save(args.output / "writing-materials.json", payload)
    delivery = invoke(model, args.output, "delivery", "pilot-focused-composition", payload, Delivery)
    lines = ["# 单机会出题实验", "## 选定的信息需求", assignment.information_need,
             "## 为什么连接", assignment.connection_value, "## 原关系",
             assignment.relation.fact, "## 补查", f"{assignment.lookup_entity}: {assignment.lookup_property}"]
    if delivery.question:
        question = delivery.question
        lines.extend(["## 模型原始题目", question.question, "## 答案", question.answer,
                      "## 事实依赖", question.dependency, "## 难度与短解",
                      question.difficulty, question.shortcut, "## 依据"])
        lines.extend(f"- {e.fact}\n  {e.source}\n  > {e.quote}" for e in question.evidence_needs)
    if delivery.gap:
        lines.extend(["## 缺口", delivery.gap])
    (args.output / "review.md").write_text("\n\n".join(lines), encoding="utf-8")
    print(f"Review: {args.output / 'review.md'}", flush=True)


if __name__ == "__main__":
    main()
