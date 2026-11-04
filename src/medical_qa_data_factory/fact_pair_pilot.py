"""Select fact pairs for manual inspection before composing questions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from .domain.common import ContractModel
from .graph_question_pilot import EvidenceNeed, invoke
from .llm.registry import build_model_port
from .question_method_pilot import save


class FactPair(ContractModel):
    observation: EvidenceNeed | None
    added_fact: EvidenceNeed | None
    information_gain: str
    reader_need: str
    proposal: Literal["pair", "single", "insufficient"]


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
    payload = {"information_need": material["assignment"]["information_need"],
               "relation_source": material["relation_source"],
               "targeted_lookup": material["targeted_lookup"]}
    args.output.mkdir(parents=True, exist_ok=False)
    save(args.output / "input.json", payload)
    save(args.output / "origin.json", {"input": str(args.input.resolve()),
        "scope": "Fixed reviewed need, full saved evidence, no new retrieval; selection only.",
        "trials": args.trials})
    model = build_model_port(args.provider, config_path=args.config)
    review = ["# 事实组合：模型原始输出", "尚未写题；需要人工阅读事实与引用。"]
    for index in range(1, args.trials + 1):
        output = args.output / f"trial-{index}"
        output.mkdir(exist_ok=False)
        result = invoke(model, output, "fact-pair", "pilot-fact-pair", payload, FactPair)
        review.append(f"## Trial {index}: {result.proposal}")
        for label, fact in [("观察", result.observation), ("新增事实", result.added_fact)]:
            review.extend([f"### {label}", fact.fact if fact else "无"])
            if fact:
                review.extend([fact.source, "> " + fact.quote])
        review.extend(["### 信息增量", result.information_gain,
                       "### 读者需求", result.reader_need])
        (args.output / "review.md").write_text("\n\n".join(review), encoding="utf-8")


if __name__ == "__main__":
    main()
