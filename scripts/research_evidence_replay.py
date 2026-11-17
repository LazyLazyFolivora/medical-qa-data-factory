"""Replay synthesis/design from a saved research request's actually read passages.

Usage: python scripts/research_evidence_replay.py INPUT_JSON OUTPUT_DIRECTORY [REVIEW_JSON]
This controlled experiment excludes previous model interpretations and reviews.
"""
import json
import sys
from pathlib import Path

from medical_qa_data_factory.generation.research_first_pilot import (
    Understanding, DesignResult, Critique, DESIGN_INSTRUCTION,
)
from medical_qa_data_factory.llm.contracts import StructuredGenerationRequest
from medical_qa_data_factory.llm.registry import build_model_port
from medical_qa_data_factory.llm.run_trace import trace_run


def main():
    source_path, output = Path(sys.argv[1]), Path(sys.argv[2])
    feedback_path = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    feedback = json.loads(feedback_path.read_text(encoding="utf-8")) if feedback_path else None
    payload = json.loads(source_path.read_text(encoding="utf-8"))["payload"]
    sources = [{"source_id": s["source_id"], "passages": s["read_passages"]}
               for s in payload["sources"] if s.get("read_passages")]
    if not sources:
        raise ValueError("No actually read passages in the saved request")
    output.mkdir(parents=True, exist_ok=False)
    model = build_model_port()

    def save(name, value):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def ask(stage, instruction, body, schema):
        request = StructuredGenerationRequest(role="question_method_pilot", system_prompt_version="evidence-replay-v1",
            invocation_id=f"{output.name}.{stage}", idempotency_key=f"{output.resolve()}|{stage}",
            payload={"stage": stage, "instruction": instruction, **body})
        save(stage + "-input.json", request.model_dump(mode="json"))
        result = schema.model_validate(model.generate_structured(request, schema).parsed)
        save(stage + "-output.json", result.model_dump(mode="json"))
        return result

    with trace_run(output, output.name):
        focus = payload.get("research_focus", "")
        save("provenance.json", {"replayed_from": str(source_path.resolve()), "sources": sources,
                                 "human_review_file": str(feedback_path.resolve()) if feedback_path else None,
                                 "scope": "Controlled replay; no autonomous discovery claim"})
        understanding = ask("synthesis",
            "只根据这些实际读过的原文段落形成认识。逐个实验记录模型、对照、终点、结果和不能推出的结论，"
            "再回答research_focus。不要补写细胞机制、不要把未见显著差异写成完全无效。"
            "体外磷酸化、体内磷酸化、受体总量、肿瘤生长是不同终点，不能混同；一个模型测到的结果不搬到另一个模型。"
            "综合认识应解释对照之间的关系；保留尚不能解释的边界。用中文。",
            {"research_focus": focus, "sources": sources}, Understanding)
        design = ask("design", DESIGN_INSTRUCTION,
            {"research_focus": focus, "understanding": None if feedback and feedback.get("design_sources_only") else understanding.model_dump(mode="json"), "sources": sources,
             "revision_feedback": feedback,
             "revision_instruction": "如有revision_feedback，针对原候选逐条修正；反馈是需核对的修改要求，事实仍以原文为准。"}, DesignResult)
        critique = None
        if design.candidate:
            critique = ask("critique",
                "仅用原文复核题干预设与答案，每条关键判断都要对应实验模型、对照和终点。"
                "不要求论文解决未研究的机制；不把无显著差异变成零作用；不把总量降低等同于已证明的清除途径。"
                "判断题干是否自包含、是否围绕一个信息需求、是否需要比较条件。单篇全文能答不自动等于简单，"
                "但必须诚实指出摘要查找或综述复述是否足够。不要用模型总结替代原文。用中文。",
                {"candidate": design.candidate.model_dump(mode="json"), "sources": sources}, Critique)
        save("result.json", {"design": design.model_dump(mode="json"),
                             "critique": critique.model_dump(mode="json") if critique else None, "published": False})
        print(design.candidate.question if design.candidate else design.reason)


if __name__ == "__main__":
    main()
