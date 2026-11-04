"""Small, replayable entity -> opportunities -> questions experiment.

Run from the project root with PYTHONPATH=src. Preparation fetches one human
UniProt record; --generate additionally uses the configured model twice.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from pydantic import Field

from .domain.common import ContractModel
from .llm.contracts import StructuredGenerationRequest
from .llm.registry import build_model_port


METHOD = """从资料出发，为 query-planner 寻找自然、有意义的知识查询。
先理解资料讲了什么，再问一个希望了解该实体的人会想查清楚什么。
可以问身份分类、属性分布、功能、关系、条件变化、比较或过程解释。
按资料选择合适方向，不要求覆盖所有类型，不优先追求复杂问题。

第一阶段 opportunities：提出最多五个不同的信息需求。每个说明用户想
知道什么、为什么值得问、拟采用的提问操作、原文依据和还缺什么。
提问操作可选直接查询、关系连接、条件限定、比较区分、解释过程、线索识别。
直接属性可以是好题；线索识别必须有区分力，不能把介绍里的主语挖掉
就当作唯一答案。原文提到关联不意味着能够解释机制。
没有支持某方向的材料时写出具体补查需求，允许没有可出题的方向。

第二阶段 questions：从已有机会中选最多三项资料足以回答的需求。
先组织答案：明确应回答什么、回答到什么范围，再写自然的中文题干。
每题返回信息需求、提问操作、问题、答案、原文引文及来源链接、难度理由。
题干写明影响答案的物种、对象和条件，保留需要检索的答案信息。
例如表达材料应先区分主要表达组织、组织间差异和条件下变化，再选择
明确问法，避免泛问“具有何种组织特异性”。比较要指定维度；解释要限定
要解释的现象或环节。没有条件变化证据就不设计变化题。
直接查到事实可评为简单；必要的连接比较可评为中等；多步整合才可能困难。
允许全部为简单题。问题简洁自然，不固定加“查询”前缀、不编造病例背景。
引用仅使用所提供资料；答案不超出引文，未知写入 follow_up 而不补造事实。
"""


class Opportunity(ContractModel):
    need: str
    value: str
    operation: str
    evidence_quote: str
    missing_information: str


class Opportunities(ContractModel):
    opportunities: list[Opportunity] = Field(default_factory=list)


class Question(ContractModel):
    need: str
    operation: str
    question: str
    answer: str
    evidence_quote: str
    source_url: str
    difficulty_reason: str


class Questions(ContractModel):
    questions: list[Question] = Field(default_factory=list)
    follow_up: list[str] = Field(default_factory=list)


def retrieve(entity: str) -> dict:
    """Fetch a single reviewed human gene record, retaining source text."""
    if not entity.isalnum():
        raise ValueError("This pilot accepts an alphanumeric gene symbol")
    url = "https://rest.uniprot.org/uniprotkb/search?" + urlencode({
        "query": f"gene_exact:{entity} AND organism_id:9606 AND reviewed:true",
        "format": "json", "size": 2,
    })
    with urlopen(url, timeout=40) as response:
        data = json.load(response)
    records = data.get("results", [])
    if len(records) != 1:
        raise ValueError(f"Expected one reviewed human record; found {len(records)}")
    record = records[0]
    return {
        "entity": entity,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "request_url": url,
        "source_url": "https://www.uniprot.org/uniprotkb/" + record["primaryAccession"],
        "record": record,
    }


def make_request(snapshot: dict, stage: str, opportunities: dict | None = None):
    record = snapshot["record"]
    payload = {
        "stage": stage,
        "entity": snapshot["entity"],
        "source_url": snapshot["source_url"],
        "material": {
            key: record.get(key)
            for key in ("primaryAccession", "proteinDescription", "genes",
                        "organism", "comments", "keywords", "references")
        },
    }
    if opportunities is not None:
        payload["opportunities"] = opportunities
    version = hashlib.sha256(METHOD.encode()).hexdigest()
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return StructuredGenerationRequest(
        invocation_id=f"method-pilot.{stage}.{digest[:16]}",
        role="question_method_pilot",
        system_prompt_version="question-method-pilot-v1",
        skill_id="question-method-pilot",
        skill_version=version,
        skill_instruction=METHOD,
        payload=payload,
        idempotency_key=f"method-pilot|{stage}|{version}|{digest}",
    )


def save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity", default="PNMA1")
    parser.add_argument("--snapshot", type=Path, help="Replay a saved source.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--provider")
    args = parser.parse_args()
    # A new directory keeps previous experiments available for comparison.
    args.output.mkdir(parents=True, exist_ok=False)
    snapshot = (json.loads(args.snapshot.read_text(encoding="utf-8"))
                if args.snapshot else retrieve(args.entity))
    save(args.output / "source.json", snapshot)
    request = make_request(snapshot, "opportunities")
    save(args.output / "opportunities.request.json", request.model_dump(mode="json"))
    if not args.generate:
        print(f"Prepared source and prompt in {args.output}; use --snapshot and --generate to run")
        return
    model = build_model_port(args.provider, config_path=args.config)
    first = model.generate_structured(request, Opportunities)
    opportunities = Opportunities.model_validate(first.parsed)
    save(args.output / "opportunities.result.json", first.model_dump(mode="json"))
    request = make_request(snapshot, "questions", opportunities.model_dump())
    save(args.output / "questions.request.json", request.model_dump(mode="json"))
    second = model.generate_structured(request, Questions)
    questions = Questions.model_validate(second.parsed)
    save(args.output / "questions.result.json", second.model_dump(mode="json"))
    lines = [f"# {snapshot['entity']} 出题方法实验", "",
             "实验草稿，尚未经独立事实验证。", "", f"资料：{snapshot['source_url']}"]
    for index, item in enumerate(opportunities.opportunities, 1):
        lines += ["", f"## 信息需求 {index}：{item.need}",
                  f"意义：{item.value}", f"操作：{item.operation}",
                  f"依据：{item.evidence_quote}", f"缺口：{item.missing_information}"]
    for index, item in enumerate(questions.questions, 1):
        lines += ["", f"## 题目 {index}", item.question, "",
                  f"信息需求：{item.need}", f"操作：{item.operation}",
                  f"答案：{item.answer}", f"依据：{item.evidence_quote}",
                  f"来源：{item.source_url}", f"难度：{item.difficulty_reason}"]
    lines += ["", "## 后续补查", *questions.follow_up]
    (args.output / "review.md").write_text("\n\n".join(lines), encoding="utf-8")
    print(f"Generated {len(questions.questions)} draft questions: {args.output / 'review.md'}")


if __name__ == "__main__":
    main()
