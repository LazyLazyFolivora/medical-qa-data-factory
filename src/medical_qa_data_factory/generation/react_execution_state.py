"""Host-owned execution facts and verification prerequisites, not research routing."""
from .objective_question_contracts import HypothesisState
from .question_reader import QUESTION_REVIEW_VERSION


ACTIVE_VERIFICATION_STATES = {
    HypothesisState.PROPOSED, HypothesisState.VERIFICATION_PLANNED, HypothesisState.VERIFYING,
}


def latest_candidate_review(snapshot, hypothesis_id):
    reviews = [a.payload for a in snapshot.artifacts.values()
               if a.artifact_type == "candidate_usability_review"
               and a.payload.get("hypothesis_id") == hypothesis_id]
    return max(reviews, key=lambda review: review.get("sequence", 0), default=None)


def question_reader_accepts(snapshot, hypothesis_id):
    """Question approval belongs to this wording, not another candidate version."""
    artifact = snapshot.artifacts.get(hypothesis_id)
    if artifact is None:
        return False
    question = artifact.payload["question_intent"]
    reviews = [a.payload for a in snapshot.artifacts.values()
               if a.artifact_type == "candidate_usability_review"
               and a.payload.get("hypothesis_id") == hypothesis_id
               and a.payload.get("question_reader")]
    if reviews:
        reader = max(reviews, key=lambda review: review.get("sequence", 0))["question_reader"]
        if reader.get("question") == question and reader.get("version") == QUESTION_REVIEW_VERSION:
            return reader.get("verdict") == "ACCEPT"
    context_artifact = snapshot.artifacts.get(f"candidate_context.{hypothesis_id}")
    context = context_artifact.payload.get("research_context", {}) if context_artifact else {}
    return bool(context and context.get("approved_question") == question
                and context.get("question_review") == "ACCEPT"
                and context.get("question_review_version") == QUESTION_REVIEW_VERSION)


def verification_readiness(snapshot, hid, evidence_mode=None):
    """Project the same review prerequisites enforced by the verification tool."""
    readiness = _verification_evidence_readiness(snapshot, hid, evidence_mode)
    reader_accepted = question_reader_accepts(snapshot, hid)
    quality = latest_candidate_review(snapshot, hid)
    review = quality["reviews"][0] if quality else None
    quality_blocked = bool(review and review["disposition"] != "KEEP")
    readiness["question_reader_accepted"] = reader_accepted
    readiness["candidate_review_disposition"] = review["disposition"] if review else None
    if not reader_accepted:
        readiness["missing"].append("question_reader_acceptance_missing")
    if quality_blocked:
        readiness["missing"].append("latest_candidate_review_not_keep")
    readiness["entry_ready"] = readiness["entry_ready"] and reader_accepted and not quality_blocked
    if snapshot.hypotheses.get(hid) in ACTIVE_VERIFICATION_STATES:
        if quality_blocked:
            readiness["repair"] = {
                "hypothesis_id": hid, "review_id": quality["artifact_id"],
                "disposition": review["disposition"], "feedback": review["rationale"],
                "instruction": "Resolve the latest candidate review by revising the candidate, obtaining the "
                    "specific missing evidence, or choosing another question. Repeating verify_answer cannot "
                    "resolve this review. Inspect the feedback before choosing an action.",
            }
        elif not reader_accepted:
            readiness["repair"] = {
                "tool": "review_candidate", "hypothesis_id": hid,
                "instruction": "This exact question has no current reader acceptance. Review the candidate "
                    "before answer verification; resolve any resulting wording or premise feedback. "
                    "Evidence prerequisites are listed separately in missing.",
            }
    return readiness


def _verification_evidence_readiness(snapshot, hid, evidence_mode=None):
    hypothesis = snapshot.artifacts[hid].payload if hid in snapshot.artifacts else {}
    mode = evidence_mode or ("existing_sources" if hypothesis.get("evidence_basis") == "source_passages" else "retrieved")
    source_ids = [aid for aid in hypothesis.get("discovery_anchor_ids", [])
                  if aid in snapshot.artifacts and snapshot.artifacts[aid].artifact_type == "evidence_anchor"]
    if mode == "existing_sources":
        active = snapshot.hypotheses.get(hid) in ACTIVE_VERIFICATION_STATES
        return {"hypothesis_id": hid, "entry_ready": active and bool(source_ids),
                "evidence_mode": mode, "source_anchor_ids": source_ids,
                "eligible_provider_result_ids": [],
                "meaning": "Existing original sources only; not independent corroboration or proof of support.",
                "missing": ([] if source_ids else ["persisted_original_source_anchors"]) + ([] if active else ["candidate_not_in_verifiable_state"]),
                "repair": None}
    results = [a for a in snapshot.artifacts.values() if a.artifact_type == "provider_result"
               and a.metadata.get("hypothesis_id") == hid
               and a.metadata.get("purpose") == "verification"]
    eligible = [a.artifact_id for a in results
                if a.payload.get("terminal_status") == "SUCCEEDED" and a.payload.get("anchor_ids")]
    active = snapshot.hypotheses.get(hid) in ACTIVE_VERIFICATION_STATES
    return {
        "hypothesis_id": hid,
        "entry_ready": active and bool(eligible),
        "meaning": "Entry prerequisites only; not a claim that evidence supports the answer or is independently sufficient.",
        "eligible_provider_result_ids": eligible,
        "retrieval_attempts": [{"id": a.artifact_id, "status": a.payload.get("terminal_status"),
                                "anchor_count": len(a.payload.get("anchor_ids", []))} for a in results],
        "missing": ([] if eligible else ["successful_candidate_verification_retrieval_with_anchors"])
                   + ([] if active else ["candidate_not_in_verifiable_state"]),
        "repair": {
            "tool": "research_evidence", "hypothesis_id": hid, "purpose": "verification",
            "query_basis": snapshot.artifacts[hid].payload.get("question_intent") if hid in snapshot.artifacts else None,
            "instruction": "Choose a suitable provider and query from the public question only, without expected answers. "
                "Inspect returned sources and specific gaps. A successful retrieval with anchors enables verify_answer; "
                "omit provider_result_ids to select candidate-linked results, or use eligible_provider_result_ids only. "
                "Discovery materials, claim IDs and anchor IDs are not candidate-linked provider_result IDs. "
                "Empty/failed retrieval requires investigating the actual error or another query/source; "
                "retrying verify_answer without new evidence cannot fix this prerequisite.",
        } if active and not eligible else None,
    }


def execution_state(snapshot, steps):
    """Project saved outputs, never the notebook's account of what happened."""
    if not steps:
        return {"origin": "host", "last_completed_step": None, "instruction": "No tool step has completed."}
    step = steps[-1]
    execution = dict(step.get("execution_record", {"status": "legacy_record_without_execution_status"}))
    if step["action"] == "tool_feedback":
        execution.setdefault("feedback_delivery_status", execution.get("status"))
        execution.update(status="NOT_EXECUTED", attempted_tool_executed=False,
                         error_category="TOOL_CALL_REJECTED")
    outputs = [snapshot.artifacts[aid] for aid in step.get("result_ids", []) if aid in snapshot.artifacts]
    feedback = next((a.payload for a in outputs if a.artifact_type == "react_observation"
                     and a.payload.get("error")), None)
    return {
        "origin": "host", "state_revision": snapshot.state_revision,
        "last_completed_step": {"action": step["action"], "sequence": step["sequence"],
            "execution": execution,
            "attempted_tool_executed": False if step["action"] == "tool_feedback" else None,
            "feedback": {k: feedback.get(k) for k in ("error", "remediation")} if feedback else None,
            "output_counts": {kind: sum(a.artifact_type == kind for a in outputs)
                              for kind in sorted({a.artifact_type for a in outputs})},
            "provider_results": [{"id": a.artifact_id, "status": a.payload.get("terminal_status"),
                "material_count": len(a.payload.get("material_ids", [])),
                "search_trace": a.payload.get("usage", {}).get("search_trace", []),
                "detail_trace": a.payload.get("usage", {}).get("detail_trace", [])}
                for a in outputs if a.artifact_type == "provider_result"]},
        "instruction": "These are persisted execution facts. Tool feedback means the attempted tool did NOT run. "
            "A completed wrapper is not proof of successful provider retrieval. Preserve exact error details; "
            "do not rename access denied to timeout or a missing prerequisite to a broken verifier. "
            "The next chosen action has not executed. Model notes are interpretations, not this execution record.",
    }
