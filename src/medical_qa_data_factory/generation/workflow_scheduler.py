"""Hierarchical scheduling: exploration batches and independent candidate queues.

Control decisions are derived from committed artifacts so pause/resume does not
reset local retry budgets. LLM calls choose search directions, never stage order.
"""
from typing import Any, Literal
import re
from pydantic import Field
from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from .objective_question_contracts import MainAgentDecision, HypothesisState, canonical_digest
from .question_strategies import question_strategy
from .exploration_memory import prepare_exploration_history
from .main_agent import MainAgentAction, _ROLE_BY_ACTION, _unprocessed_fact_material_ids

VERSION = "hierarchical-v1"
FINAL_QUESTION_WORKFLOW = "objective-hierarchical-loop-v2"
MAX_EVIDENCE_ROUNDS = 2
MAX_STAGE_FAILURES = 2
DIFFICULTY_FACTORS = {"easy": 1, "medium": 2, "hard": 4}


def exploration_budget(goal):
    factor = max((DIFFICULTY_FACTORS[level] for level in goal.difficulty_targets), default=1)
    return goal.question_count_upper_bound * 3 * factor


class SearchChoice(ContractModel):
    action: Literal["retrieve_materials", "stop"]
    rationale: str = Field(min_length=1)
    provider_id: str | None = None
    query: dict[str, Any] = Field(default_factory=dict)
    evidence_need: str = ""


class VerificationSearchChoice(SearchChoice):
    # `stop` is exploration-only. Ending a search is not an answer verdict.
    action: Literal["retrieve_materials", "finish_search", "search_exhausted"]


def candidate_outcomes(snapshot):
    """Current state plus downstream outcomes, not historical KEEP decisions."""
    outcomes = []
    for hid, state in snapshot.hypotheses.items():
        artifacts = [a.payload for a in snapshot.artifacts.values()
                     if a.payload.get("hypothesis_id") == hid]
        hypothesis = next((a for a in artifacts if a.get("question_intent")), {})
        resolution = next((a for a in artifacts if a.get("resolution_id")), {})
        closure = next((a for a in artifacts if a.get("artifact_id", "").startswith("candidate_closed.")), {})
        verification_reason = next((slot.result.stop_reason for slot in snapshot.child_slots.values()
                                    if slot.result and resolution.get("resolution_id") in slot.artifact_ids), None)
        contract_ids = {a.payload["contract_id"] for a in snapshot.artifacts.values()
                        if a.artifact_type == "frozen_question_contract"
                        and a.payload.get("resolution_id") == resolution.get("resolution_id")}
        draft_ids = {a.payload["draft_id"] for a in snapshot.artifacts.values()
                     if a.artifact_type == "objective_question_draft" and a.payload.get("contract_id") in contract_ids}
        release_reviews = [{"question_id": a.payload["question_id"], "verdict": a.payload["verdict"],
                            "reason": a.payload["primary_reason"], "assessed_difficulty": a.payload.get("assessed_difficulty")} for a in snapshot.artifacts.values()
                           if a.artifact_type == "objective_validation_report" and a.payload.get("question_id") in draft_ids]
        outcomes.append({"hypothesis_id": hid, "state": state.value,
                         "question": hypothesis.get("question_intent"),
                         "answer_status": resolution.get("status"),
                         "verification_reason": verification_reason,
                         "verified_answer_values": resolution.get("verified_answer_values", []),
                         "closure_reason": closure.get("reason"),
                         "release_reviews": release_reviews,
                         "verification_gaps": [a for a in artifacts if "evidence_need" in a and "used_anchor_ids" in a]})
    return outcomes


def target_question_count(snapshot):
    if not snapshot.goal or set(snapshot.goal.difficulty_targets) not in ({"medium"}, {"hard"}):
        return sum(s is HypothesisState.PUBLISHED for s in snapshot.hypotheses.values())
    return sum(row["state"] == HypothesisState.PUBLISHED.value and any(
        review["verdict"] == "PASS" and review["assessed_difficulty"] in snapshot.goal.difficulty_targets
        for review in row["release_reviews"]) for row in candidate_outcomes(snapshot))


def is_hierarchical(decision):
    return decision.action_parameters.get("workflow_version") in {VERSION, "objective-react-v1"}


def candidate_decisions(snapshot, hypothesis_id):
    return [d for d in snapshot.decisions.values()
            if is_hierarchical(d) and d.action_parameters.get("hypothesis_id") == hypothesis_id]


def workflow_phase(snapshot):
    if any(s not in {HypothesisState.REJECTED, HypothesisState.INDETERMINATE, HypothesisState.PUBLISHED}
           for s in snapshot.hypotheses.values()):
        return "candidate_processing"
    return "exploration"


class HierarchicalMainAgent:
    def __init__(self, model, *, provider_ids, memory_root=None):
        self.memory_root = memory_root
        self.model = model
        self.provider_ids = list(provider_ids)

    def _decision(self, snapshot, action, *, phase, hypothesis_id=None, inputs=(), reason=None, **params):
        digest = canonical_digest(snapshot.model_dump(mode="json"))
        return MainAgentDecision(
            decision_id=f"decision.{digest[:20]}", action=action.value,
            target_subagent_role=_ROLE_BY_ACTION.get(action), input_artifact_ids=list(inputs),
            state_digest=digest, requested_budget={}, expected_information_gain=0.5,
            action_parameters={"workflow_version": VERSION, "phase": phase,
                               **({"hypothesis_id": hypothesis_id} if hypothesis_id else {}), **params},
            stop_reason=reason if action is MainAgentAction.STOP else None,
        )

    def _search(self, snapshot, *, hypothesis=None, gap=None):
        local = hypothesis is not None
        hid = hypothesis.get("hypothesis_id") if local else None
        history = [d for d in snapshot.decisions.values() if is_hierarchical(d)
                   and d.action in {"retrieve_materials", "request_more_evidence"}
                   and (d.action_parameters.get("hypothesis_id") == hid)]
        failed = [a.payload for a in snapshot.artifacts.values() if a.artifact_type == "subagent_failure"
                  and a.parent_decision_id in {d.decision_id for d in history}]
        outcomes = candidate_outcomes(snapshot)
        request = StructuredGenerationRequest(
            invocation_id=f"workflow.{snapshot.run_id}.{snapshot.state_revision}",
            idempotency_key=f"{snapshot.run_id}|workflow|{snapshot.state_revision}",
            role="objective_workflow_planner", system_prompt_version=VERSION,
            payload={"stage": "verification_gap" if local else "exploration",
                     "goal": snapshot.goal.model_dump(mode="json"),
                     "generation_strategy": question_strategy(snapshot.goal).payload(),
                     "hypothesis": hypothesis, "evidence_gap": gap,
                     "available_providers": self.provider_ids,
                     "prior_searches": [d.action_parameters for d in history],
                     "search_results": [{"metadata": a.metadata, "status": a.payload.get("terminal_status"),
                         "material_count": len(a.payload.get("material_ids", []))}
                         for a in snapshot.artifacts.values() if a.artifact_type == "provider_result"
                         and a.parent_decision_id in {d.decision_id for d in history}],
                     "failures": failed[-3:],
                     "facts": [a.payload for a in snapshot.artifacts.values() if a.artifact_type == "candidate_claim"],
                     "design_feedback": [a.payload for a in snapshot.artifacts.values()
                                         if a.artifact_type in {"question_design_notes", "question_design_batch"}][-3:],
                     "candidate_outcomes": outcomes,
                     "counted_toward_target": target_question_count(snapshot),
                     "published_question_count": sum(s is HypothesisState.PUBLISHED for s in snapshot.hypotheses.values()),
                     "candidate_reviews": [] if local else [a.payload.get("reviews", []) for a in snapshot.artifacts.values()
                                           if a.artifact_type == "candidate_usability_review"],
                     "decision_boundary": (
                         "Only plan evidence collection. KEEP is a design review, not answer verification. "
                         "finish_search hands available evidence to verification; search_exhausted means no further search is feasible. "
                         "Neither action asserts that the answer is supported."
                         if local else "Use candidate_outcomes as current truth. Historical KEEP does not mean verified or published. "
                         "Below the counted output target, switch to an unexplored relation, source or literature lead. "
                         "Finishing all current candidates is not a reason to stop. Only suggest stopping after checking "
                         "the facts and prior discovery searches for remaining useful directions."),
                     "remaining_search_rounds": (MAX_EVIDENCE_ROUNDS if local else exploration_budget(snapshot.goal)) - len(history)},
        )
        if local:
            # Build a separate searcher's view. Verification notes may quote both
            # the expected answer and a previous blind solver's guesses.
            wording = hypothesis["question_intent"]
            note = str((gap or {}).get("evidence_need", "")).lower()
            need = "Find a source directly linking the entity to the property asked in the public question."
            if any(word in note for word in ("complete list", "all members", "完整", "所有成员")):
                need = "Find a source establishing the complete set requested by the public question."
            elif any(word in note for word in ("alternative", "unique", "唯一", "竞争")):
                need = "Check independent sources for competing answers within the public scope."
            for words, condition in (
                (("protein level", "蛋白水平"), "Use a protein-level measurement."),
                (("tissue", "组织"), "Check the tissue specified in the question."),
                (("species", "物种"), "Verify the species in the public scope."),
                (("assay", "测定"), "Check the assay and conditions used."),
                (("transcript", "剪接"), "Inspect the transcript and isoform annotation."),
            ):
                if any(word in note for word in words):
                    need += " " + condition
            missing = (gap or {}).get("missing_evidence_kind")
            if missing:
                need += " " + {
                    "identity": "Resolve the named entity by stable ID and documented synonyms.",
                    "direct_relation": "Find a source stating the exact relation asked in the question.",
                    "study_population": "Match the population, model and intervention in the question.",
                    "measurement": "Find the specified endpoint, unit and comparison group.",
                    "complete_set": "Find an authoritative complete enumeration.",
                    "alternatives": "Check plausible alternatives within the public scope.",
                    "source_field": "Locate the authoritative field at the requested precision.",
                }.get(missing, "")
            blind_payload = {"stage": "verification_gap", "question": wording,
                "target_entity": snapshot.goal.target_entity, "target_entity_id": snapshot.goal.target_entity_id,
                "available_providers": self.provider_ids,
                "prior_searches": [{"provider_id": d.action_parameters.get("provider_id")} for d in history],
                "evidence_gap": {"kind": (gap or {}).get("kind", "verification_gap"),
                                 "reason": "independent_evidence_required", "evidence_need": need},
                "remaining_search_rounds": (1 if (gap or {}).get("kind") == "release_evidence_gap"
                                            else MAX_EVIDENCE_ROUNDS - len(history)),
                "instruction": "Search from the public question only. Never guess an answer for use as a search term. "
                               "Choose the source that directly records the requested relation; use an entity-only lookup for structured databases."}
            request = request.model_copy(update={"payload": blind_payload})
        elif self.memory_root is not None:
            request = prepare_exploration_history(self.model, request, SearchChoice, self.memory_root, snapshot.run_id)
        schema = VerificationSearchChoice if local else SearchChoice
        choice = schema.model_validate(self.model.generate_structured(request, schema).parsed)
        if local:
            # Gene coordinates and nomenclature have authoritative indexed fields.
            # Prefer a database outside the discovery source and earlier attempts.
            question = hypothesis["question_intent"].casefold()
            order = ()
            if snapshot.manifest.orchestrator_version == FINAL_QUESTION_WORKFLOW and any(t in question for t in ("细胞遗传", "区带", "cytogenetic", "cytoband")):
                order = ("ncbi_gene", "hgnc", "ensembl")
            elif any(t in question for t in ("染色体", "chromosom", "genomic location", "细胞遗传")):
                order = ("ensembl", "ncbi_gene", "hgnc")
            elif any(t in question for t in ("别名", "正式名称", "正式全称", "gene symbol", "alias", "基因群组")):
                order = ("hgnc", "ncbi_gene", "ensembl")
            used_sources = {d.action_parameters.get("provider_id") for d in history}
            for aid in hypothesis.get("discovery_anchor_ids", []):
                anchor = snapshot.artifacts.get(aid)
                material = snapshot.artifacts.get(anchor.payload.get("material_id")) if anchor else None
                if material:
                    used_sources.add(material.payload.get("provider_id"))
            preferred = next((p for p in order if p in self.provider_ids and p not in used_sources), None)
            if preferred or (choice.action == "retrieve_materials" and choice.provider_id in {"hgnc", "ensembl", "ncbi_gene", "primekg"}):
                source = preferred or choice.provider_id
                entity = snapshot.goal.target_entity
                query = {"mention": entity, "direction": "both"} if source == "primekg" else {"symbol": entity}
                native_id = (snapshot.goal.target_entity_id or "").removeprefix("gene:")
                if snapshot.manifest.orchestrator_version == FINAL_QUESTION_WORKFLOW and source == "ncbi_gene" and native_id.isdigit():
                    query["native_id"] = native_id
                choice = VerificationSearchChoice(action="retrieve_materials", provider_id=source, query=query,
                    evidence_need=need + " " + question, rationale="Look up the named entity and inspect the relevant record.")
            elif choice.action == "retrieve_materials" and choice.provider_id == "pubmed":
                term = str(choice.query.get("term", ""))
                answers = list(hypothesis.get("proposed_answer_values", []))
                for artifact in snapshot.artifacts.values():
                    if artifact.artifact_type == "answer_resolution" and artifact.payload.get("hypothesis_id") == hid:
                        answers.extend(artifact.payload.get("verified_answer_values", []))
                for value in answers:
                    value = str(value).strip()
                    if value and value.casefold() not in question and re.search(
                            rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", term, re.I):
                        term = snapshot.goal.target_entity
                        break
                choice = choice.model_copy(update={"query": {"term": term or snapshot.goal.target_entity},
                    "evidence_need": need + " " + wording, "rationale": "Search the public question for independent evidence."})
        attempted = {(d.action_parameters.get("provider_id"), canonical_digest(d.action_parameters.get("query", {})))
                     for d in history}
        if not local and choice.action == "retrieve_materials" and (choice.provider_id, canonical_digest(choice.query)) in attempted:
            alternative = self._untried_direction(snapshot, history)
            if alternative:
                return alternative
            for retry in range(1, 3):
                redirected = request.model_copy(update={
                    "invocation_id": f"{request.invocation_id}.redirect{retry}",
                    "idempotency_key": f"{request.idempotency_key}|redirect{retry}",
                    "payload": {**request.payload, "rejected_query": choice.query,
                                "redirect_instruction": "This query was already executed. Choose a different factual question or evidence source for the entity. Use aliases, concrete findings and unresolved directions in the supplied facts. Stop only if no useful distinct direction remains."},
                })
                choice = schema.model_validate(self.model.generate_structured(redirected, schema).parsed)
                if choice.action == "stop" or (choice.provider_id, canonical_digest(choice.query)) not in attempted:
                    break
            else:
                alternative = self._untried_direction(snapshot, history, additional_topics=True)
                if alternative:
                    return alternative
                return self._decision(snapshot, MainAgentAction.STOP, phase="exploration",
                                      reason="exploration_alternatives_exhausted")
        if local and choice.action in {"finish_search", "search_exhausted"}:
            # Finish any unverified evidence before interpreting search exhaustion.
            results = [a for a in snapshot.artifacts.values() if a.artifact_type == "provider_result"
                       and a.metadata.get("hypothesis_id") == hid
                       and a.payload.get("terminal_status") == "SUCCEEDED" and a.payload.get("anchor_ids")]
            used = {aid for d in candidate_decisions(snapshot, hid) if d.action == "verify_answer"
                    for aid in d.action_parameters.get("provider_result_ids", [])}
            if any(a.artifact_id not in used for a in results):
                return self._decision(snapshot, MainAgentAction.VERIFY_ANSWER, phase="verification", hypothesis_id=hid,
                    inputs=[hid, *[a.artifact_id for a in results]], provider_result_ids=[a.artifact_id for a in results],
                    verification_round=1 + sum(d.action == "verify_answer" for d in candidate_decisions(snapshot, hid)))
            return self._decision(snapshot, MainAgentAction.CLOSE_CANDIDATE, phase="verification", hypothesis_id=hid,
                closure_reason="unresolved_verification_gap" if results else "no_independent_verification_evidence",
                search_rationale=choice.rationale)
        if choice.action == "stop":
            if snapshot.manifest.orchestrator_version == FINAL_QUESTION_WORKFLOW:
                alternative = self._untried_direction(snapshot, history, additional_topics=True)
                if alternative:
                    return alternative
                return self._decision(snapshot, MainAgentAction.STOP, phase="exploration",
                                      reason="exploration_alternatives_exhausted")
            return self._decision(snapshot, MainAgentAction.STOP, phase="exploration", reason=choice.rationale)
        if choice.provider_id not in self.provider_ids or not choice.query or not choice.evidence_need.strip():
            from .subagent_contracts import RoleContractViolation
            raise RoleContractViolation("search planning requires an available provider, query and concrete evidence need")
        signature = (choice.provider_id, canonical_digest(choice.query))
        if local and signature in attempted:
            return self._decision(snapshot, MainAgentAction.CLOSE_CANDIDATE, phase="verification",
                                  hypothesis_id=hid, closure_reason="search_direction_repeated")
        return self._decision(snapshot, MainAgentAction.REQUEST_MORE_EVIDENCE if local else MainAgentAction.RETRIEVE_MATERIALS,
            phase="verification" if local else "exploration", hypothesis_id=hid,
            provider_id=choice.provider_id, query=choice.query, evidence_need=choice.evidence_need,
            purpose="verification" if local else "discovery", verification_focus="comprehensive",
            max_search_rounds=1 if local else 2, max_queries_per_round=1 if local else 2, rationale=choice.rationale)

    def decide(self, snapshot):
        artifacts = list(snapshot.artifacts.values())
        resolutions = {a.payload["resolution_id"]: a.payload for a in artifacts if a.artifact_type == "answer_resolution"}
        contracts = {a.payload["contract_id"]: a.payload for a in artifacts if a.artifact_type == "frozen_question_contract"}
        drafts = [a.payload for a in artifacts if a.artifact_type == "objective_question_draft"]
        reports = {a.payload["question_id"]: a.payload for a in artifacts if a.artifact_type == "objective_validation_report"}
        active = {hid: state for hid, state in snapshot.hypotheses.items()
                  if state not in {HypothesisState.REJECTED, HypothesisState.INDETERMINATE, HypothesisState.PUBLISHED}}
        if target_question_count(snapshot) >= snapshot.goal.question_count_upper_bound:
            active = {hid: state for hid, state in active.items()
                      if set(snapshot.goal.difficulty_targets) in ({"medium"}, {"hard"}) and hid in snapshot.artifacts
                      and snapshot.artifacts[hid].payload.get("proposed_difficulty") not in snapshot.goal.difficulty_targets}
            if not active:
                return self._decision(snapshot, MainAgentAction.STOP, phase="complete", reason="target_reached")
        # First finish ready candidates, then rotate waiting candidates by their local work count.
        for hid in sorted(active, key=lambda key: (0 if active[key] in {HypothesisState.ANSWER_VERIFIED, HypothesisState.QUESTION_REALIZED} else 1,
                                                   len(candidate_decisions(snapshot, key)), key)):
            decisions = candidate_decisions(snapshot, hid)
            failures = [a for a in artifacts if a.artifact_type == "subagent_failure"
                        and a.parent_decision_id in {d.decision_id for d in decisions}]
            if len(failures) >= MAX_STAGE_FAILURES:
                return self._decision(snapshot, MainAgentAction.CLOSE_CANDIDATE, phase="candidate", hypothesis_id=hid,
                                      closure_reason="candidate_stage_failures_exhausted")
            contract = next((c for c in contracts.values() if resolutions.get(c["resolution_id"], {}).get("hypothesis_id") == hid), None)
            if active[hid] is HypothesisState.ANSWER_VERIFIED and contract:
                return self._decision(snapshot, MainAgentAction.REALIZE_QUESTION, phase="realization", hypothesis_id=hid, inputs=[contract["contract_id"]])
            if active[hid] is HypothesisState.QUESTION_REALIZED and contract:
                candidate_drafts = [d for d in drafts if d["contract_id"] == contract["contract_id"]]
                unreviewed = [d for d in candidate_drafts if d["draft_id"] not in reports]
                if unreviewed:
                    draft_id = unreviewed[-1]["draft_id"]
                    gap = next((a.payload for a in artifacts if a.artifact_type == "release_evidence_gap"
                                and a.payload["draft_id"] == draft_id), None)
                    if gap:
                        attempts = [d for d in decisions if d.action == "request_more_evidence"
                                    and d.action_parameters.get("repair_draft_id") == draft_id]
                        if not attempts:
                            search = self._search(snapshot, hypothesis=snapshot.artifacts[hid].payload, gap=gap)
                            if search.action == "request_more_evidence":
                                return search.model_copy(update={"action_parameters": {
                                    **search.action_parameters, "repair_draft_id": draft_id}})
                            return self._decision(snapshot, MainAgentAction.CLOSE_CANDIDATE, phase="verification",
                                hypothesis_id=hid, closure_reason="release_evidence_unavailable")
                        result_ids = {d.decision_id for d in attempts}
                        excluded = set(gap["used_documents"])
                        fresh = sorted({aid for a in artifacts if a.artifact_type == "provider_result"
                            and a.parent_decision_id in result_ids and a.payload.get("terminal_status") == "SUCCEEDED"
                            for aid in a.payload.get("anchor_ids", []) if aid in snapshot.artifacts
                            and snapshot.artifacts[aid].payload.get("document_id") not in excluded})
                        if fresh:
                            return self._decision(snapshot, MainAgentAction.REVIEW_RELEASE, phase="release_evidence_recheck",
                                hypothesis_id=hid, inputs=[draft_id], additional_anchor_ids=fresh)
                        return self._decision(snapshot, MainAgentAction.CLOSE_CANDIDATE, phase="verification",
                            hypothesis_id=hid, closure_reason="no_new_independent_release_evidence")
                    return self._decision(snapshot, MainAgentAction.REVIEW_RELEASE, phase="release_review", hypothesis_id=hid, inputs=[unreviewed[-1]["draft_id"]])
                repair = next((a.payload for a in artifacts if a.artifact_type == "wording_repair_request"
                               and a.payload.get("hypothesis_id") == hid), None)
                if repair:
                    return self._decision(snapshot, MainAgentAction.REALIZE_QUESTION, phase="wording_repair", hypothesis_id=hid,
                        inputs=[contract["contract_id"]], repair_feedback=repair, draft_revision=1)
            hypothesis = next((a.payload for a in artifacts if a.artifact_type == "question_hypothesis" and a.artifact_id == hid), None)
            if hypothesis is None:
                return self._decision(snapshot, MainAgentAction.CLOSE_CANDIDATE, phase="candidate", hypothesis_id=hid, closure_reason="missing_hypothesis")
            searches = [d for d in decisions if d.action == "request_more_evidence"]
            checks = [d for d in decisions if d.action == "verify_answer"]
            # Every search returns to verification; no unrelated discovery is permitted here.
            results = [a for a in artifacts if a.artifact_type == "provider_result" and a.metadata.get("hypothesis_id") == hid]
            good = [a for a in results if a.payload.get("terminal_status") == "SUCCEEDED" and a.payload.get("anchor_ids")]
            if len(searches) > len(checks) and good:
                prior_gaps = [a.payload for a in artifacts if a.artifact_type == "verification_gap" and a.payload.get("hypothesis_id") == hid]
                used = {key for gap in prior_gaps for key in gap.get("used_anchor_ids", [])}
                available = {key for a in good for key in a.payload.get("anchor_ids", [])}
                if checks and not available - used:
                    return self._decision(snapshot, MainAgentAction.CLOSE_CANDIDATE, phase="verification", hypothesis_id=hid,
                                          closure_reason="no_new_candidate_evidence")
                return self._decision(snapshot, MainAgentAction.VERIFY_ANSWER, phase="verification", hypothesis_id=hid,
                    inputs=[hid, *[a.artifact_id for a in good]], provider_result_ids=[a.artifact_id for a in good],
                    verification_round=len(checks) + 1)
            if len(searches) >= MAX_EVIDENCE_ROUNDS:
                return self._decision(snapshot, MainAgentAction.CLOSE_CANDIDATE, phase="verification", hypothesis_id=hid,
                                      closure_reason="candidate_evidence_exhausted")
            gaps = [a.payload for a in artifacts if a.artifact_type == "verification_gap" and a.payload.get("hypothesis_id") == hid]
            gap = gaps[-1] if gaps else {
                "kind": "initial_independent_verification", "hypothesis_id": hid,
                "evidence_need": "Find independent evidence to answer the question, check alternatives and contradictions. "
                                 "Candidate design acceptance does not establish answer correctness.",
                "question": hypothesis["question_intent"],
                "excluded_discovery_anchor_ids": hypothesis.get("discovery_anchor_ids", []),
            }
            if not gaps:
                review_need = next((review["evidence_need"] for a in artifacts
                                    if a.artifact_type == "candidate_usability_review"
                                    for review in a.payload.get("reviews", [])
                                    if review["hypothesis_id"] == hid and review["disposition"] == "NEEDS_EVIDENCE"
                                    and review.get("evidence_need")), None)
                if review_need:
                    gap = {**gap, "kind": "candidate_review_evidence_need", "evidence_need": review_need}
            return self._search(snapshot, hypothesis=hypothesis, gap=gap)
        # Discovery material alone enters the fact-design cycle. Candidate evidence stays local.
        unprocessed = _unprocessed_fact_material_ids(snapshot)
        discovery_ids = [a.artifact_id for a in artifacts if a.artifact_type == "evidence_material"
                         and a.metadata.get("purpose", "discovery") != "verification" and a.artifact_id in unprocessed]
        if discovery_ids:
            return self._decision(snapshot, MainAgentAction.UNDERSTAND_FACTS, phase="exploration", inputs=discovery_ids,
                                  processing_ids=discovery_ids)
        claims = [a.artifact_id for a in artifacts if a.artifact_type == "candidate_claim"]
        fingerprint = canonical_digest(sorted(claims))
        completed_designs = {a.parent_decision_id for a in artifacts if a.artifact_type == "question_design_batch"}
        designed = any(d.decision_id in completed_designs and d.action == "design_questions"
                       and d.action_parameters.get("claims_fingerprint") == fingerprint
                       for d in snapshot.decisions.values())
        if claims and not designed:
            return self._decision(snapshot, MainAgentAction.DESIGN_QUESTIONS, phase="design", claims_fingerprint=fingerprint)
        searches = [d for d in snapshot.decisions.values() if is_hierarchical(d) and d.action == "retrieve_materials"]
        if len(searches) >= exploration_budget(snapshot.goal):
            return self._decision(snapshot, MainAgentAction.STOP, phase="exploration", reason="exploration_budget_exhausted")
        if not self.provider_ids:
            return self._decision(snapshot, MainAgentAction.STOP, phase="exploration", reason="exploration_exhausted")
        gene_id = (snapshot.goal.target_entity_id or "").removeprefix("gene:")
        gene_target = any(word in (snapshot.goal.target_entity_type or "").lower() for word in ("gene", "protein"))
        if not searches and not claims and gene_target and gene_id.isdigit() and "ncbi_gene" in self.provider_ids:
            return self._decision(snapshot, MainAgentAction.RETRIEVE_MATERIALS, phase="exploration",
                provider_id="ncbi_gene", query={"native_id": gene_id, "symbol": snapshot.goal.target_entity},
                evidence_need="Establish the target gene's official identity for interpreting ambiguous literature names",
                purpose="discovery", verification_focus="comprehensive", max_search_rounds=2, max_queries_per_round=2)
        if HypothesisState.INDETERMINATE in snapshot.hypotheses.values():
            alternative = self._untried_direction(snapshot, searches)
            if alternative:
                return alternative
        return self._search(snapshot)

    def _untried_direction(self, snapshot, searches, *, additional_topics=False):
        goal = snapshot.goal
        entity = goal.target_entity
        gene = any(word in (goal.target_entity_type or "").lower() for word in ("gene", "protein"))
        routes = []
        if gene:
            routes.extend((provider, {"symbol": entity}, need) for provider, need in (
                ("hgnc", "Check official nomenclature and gene group membership"),
                ("ensembl", "Explore genomic location and classification"),
                ("ncbi_gene", "Explore the curated gene summary and documented function")))
        routes.append(("primekg", {"mention": entity, "direction": "both"},
                       "Explore graph neighbors for a different supported relation"))
        if additional_topics:
            # A new study or a graph relation can lead beyond the fields of the initial gene record.
            routes.insert(0, ("pubmed", {"term": entity}, "Look for studies directly concerning the target entity"))
        for fact_type in goal.allowed_fact_types:
            topic = fact_type.replace("_", " ")
            routes.append(("pubmed", {"term": f"{entity} {topic}"},
                           f"Explore {topic} for a different question about {entity}"))
        if additional_topics and gene:
            topics = {"classification": ("conserved protein domains", "ortholog family"),
                      "observation_association": ("tissue expression atlas", "loss of function phenotype", "protein interaction partners"),
                      "gene_location": ("genomic neighborhood",), "entity_identity": ("protein isoforms",)}
            for fact_type in goal.allowed_fact_types:
                for topic in topics.get(fact_type, ()):
                    routes.append(("pubmed", {"term": f"{entity} {topic}"}, f"Explore {topic} for {entity}"))
        preferred = question_strategy(goal).preferred_sources
        routes.sort(key=lambda route: preferred.index(route[0]) if route[0] in preferred else len(preferred))
        attempted = {(d.action_parameters.get("provider_id"), canonical_digest(d.action_parameters.get("query", {})))
                     for d in searches}
        for provider, query, need in routes:
            if provider == "ncbi_gene" and any(
                d.action_parameters.get("provider_id") == provider
                and d.action_parameters.get("query", {}).get("symbol") == entity
                for d in searches
            ):
                continue
            if provider in self.provider_ids and (provider, canonical_digest(query)) not in attempted:
                return self._decision(snapshot, MainAgentAction.RETRIEVE_MATERIALS, phase="exploration",
                    provider_id=provider, query=query, evidence_need=need, purpose="discovery",
                    verification_focus="comprehensive", max_search_rounds=2, max_queries_per_round=2,
                    rationale=need)
        return None
