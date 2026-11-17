"""Explicit research tools. Each result returns to the ReAct controller."""
import json

from ..llm.contracts import StructuredGenerationRequest
from .candidate_review import CandidateReviewBatch
from .objective_question_contracts import HypothesisState, ObjectiveValidationReport
from .react_main_agent import latest_candidate_review, publication_for, question_reader_accepts, validate_react_decision
from .react_observations import ObservationView
from .subagent_contracts import RoleContractViolation, SubagentRole


def execute_react_tool(loop, snapshot, decision):
    # Import here to avoid a cycle with the action dispatcher.
    from .agent_loop import _artifact, _success_result
    action, p = decision.action, decision.action_parameters
    validate_react_decision(snapshot, decision, list(loop.providers))
    if action == "research_evidence":
        from .research_agent import research_evidence
        return research_evidence(loop, snapshot, decision)
    if action == "discover_questions":
        from .question_discovery import discover_questions
        return discover_questions(loop, snapshot, decision)
    if action in {"read_source_details", "synthesize_evidence"}:
        from .research_tools import read_source_details, synthesize_evidence
        return (read_source_details if action == "read_source_details" else synthesize_evidence)(loop, snapshot, decision)
    hid = p.get("hypothesis_id")
    sequence = snapshot.commit_sequence
    if action == "publish_question":
        proposal = publication_for(snapshot, hid)
        report = ObjectiveValidationReport.model_validate(proposal.payload["report"])
        if not report.release_allowed or report.verdict != "PASS":
            raise RoleContractViolation("Publication report does not permit release")
        artifact = _artifact(report, "objective_validation_report", decision, SubagentRole.RELEASE_REVIEW)
        loop._commit_child(snapshot, decision, f"publish.{decision.decision_id}",
            _success_result(f"publish.{decision.decision_id}", [artifact.artifact_id], "published"),
            [artifact], hypothesis_transition=(hid, [HypothesisState.PUBLISHED]))
        return
    if action == "review_candidate":
        from .candidate_review_inputs import candidate_review_inputs
        inputs = candidate_review_inputs(snapshot, hid, loop.model)
        hypothesis, claims, originals = inputs["hypothesis"], inputs["claims"], inputs["originals"]
        source_context, review_input_hash = inputs["source_context"], inputs["review_input_hash"]
        previous = latest_candidate_review(snapshot, hid)
        if previous and previous.get("review_input_hash") == review_input_hash:
            reused = {**previous, "artifact_id": f"quality.{decision.decision_id}",
                      "sequence": sequence, "reused_from": previous["artifact_id"],
                      "reuse_reason": "Candidate, evidence and review instructions are unchanged. "
                          "This call did not re-evaluate or revise the question. Apply the existing feedback "
                          "through revise_candidate (hypothesis_id + revision_instruction), or supply changed evidence before re-review."}
            artifact = _artifact(reused, "candidate_usability_review", decision, SubagentRole.QUESTION_DESIGN)
            loop._commit_child(snapshot, decision, f"react.{decision.decision_id}",
                _success_result(f"react.{decision.decision_id}", [artifact.artifact_id], "unchanged_review_reused"), [artifact])
            return
        reader_note = None
        if not question_reader_accepts(snapshot, hid):
            from .question_reader import QUESTION_REVIEW_VERSION, review_questions
            reader, views, _ = review_questions(loop.model,
                operation_id=f"{snapshot.run_id}|quality|{decision.decision_id}|reader",
                goal=snapshot.goal.model_dump(mode="json"), materials=originals, navigate_materials=True,
                questions=[{"index": 0, "question": hypothesis["question_intent"]}])
            judgments = reader.judgments
            reader_note = {"question": hypothesis["question_intent"], "version": QUESTION_REVIEW_VERSION,
                           **judgments[0].model_dump(mode="json"), **views}
            if judgments[0].verdict != "ACCEPT":
                needs_evidence = judgments[0].verdict == "NEEDS_EVIDENCE"
                payload = {"artifact_id": f"quality.{decision.decision_id}", "hypothesis_id": hid,
                           "sequence": sequence, "review_input_hash": review_input_hash, "question_reader": reader_note,
                           "reviews": [{"hypothesis_id": hid,
                                        "disposition": "NEEDS_EVIDENCE" if needs_evidence else
                                            "REJECT" if judgments[0].verdict == "RESELECT" else "REWRITE",
                                        "rationale": judgments[0].reason, "revised_question": None,
                                        "revised_scope": None,
                                        "evidence_need": judgments[0].reason if needs_evidence else None}]}
                artifact = _artifact(payload, "candidate_usability_review", decision, SubagentRole.QUESTION_DESIGN)
                loop._commit_child(snapshot, decision, f"react.{decision.decision_id}",
                    _success_result(f"react.{decision.decision_id}", [artifact.artifact_id],
                        "question_needs_evidence" if needs_evidence else
                        "question_requires_reselection" if judgments[0].verdict == "RESELECT"
                        else "question_requires_revision"), [artifact])
                return
        # The question reader owns question acceptance by default;
        # answer verification still owns answer correctness and publication.
        if getattr(loop.model, "candidate_review_mode", "reader_only") == "reader_only":
            payload = {"artifact_id": f"quality.{decision.decision_id}", "hypothesis_id": hid,
                       "sequence": sequence, "review_input_hash": review_input_hash,
                       "review_origin": "host_reuses_accepted_question_reader",
                       "review_scope": "Question acceptance only; answer verification is still required.",
                       "reviews": [{"hypothesis_id": hid, "disposition": "KEEP",
                           "rationale": "题干已通过独立读者审核；复用该判断，答案正确性仍交verify_answer核验。",
                           "revised_question": None, "revised_scope": None, "evidence_need": None}]}
            if reader_note:
                payload["question_reader"] = reader_note
            artifact = _artifact(payload, "candidate_usability_review", decision, SubagentRole.QUESTION_DESIGN)
            loop._commit_child(snapshot, decision, f"react.{decision.decision_id}",
                _success_result(f"react.{decision.decision_id}", [artifact.artifact_id], "question_review_reused"), [artifact])
            return
        response = loop.model.generate_structured(StructuredGenerationRequest(
            role="objective_candidate_review", system_prompt_version="candidate-review-v1",
            invocation_id=f"quality.{decision.decision_id}", idempotency_key=f"{snapshot.run_id}|quality|{decision.decision_id}",
            payload={"stage": "review_only", "hypotheses": [hypothesis], "claims": claims,
                     "previous_review": previous,
                     # Keep original passages, not the writer's approval or private
                     # scope claims: those cannot repair the public question.
                     "research_context": source_context,
                     "evidence_excerpts": originals,
                     "instruction": "Return advice only. Do not rewrite automatically. A correct but pointless question must not receive KEEP. "
                     "Comparisons of domain entities can be valid information needs. Multiple related aspects can serve one comparison; "
                     "do not confuse that with extracting an unspecified trial's internal metrics. "
                     "Ground each factual objection in original excerpts and the actual public question, not risk_flags or presumed confusion. "
                     "Check actual population/comparator/endpoint definitions; do not replace eligibility criteria with an unsupported treatment-line label. "
                     "Judge the public question and answer using original evidence_excerpts and source_passages. "
                     "Suggest REWRITE for correctable wording/scope defects. "
                     "review_candidate does not apply a suggested revision: inspect the actual question_intent. "
                     "When previous_review is supplied, check whether its concrete objection is resolved. "
                     "If you overturn it, identify the original evidence or mistaken assumption that justifies the change; "
                     "repeating this call or a plan to revise is not a completed repair."}), CandidateReviewBatch)
        review = CandidateReviewBatch.model_validate(response.parsed)
        if len(review.reviews) != 1 or review.reviews[0].hypothesis_id != hid:
            raise RoleContractViolation("Review must cover the selected candidate exactly once")
        payload = {"artifact_id": f"quality.{decision.decision_id}", "hypothesis_id": hid,
                   "sequence": sequence, "review_input_hash": review_input_hash,
                   "reviews": [r.model_dump(mode="json") for r in review.reviews]}
        if reader_note:
            payload["question_reader"] = reader_note
        artifact = _artifact(payload, "candidate_usability_review", decision, SubagentRole.QUESTION_DESIGN)
    else:
        payload = {"artifact_id": f"observation.{decision.decision_id}", "sequence": sequence}
        if action == "tool_feedback":
            payload["error"] = p["error"]
            payload["attempted_tool_executed"] = False
            payload["remediation"] = p.get("remediation")
            payload["attempted_tool_call"] = p.get("attempted_tool_call")
        elif decision.input_artifact_ids:
            from .research_tools import material_passages
            sections = p.get("source_sections", {})
            views = []
            for aid in decision.input_artifact_ids:
                if snapshot.artifacts[aid].artifact_type != "evidence_material":
                    continue
                try:
                    views.append(material_passages(loop, snapshot, aid, sections.get(aid, p.get("paragraph_ids")),
                                                   index_offset=p.get("index_offset", 0), paginate=True,
                                                   max_chars=max(0, 16000 - sum(len(v["content"]) for v in views))))
                except (OSError, UnicodeError) as exc:
                    views.append({"material_id": aid, "content": "", "content_complete": False,
                                  "read_error": f"{type(exc).__name__}: {exc}"})
            payload.update(source_ids=decision.input_artifact_ids, source_views=views)
            payload["reading_page_instruction"] = (
                "Only returned content was read. For views with next_read, call read_artifacts again with "
                "input_artifact_ids=[next_read.material_id] and parameters.paragraph_ids=next_read.paragraph_ids; "
                "deferred paragraphs are not evidence yet.")
            offset, limit = p.get("offset", 0), min(p.get("limit", 6000), 12000)
            records = []
            view = ObservationView(snapshot)
            for aid in decision.input_artifact_ids:
                source = snapshot.artifacts[aid]
                if source.artifact_type == "evidence_material":
                    continue
                record = {"id": aid, "type": source.artifact_type, "content": view.content(source)}
                record["payload"] = source.payload
                records.append(record)
            raw = json.dumps(records, ensure_ascii=False)
            payload.update(source_ids=decision.input_artifact_ids, offset=offset,
                           content=raw[offset:offset + limit], total_chars=len(raw),
                           next_offset=offset + limit if offset + limit < len(raw) else None)
        else:
            view = ObservationView(snapshot)
            index = sorted((view.card(a) for a in snapshot.artifacts.values()), key=lambda a: a["id"])
            offset, limit = p.get("offset", 0), min(p.get("limit", 50), 100)
            payload.update(items=index[offset:offset + limit], total=len(index),
                           next_offset=offset + limit if offset + limit < len(index) else None)
        artifact = _artifact(payload, "react_observation", decision, SubagentRole.FACT_UNDERSTANDING)
    loop._commit_child(snapshot, decision, f"react.{decision.decision_id}",
        _success_result(f"react.{decision.decision_id}", [artifact.artifact_id], action), [artifact])
