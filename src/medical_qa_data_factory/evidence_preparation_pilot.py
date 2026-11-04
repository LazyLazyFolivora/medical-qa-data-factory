"""Repeat evidence preparation and writing for a fixed, reviewed opportunity."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .domain.common import ContractModel
from .focused_question_pilot import Delivery
from .graph_question_pilot import EvidenceNeed, invoke
from .llm.registry import build_model_port
from .question_method_pilot import save


class Observation(ContractModel):
    entity: str
    intervention: str
    effect: str
    conditions: str
    species: str


class EvidencePacket(ContractModel):
    observation: Observation
    relation_evidence: list[EvidenceNeed]
    property_evidence: list[EvidenceNeed]
    boundaries: list[str]
    gaps: list[str]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--provider")
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("--trials must be positive")
    material = json.loads(args.input.read_text(encoding="utf-8"))
    # Supply the knowledge need, not the reviewed answer/interpretation fields.
    assignment = material["assignment"]
    task = {key: assignment[key] for key in (
        "information_need", "lookup_property", "question_outline")}
    payload = {"task": task, "relation_source": material["relation_source"],
               "targeted_lookup": material["targeted_lookup"]}
    args.output.mkdir(parents=True, exist_ok=False)
    save(args.output / "origin.json", {
        "input": str(args.input.resolve()), "trials": args.trials,
        "scope": "Fixed human-reviewed need; model prepares evidence and writes. No new retrieval.",
    })
    save(args.output / "preparation-materials.json", payload)
    model = build_model_port(args.provider, config_path=args.config)
    review = ["# 自动证据整理与写作", "固定机会实验；以下是模型原始输出，不代表独立验证通过。"]
    for index in range(1, args.trials + 1):
        output = args.output / f"trial-{index}"
        output.mkdir(exist_ok=False)
        packet = invoke(model, output, "preparation", "pilot-evidence-preparation",
                        payload, EvidencePacket)
        writing = {"assignment": task, "evidence_packet": packet.model_dump(mode="json")}
        save(output / "writing-materials.json", writing)
        delivery = invoke(model, output, "delivery", "pilot-focused-composition",
                          writing, Delivery)
        review.extend([f"## Trial {index}", "### 模型整理的观察",
                       json.dumps(packet.observation.model_dump(), ensure_ascii=False),
                       "### 边界与缺口", json.dumps({"boundaries": packet.boundaries,
                           "gaps": packet.gaps}, ensure_ascii=False)])
        if delivery.question:
            q = delivery.question
            review.extend(["### 题目", q.question, "### 答案", q.answer,
                           "### 难度与短解自述", q.difficulty, q.shortcut])
        review.extend(["### 写作缺口", delivery.gap])
        (args.output / "review.md").write_text("\n\n".join(review), encoding="utf-8")
        print(f"Completed trial {index}", flush=True)


if __name__ == "__main__":
    main()
