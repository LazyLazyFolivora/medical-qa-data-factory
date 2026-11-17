"""Collect supervised search demonstrations for verified objective questions."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field

from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from ..llm.prompts import template_for
from ..llm.registry import build_model_port
from .evidence_provider import EvidenceProviderRequest, ProviderTerminalStatus
from .objective_provider_adapters import PubMedMaterialProvider
from .objective_question_contracts import ObjectiveValidationReport
from .task_release import ObjectiveQuestionReleaseGate


class SearchDecision(ContractModel):
    action: Literal["search", "read_full_text", "evidence_sufficient", "evidence_insufficient"]
    query: str | None = Field(default=None, max_length=500)
    material_id: str | None = None
    reason: str = Field(min_length=1, max_length=1000)


class DemonstrationReview(ContractModel):
    accepted: bool
    supporting_material_ids: list[str]
    reason: str = Field(min_length=1, max_length=1000)


def _model_result(model, role, operation_id, payload, result_type):
    request = StructuredGenerationRequest(
        role=role, system_prompt_version=template_for(role).version,
        invocation_id=operation_id, idempotency_key=operation_id, payload=payload,
    )
    return result_type.model_validate(model.generate_structured(request, result_type).parsed)


def collect_demonstration(question_id, question, model, provider, *, max_searches):
    steps = []
    evidence = {}
    for index in range(max_searches + 1):
        operation_id = f"demonstration:{question_id}:{index}"
        can_retrieve = index < max_searches
        decision = _model_result(model, "objective_training_demonstrator", operation_id, {
            "question": question,
            "previous_steps": [
                {"action": step["action"], "result": {
                    key: value for key, value in step["result"].items() if key != "new_evidence"
                }} for step in steps
            ],
            "evidence": list(evidence.values()),
            "retrieval_remaining": max_searches - index,
        }, SearchDecision)
        step = {"action": decision.model_dump(exclude_none=True),
                "visible_material_ids": list(evidence), "result": None}
        steps.append(step)
        if decision.action in {"evidence_sufficient", "evidence_insufficient"}:
            return steps, evidence
        if not can_retrieve:
            raise ValueError("Demonstrator did not stop within the retrieval budget")
        if decision.action == "search" and not (decision.query or "").strip():
            raise ValueError("A PubMed search requires a query")
        if decision.action == "read_full_text" and decision.material_id not in evidence:
            raise ValueError("Full-text retrieval must use a previously retrieved material ID")
        request = EvidenceProviderRequest(
            request_id=operation_id, provider_id=provider.capability.provider_id,
            provider_version=provider.capability.provider_version, operation_key=operation_id,
            query={"term": decision.query} if decision.action == "search" else {"material_id": decision.material_id},
            requested_at=datetime.now(timezone.utc), material_limit=3,
        )
        if decision.action == "search":
            result = provider.retrieve(request)
        else:
            result = provider.retrieve_details(request, material_ids=[decision.material_id])
        new_evidence = []
        if result.terminal_status == ProviderTerminalStatus.SUCCEEDED:
            for material in result.materials:
                mid = material["material_id"]
                original = result.raw_content_by_material_id.get(mid)
                if not original:
                    continue
                current = evidence.get(mid)
                if current and current["material_type"] == "literature_full_text" and decision.action == "search":
                    continue
                row = {"material_id": mid, "source": material["source_record_id"],
                       "material_type": material["material_type"], "content_hash": material["content_hash"],
                       "text": original[:12000], "truncated": len(original) > 12000}
                if row != current:
                    evidence[mid] = row
                    new_evidence.append(row)
        step["result"] = {
            "provider": request.provider_id, "provider_version": request.provider_version,
            "status": result.terminal_status.value, "error_category": result.error_category,
            "snapshot_id": result.snapshot_id, "material_ids": result.material_ids,
            "new_evidence": new_evidence,
        }
    raise ValueError("Demonstrator returned no terminal decision")


def _read_jsonl(path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def export_training_release(release, output, model, provider, *, question_ids=None, max_searches=6):
    if max_searches < 1:
        raise ValueError("--max-searches must be at least 1")
    if output.exists():
        raise FileExistsError(output)
    questions = _read_jsonl(release / "public" / "questions.jsonl")
    reports = {row["question_id"]: ObjectiveValidationReport.model_validate(row)
               for row in _read_jsonl(release / "audit" / "validation_reports.jsonl")}
    if question_ids and (missing := set(question_ids) - {row["question_id"] for row in questions}):
        raise ValueError(f"Unknown released question IDs: {sorted(missing)}")
    rows, skipped = [], {}
    for row in questions:
        question_id = row["question_id"]
        if question_ids and question_id not in question_ids:
            continue
        report = reports.get(question_id)
        if report is None or not report.release_allowed or not report.evidence_used:
            skipped[question_id] = "Missing passing answer report or evidence sources"
            continue
        ObjectiveQuestionReleaseGate.assert_releasable(report)
        steps, evidence = collect_demonstration(question_id, row["question"], model, provider,
                                                max_searches=max_searches)
        if not evidence or steps[-1]["action"]["action"] != "evidence_sufficient":
            errors = sorted({step["result"]["error_category"] for step in steps
                             if step["result"] and step["result"]["error_category"]})
            skipped[question_id] = {
                "reason": steps[-1]["action"]["reason"], "retrieved_materials": len(evidence),
                "provider_errors": errors,
            }
            continue
        review = _model_result(model, "objective_training_trace_review", f"demonstration-review:{question_id}", {
            "question": row["question"], "verified_answer": row["answer"], "steps": steps,
        }, DemonstrationReview)
        if not review.accepted or not review.supporting_material_ids or not set(review.supporting_material_ids) <= evidence.keys():
            skipped[question_id] = review.reason
            continue
        rows.append({
            "schema_version": "objective-training-sample-v1",
            "question_id": question_id, "question": row["question"], "answer": row["answer"],
            **({"suggested_retrieval": row["suggested_retrieval"]} if row.get("suggested_retrieval") else {}),
            "answer_sources": report.evidence_used, "answer_validation_report_id": report.report_id,
            "answer_validation_digest": report.input_digest,
            "trajectory": steps, "trajectory_review": review.model_dump(),
        })
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"exported": len(rows), "skipped": skipped}


def run_training_export_command(args):
    model = build_model_port(config_path=args.model_config)
    result = export_training_release(Path(args.release), Path(args.output), model, PubMedMaterialProvider(),
                                     question_ids=args.question_id, max_searches=args.max_searches)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["exported"] else 1
