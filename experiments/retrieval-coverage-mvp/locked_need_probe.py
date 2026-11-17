"""Isolated select-then-write probe. Run from the project root."""
import copy
import importlib.util
import json
import argparse
import os
import requests
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deepseek", action="store_true")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location(
        "saved_probe", "data/experiments/fixed-evidence-design-20261111/run_probe.py"
    )
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    harness.ROOT = Path("data/experiments/tp53-locked-need-20261112")
    provider = None
    if args.deepseek:
        from dotenv import load_dotenv
        load_dotenv(".env.deepseek", override=False)
        provider = json.loads(Path("config/models.json").read_text(encoding="utf8"))["providers"]["deepseek-official"]
        raw_key = Path(".env.deepseek").read_text(encoding="utf-8-sig").strip()
        if raw_key.startswith("sk-") and not any(c.isspace() for c in raw_key):
            os.environ.setdefault(provider["api_key_env"], raw_key)
        if not os.environ.get(provider["api_key_env"]):
            raise RuntimeError("DeepSeek credential unavailable")
        harness.ROOT = Path("data/experiments/tp53-locked-need-deepseek-20261112")
    payload = json.loads(Path(
        "data/experiments/entity-retrieval-tp53-20261112-network/research-8-input.json"
    ).read_text(encoding="utf8"))["payload"]
    passages = {}
    for source in payload["sources"]:
        for passage in source.get("read_passages", []):
            key = f"E{len(passages) + 1}"
            passages[key] = {"source_id": source["source_id"], **passage}

    def ask(name, instruction, data):
        body = {k: copy.deepcopy(v) for k, v in harness.BASE.items() if k != "messages"}
        body["messages"] = [{"role": "system", "content": instruction},
                            {"role": "user", "content": json.dumps(data, ensure_ascii=False)}]
        if provider:
            body = {"model": provider["model_id"], "messages": body["messages"],
                    "max_tokens": 4096, "response_format": {"type": "json_object"},
                    **provider.get("request_parameters", {})}
            folder = harness.ROOT / name
            folder.mkdir(parents=True, exist_ok=False)
            (folder / "request.json").write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf8")
            (folder / "context.json").write_text(json.dumps({"context_window_tokens": provider["context_window_tokens"]}), encoding="utf8")
            response = requests.post(provider["base_url"], json=body, headers={
                "Authorization": "Bearer " + os.environ[provider["api_key_env"]]}, timeout=180)
            (folder / "response.txt").write_text(response.text, encoding="utf8")
            response.raise_for_status()
            result = response.json()
            parsed = json.loads(result["choices"][0]["message"]["content"])
            (folder / "output.json").write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf8")
            print(name, result.get("usage"), flush=True)
            return parsed
        return harness.call(name, body)

    for trial in range(1, 4):
        need = ask(f"select-{trial}",
                   "从已读原文中选一个自然且完整的信息需求。只选一个求知目标；可以查一个事实，也可以比较同一终点。不要追加机制、治疗或监测来凑综合性。不要写题目。只输出JSON："
                   '{"information_need":"读者想知道什么","answer_scope":"回答到哪里为止",'
                   '"excluded_topics":[],"evidence_ids":["E1"]}。证据ID只能选输入中实际支持该需求的段落。',
                   {"passages": passages})
        ids = need["evidence_ids"]
        if not ids or any(key not in passages for key in ids):
            raise ValueError("Selection did not identify available evidence")
        ask(f"write-{trial}",
            "将选定的信息需求写成一个中文问句并给出简洁参考答案。需求已确定，不扩展求知目标。只用提供的原文，必要范围放入题干，不依赖隐藏材料。无证据则返回question:null及原因。不要补机制或临床建议。只输出JSON："
            '{"question":"...","answer":"...","evidence_ids":[],"reason":"..."}。',
            {"selected_need": need, "passages": {key: passages[key] for key in ids}})


if __name__ == "__main__":
    main()
