"""LLM-owned action selection; host validation never selects a research route."""
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from .exploration_memory import prepare_exploration_history
from .evidence_search_agent import _query_schema
from .main_agent import MainAgentAction, _ROLE_BY_ACTION, _unprocessed_fact_material_ids
from .objective_question_contracts import HypothesisState, MainAgentDecision, canonical_digest
from .question_strategies import question_strategy
from .react_observations import ObservationView
from .react_call_contracts import ResearchDesign, action_schema, validate_parameters
from .material_aliases import material_aliases, translate_references
from .react_execution_state import (
    execution_state, verification_readiness, latest_candidate_review, question_reader_accepts,
)
from .subagent_contracts import RoleContractViolation
from .workflow_scheduler import target_question_count
from .question_reader import QUESTION_REVIEW_VERSION

REACT_WORKFLOW = "objective-react-v1"

PROVIDER_GUIDE = {
    "hgnc": "Human gene approved symbols, aliases and identifiers. Useful for identity resolution, not mechanistic or clinical evidence.",
    "ncbi_gene": "Gene identity, genomic location, functional summaries and resource links. Summaries may omit experimental conditions.",
    "ensembl": "Genome coordinates, transcripts and structural annotation. Not a source of clinical treatment comparisons.",
    "primekg": "Discover related entities and graph edges. Edges are leads, not causal proof or exhaustive sets. Use real relation/type names from results; do not invent filters.",
    "pubmed": "Find research articles and title/abstract evidence. Useful for mechanisms, experiments and clinical results. Abstracts may omit methods, subgroups and tables. read_source_details retrieves available PubMed/PMC article text; unavailable content remains a gap.",
    "ctd": "Curated chemical-gene-disease associations; check evidence type and underlying publications before treating an association as a mechanism.",
}


def is_react(decision):
    return decision.action_parameters.get("workflow_version") == REACT_WORKFLOW


class NotebookFinding(ContractModel):
    statement: str = Field(min_length=1, max_length=450)
    status: Literal["supported", "tentative", "contradicted"]
    source_ids: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=list, max_length=6)
    scope: str = Field(default="", max_length=300)


NotebookLine = Annotated[str, Field(min_length=1, max_length=300)]


class WorkingNotebook(ContractModel):
    """Bounded public research state, never evidence or private reasoning."""
    current_goal: str = Field(max_length=400)
    findings: list[NotebookFinding] = Field(default_factory=list, max_length=8)
    open_questions: list[NotebookLine] = Field(default_factory=list, max_length=6)
    explored_directions: list[NotebookLine] = Field(default_factory=list, max_length=8)
    next_step: str = Field(max_length=500)

    @model_validator(mode="before")
    @classmethod
    def discard_echoed_host_metadata(cls, value):
        # The host recomputes these annotations; copying a previous note must not
        # fail the entire model request or let the model override reference checks.
        if isinstance(value, dict):
            return {k: v for k, v in value.items()
                    if k not in {"unresolved_source_ids", "observed_state_revision", "last_step_progress"}}
        return value


def latest_notebook(steps):
    for step in reversed(steps):
        note = step.get("parameters", {}).get("working_notebook")
        if note:
            return {k: v for k, v in note.items() if k != "last_step_progress"}
    return None


class ReactChoice(ContractModel):
    rationale: str = Field(min_length=1, max_length=2000)
    action: str
    research_note: str = Field(default="", max_length=2000)
    working_notebook: WorkingNotebook | None = None
    input_artifact_ids: list[str] = Field(default_factory=list, max_length=100)
    parameters: dict = Field(default_factory=dict)

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        schema = action_schema(super().model_json_schema(*args, **kwargs))
        schema["anyOf"] = [b for b in schema["anyOf"] if b["properties"]["action"]["const"] not in {"retrieve_materials", "request_more_evidence"}]
        return schema

    @model_validator(mode="after")
    def validate_tool_parameters(self):
        validate_parameters(self.action, self.parameters)
        return self

    @model_validator(mode="before")
    @classmethod
    def normalize_artifact_ids(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get("parameters"), dict):
            return value
        parameters = dict(value["parameters"])
        if "input_artifact_ids" not in parameters:
            return value
        nested = parameters.pop("input_artifact_ids")
        if value.get("input_artifact_ids") and value["input_artifact_ids"] != nested:
            raise ValueError("Conflicting input_artifact_ids; supply the top-level list only")
        return {**value, "input_artifact_ids": nested, "parameters": parameters}


TOOLS = {
    "research_evidence": "Delegate a concrete information need to an independent research agent. It loops over search, full-text acquisition and reading, then returns original passages, scoped findings and unresolved gaps. parameters: information_need, optional preferred_provider (source suggestion, not query parameters), optional hypothesis_id (links all retrieval to this candidate for verification), optional max_steps (1-64, default 24). Optional input_artifact_ids: known evidence_material IDs as leads. Prefer this for investigating a discovery opportunity's missing evidence or an unfamiliar direction. It does not draft, verify or publish. Its selected originals can be passed directly to discover_questions by report ID. Findings are research guidance. A partial report or exhausted subtask budget does not mean the entire entity is exhausted.",
    "discover_questions": "Delegate to the independent question-discovery agent after reading useful materials, when a direction stalls, or before drafting. Set input_artifact_ids to ONE research report ID from discovery_inputs.research_handoffs, or 1-6 evidence_material IDs. The report form opens its exact saved original passages; omit source_sections. In the material form, use source_sections={material_id:[paragraph_ids]} for long sources. Optional focus is an information need. Selected text must fit 32000 characters. Formulates public questions, then an independent reader checks usefulness and source-supported premises without seeing candidate answers. Returns reader-accepted questions and retained NEEDS_EVIDENCE research opportunities with observations, boundaries and gaps; it does not answer or verify facts. Only an ACCEPT opportunity can be sent to design_questions. For NEEDS_EVIDENCE, preserve its question, retrieve/read the missing evidence and call discovery again. Existing questions and the latest research notebook are supplied automatically. No need to know the research question in advance.",
    "retrieve_materials": 'Execute exactly one provider query and return unreviewed hits. Use this to investigate graph leads even before any candidate exists. No hypothesis_id required. You choose which IDs to read/extract and whether to search again. parameters: provider_id, query (object matching provider schema; PubMed example: {"term": "BRCA1 DNA repair"}), evidence_need, purpose=discovery; optional target_entity, material_limit (1-50), fetch_full_text (boolean, PubMed only).',
    "read_artifacts": "Read persisted artifacts. ALWAYS put input_artifact_ids at the TOP LEVEL, outside parameters; select up to 8 IDs, for both single and batch reading. Materials automatically return source_views with original text and paragraph indexes; no mode flag is needed. Optional parameters: source_sections={material_id:[paragraph_ids]} for selected passages, index_offset for index paging. Selected source text must fit 16000 characters in total. Other artifact records use offset/limit for JSON paging. With no IDs, list the artifact index.",
    "understand_facts": "Create persisted candidate_claim facts from selected discovery materials. input_artifact_ids: evidence_material IDs from materials_without_extracted_claims. Returns claim IDs usable by design_questions. Reading materials with read_artifacts or understanding them in your own reasoning does NOT create claims. You choose when and which materials to extract.",
    "design_questions": "Answer a reader-accepted question using its research_id from discover_questions, plus design_instruction and evidence_sufficiency_reason. That ID loads ONLY the original passages saved with that opportunity; newer research is NOT included, even if mentioned in your instruction. To use new evidence for an existing candidate, call revise_candidate with explicit source_sections. For a new question from new materials, first call discover_questions with those passages, then use its new accepted research_id. Legacy claim_ids mode requires persisted claims and an accepted question. Inspect the returned question and answer; this tool does not verify or publish.",
    "review_candidate": "Assess naturalness, usefulness, scope and planner suitability. parameters: hypothesis_id. Returns KEEP/REWRITE/NEEDS_EVIDENCE/REJECT advice; does not rewrite or close automatically.",
    "revise_candidate": "Revise an existing question or answer. parameters: hypothesis_id, revision_instruction, revision_target. Choose revision_target=answer for incorrect or unsupported answer details: the question stays fixed; missing essential evidence should be researched, not replaced by an easier question. Choose question_and_answer only for an identified question defect. For newly researched evidence, set evidence_research_id to a research_evidence report ID: the host loads its selected original passages, not its summary. Alternatively source_sections={material_id:[paragraph_ids]} selects COMPLETE replacement evidence; include old passages if still needed. Choose only one evidence selector. Without either, reuse only original anchors. Returns a NEW candidate; review and verify that new ID. This actually changes content; review_candidate does not.",
    "request_more_evidence": 'Execute one query for an EXISTING candidate; copy hypothesis_id from hypotheses, never invent it. Before a candidate exists, investigate leads with retrieve_materials instead. parameters: hypothesis_id, provider_id, query (object; PubMed: {"term": "question-based search"}), evidence_need, purpose=verification. Queries must follow the public question, never an expected answer. Related entities are allowed. Optional fetch_full_text=true requests PubMed full text.',
    "verify_answer": "Blind solve and evidence verification, then independent difficulty assessment. parameters: hypothesis_id; optional provider_result_ids. Does not retrieve evidence itself. Check verification_readiness first; missing evidence can be supplied by research_evidence with hypothesis_id. Returns issues or a publication proposal, never publishes. No local retry quota.",
    "publish_question": "Publish a verified proposal after review_candidate returned KEEP for the same hypothesis_id. The final_review returned by verify_answer is a different check and cannot replace review_candidate. parameters: hypothesis_id. Host checks persisted verification and quality prerequisites; model assertions cannot bypass them.",
    "close_candidate": "Explicitly abandon a candidate, retaining all artifacts. parameters: hypothesis_id, closure_reason.",
    "stop": "End exploration with stop_basis: target_reached, no_viable_directions, or resource_limit. Use the actual counted_toward_target and goal.question_count_upper_bound, not a remembered target. Explain unmet quota and remaining leads in rationale. Finishing current candidates does not establish exhausted directions. Provider failure does not establish exhausted scientific directions. Active candidates are retained.",
}

TOOLS["retrieve_materials"] += (' Alternatively provide searches=[{provider_id, query, evidence_need, optional material_limit, fetch_full_text}] '
    '(1-6 queries). Independent sources run concurrently; each query has its own purpose and provider schema. '
    'Do not also provide top-level provider_id/query. Related queries that depend on new results belong in the next turn.')
TOOLS["request_more_evidence"] += (' Also accepts searches=[{provider_id, query, evidence_need, optional material_limit, fetch_full_text}] '
    '(1-6) for parallel evidence retrieval; all belong to the top-level existing hypothesis_id. Do not mix batch and single query forms.')
TOOLS["read_source_details"] = ('Acquire available FULL TEXT for selected PubMed evidence_material IDs (1-3), without repeating search. '
    'parameters: evidence_need; optional hypothesis_id with purpose=verification, otherwise purpose=discovery. '
    'Use when abstracts omit the experimental model, intervention/comparator, endpoint, mechanism evidence, or safety/efficacy limits needed for your question. '
    'State the concrete gap in evidence_need. Returns per-article acquisition status, separate saved material IDs, paragraph indexes and diagnostics. '
    'A prior failed acquisition is reused without network access unless parameters.retry_reason explains why another attempt may succeed. '
    'Acquisition is NOT reading. Next choose relevant methods/results paragraphs via read_artifacts source_sections. '
    'Original abstracts remain saved. Text can be truncated; tables/supplements may be missing. '
    'A clear abstract fact can suffice; do not fetch full text merely to meet a quota.')
TOOLS["synthesize_evidence"] = ('Ask an LLM to assess information value and integrate 1-6 selected evidence_material IDs across suitable sources. '
    'parameters: research_question, optional source_sections={material_id:[paragraph_ids]} (1-4 paragraphs per material). '
    'Selected original text across all materials must fit 16000 characters; compare a focused set, not whole long papers. '
    'Returns source-located observations, comparisons, integrated understanding, ONE question_focus, answer_boundaries and gaps. '
    'Long sources return an index until you select source_sections. Use the synthesis artifact ID as research_id in design_questions. '
    'Does NOT create candidate_claim or verify answers. Use understand_facts to extract grounded claims after reading enough evidence.')
TOOLS["read_artifacts"] += ' Each source_sections entry selects 1-4 paragraph IDs belonging to that material. Long materials initially return an index. Very long paragraphs have paragraph_fragment/continues_at markers. Use material IDs to read source text, not earlier observation IDs.'
TOOLS["design_questions"] += ' For a NEW candidate, pass research_id from a reader-accepted opportunity. This can be an alternative over the same sources returned when a prior question had a gap. The answer stage preserves its public question. Synthesis or direct source selection alone does not authorize a new question; use discovery first. Only the explicitly selected opportunity or synthesis is used. Research notes are not evidence.'
TOOLS["discover_questions"] += ' When source_sections is omitted for a long selected material, previously read paragraphs from the host reading ledger are reused. Explicit source_sections overrides that default. The same aggregate text budget applies; summaries and unread paragraphs are never substituted.'
TOOLS["review_candidate"] += ' If this exact wording has no reader acceptance (including old or revised candidates), an answer-blind question review runs first. REWRITE may supply a concrete reason without replacement wording; use that reason to revise or rediscover, then review the new candidate.'
TOOLS["design_questions"] += ' research_id alone (with evidence_sufficiency_reason) loads the saved original passages automatically; understand_facts and claim_ids are not required. Do not combine research_id with source_sections. The host preserves that research context for this candidate and its reviews.'
TOOLS["understand_facts"] += ' Optional research_id extracts only the original passages selected by that synthesis, retaining original offsets and scope. Use this for long papers. Partial extraction does not mark the entire source processed; do not repeat it merely because the source remains in materials_without_extracted_claims.'
TOOLS["understand_facts"] += ' Alternatively pass source_sections={material_id:[paragraph_ids]} covering every selected material (1-4 IDs each, 16000 chars aggregate). Use the index from read_artifacts. No synthesis ID is needed for direct selected-passage extraction. Whole long papers are not sent to the model. Do not combine source_sections with research_id.'


TOOLS["design_questions"] += ' For revision_of only, use source_sections={material_id:[paragraph_ids]} (null only for a short complete source) to revise directly from original passages without understand_facts or claim_ids. Do not combine modes. Prefer this when you have already identified useful evidence; fact extraction remains optional.'
TOOLS["design_questions"] += ' With revision_of, the tool automatically supplies the previous candidate and its verification feedback. Select source_sections or claim_ids as usual. New candidates must be reviewed and verified again; inspect returned changes and explicitly close superseded candidates.'
TOOLS["design_questions"] += ' REQUIRED parameters.evidence_sufficiency_reason: briefly state the intended question and answer boundary, cite the selected source IDs/paragraphs supporting its key points, and explain why remaining gaps do not prevent this scoped question. Say when one source is enough; do not claim integration merely because several sources were read. This judgment is sent to the designer, not treated as verified evidence.'
TOOLS["verify_answer"] += ' A verification_gap separates diagnosis/repair_issues from evidence_need. Use revise_candidate with revision_target=answer for incorrect answer wording. Retrieve only for a concrete evidence gap. If the same gap returns without new evidence or a substantive repair, advance another available question and retain the unresolved candidate. Reader agreement alone is not support.'
TOOLS["verify_answer"] += ' For source_passages candidates, default is existing_sources: independent reading of saved original evidence, NOT independent-source corroboration. Existing claim-based candidates may explicitly use evidence_mode=existing_sources. No new search is required merely to enter this mode. Choose retrieved for additional-source verification; concrete gaps should guide further reading/search.'


def unprocessed_discovery(snapshot):
    return {aid for aid in _unprocessed_fact_material_ids(snapshot)
            if snapshot.artifacts[aid].metadata.get("purpose", "discovery") != "verification"}


def publication_for(snapshot, hid):
    return next((a for a in snapshot.artifacts.values()
                 if a.artifact_type == "react_publication_proposal" and a.payload["hypothesis_id"] == hid), None)


def duplicate_published_question(snapshot, hid):
    """Exact public-text identity only; semantic similarity remains a model judgment."""
    def text_for(candidate):
        proposal = publication_for(snapshot, candidate)
        draft = snapshot.artifacts.get(proposal.payload.get("draft_id")) if proposal else None
        text = draft.payload["question_text"] if draft else snapshot.artifacts[candidate].payload["question_intent"]
        return " ".join(text.split())
    text = text_for(hid)
    return next((other for other, state in snapshot.hypotheses.items()
                 if other != hid and state == HypothesisState.PUBLISHED and text_for(other) == text), None)


def _question_already_has_candidate(artifacts, approved_question):
    normalized = (approved_question or "").strip().casefold()
    return bool(normalized) and any(
        artifact.artifact_type == "question_hypothesis"
        and (artifact.payload.get("question_intent") or "").strip().casefold() == normalized
        for artifact in artifacts
    )


def _discovery_handoffs(snapshot, steps):
    from .question_discovery import is_research_report, research_report_sections

    used_reports = {artifact.payload.get("evidence_research_id")
                    for artifact in snapshot.artifacts.values()
                    if artifact.payload.get("status") == "discovery_advice_not_verified_evidence"}
    handoffs = []
    for step in reversed(steps):
        if step["action"] != "research_evidence" or step.get("parameters", {}).get("hypothesis_id"):
            continue
        for aid in reversed(step.get("result_ids", [])):
            artifact = snapshot.artifacts.get(aid)
            if aid in used_reports or not is_research_report(artifact):
                continue
            try:
                sections = research_report_sections(snapshot, artifact)
            except RoleContractViolation:
                continue
            handoffs.append({
                "report_id": aid,
                "information_need": artifact.payload.get("information_need", ""),
                "sources": [{"material_id": material_id,
                             "record_id": snapshot.artifacts[material_id].payload["source_record_id"],
                             "material_type": snapshot.artifacts[material_id].payload["material_type"]}
                            for material_id in sections],
            })
            if len(handoffs) >= 3:
                return handoffs
    return handoffs


def _unused_discovery_opportunities(snapshot, steps):
    """Surface previously accepted directions beyond the current candidate."""
    attempted = {step.get("parameters", {}).get("research_id") for step in steps
                 if step.get("action") == "design_questions"}
    alternatives, opportunities = [], []
    reading_by_decision = {artifact.parent_decision_id: artifact.payload["answer_source_reading"]
                           for artifact in snapshot.artifacts.values()
                           if artifact.artifact_type == "question_design_notes"
                           and artifact.payload.get("answer_source_reading")}
    for artifact in reversed(list(snapshot.artifacts.values())):
        data = artifact.payload
        question = data.get("approved_question")
        if (data.get("status") != "model_assessment_not_verified_fact"
                or data.get("question_review") != "ACCEPT" or not question
                or data.get("question_review_version") != QUESTION_REVIEW_VERSION
                or not data.get("assessment", {}).get("ready_for_design")
                or not data.get("source_passages") or artifact.artifact_id in attempted
                or data.get("draft_attempt")
                or _question_already_has_candidate(snapshot.artifacts.values(), question)):
            continue
        opportunity = {"research_id": artifact.artifact_id, "question": question}
        if data.get("origin") == "answer_reading_alternative":
            reading = reading_by_decision.get(artifact.parent_decision_id, {})
            gaps = reading.get("unestablished_links", []) or reading.get("missing_relationships", [])
            original = snapshot.artifacts.get(data.get("parent_research_id"))
            alternatives.append({**opportunity,
                                 "original_question": original.payload.get("research_question") if original else None,
                                 "original_question_gap": gaps[0][:650] if gaps else None,
                                 "supporting_observation": data.get("opportunity", {}).get("primary_evidence", {}).get("text", "")[:650]})
        else:
            opportunities.append(opportunity)
    return (alternatives + opportunities)[:4]


def _repeated_verification_gaps(snapshot, successors):
    gaps_by_candidate = {}
    for artifact in snapshot.artifacts.values():
        if artifact.artifact_type == "verification_gap" and artifact.payload.get("hypothesis_id"):
            gap = artifact.payload
            gaps_by_candidate.setdefault(gap["hypothesis_id"], []).append(gap)
    earlier_versions = {child: parent for parent, children in successors.items() for child in children}
    repeated = []
    for candidate, gaps in gaps_by_candidate.items():
        if candidate in successors or snapshot.hypotheses.get(candidate) != HypothesisState.PROPOSED:
            continue
        latest = max(gaps, key=lambda gap: gap.get("round", 0))
        issue = latest.get("evidence_need") or latest.get("diagnosis")
        if not issue:
            continue
        previous = earlier_versions.get(candidate)
        earlier_gaps = [gap for gap in gaps if gap is not latest]
        if previous:
            earlier_gaps.extend(gaps_by_candidate.get(previous, []))
        if any(
            gap.get("question") == latest.get("question")
            and (gap.get("evidence_need") or gap.get("diagnosis")) == issue
            and set(gap.get("used_anchor_ids", [])) == set(latest.get("used_anchor_ids", []))
            for gap in earlier_gaps
        ):
            repeated.append({"hypothesis_id": candidate, "question": latest["question"], "unresolved_issue": issue})
    return repeated


def validate_react_decision(snapshot, decision, provider_ids=None):
    """Check tool preconditions, not preferred stage order or scientific decisions."""
    action = decision.action
    p = decision.action_parameters
    if action == "tool_feedback":
        if not isinstance(p.get("error"), str):
            raise RoleContractViolation("Tool feedback requires an error")
        return
    if action not in TOOLS:
        raise RoleContractViolation(f"Unknown tool {action}; available tools: {list(TOOLS)}")
    if action == "stop":
        validate_parameters(action, p)
        counted = target_question_count(snapshot)
        target = snapshot.goal.question_count_upper_bound
        if p["stop_basis"] == "target_reached" and counted < target:
            raise RoleContractViolation(
                f"Target not reached: counted_toward_target={counted}, "
                f"question_count_upper_bound={target}, remaining={target - counted}. "
                "Continue useful work on remaining candidates or unexplored directions. "
                "If no viable direction remains or a concrete resource limit prevents progress, "
                "select that stop_basis and explain it; do not report target completion."
            )
        if p["stop_basis"] == "no_viable_directions" and counted < target:
            steps = [a.payload for a in snapshot.artifacts.values() if a.artifact_type == "react_step"]
            unused = _unused_discovery_opportunities(snapshot, steps)
            ready = [hid for hid, state in snapshot.hypotheses.items()
                     if state == HypothesisState.RELEASE_REVIEWED
                     and publication_readiness(snapshot, hid)["publishable"]]
            if unused or ready:
                raise RoleContractViolation(
                    f"Viable work remains: ready_to_publish={ready}, unused_questions={unused}. "
                    "Advance a candidate, or report a concrete resource limit."
                )
    if action in {"retrieve_materials", "request_more_evidence"} and "searches" in p:
        searches = p["searches"]
        if not isinstance(searches, list) or not 1 <= len(searches) <= 6:
            raise RoleContractViolation("searches must contain 1-6 query objects")
        if "provider_id" in p or "query" in p:
            raise RoleContractViolation("Use searches OR a top-level provider_id/query, not both")
        allowed = {"provider_id", "query", "evidence_need", "material_limit", "fetch_full_text", "target_entity"}
        for index, item in enumerate(searches):
            if not isinstance(item, dict) or set(item) - allowed:
                raise RoleContractViolation(f"searches[{index}] must contain provider_id/query/evidence_need and supported search options")
            params = {k: v for k, v in p.items() if k != "searches"}
            params.update(item)
            try:
                validate_react_decision(snapshot, decision.model_copy(update={"action_parameters": params}), provider_ids)
            except RoleContractViolation as exc:
                raise RoleContractViolation(f"searches[{index}]: {exc}") from exc
        return
    reference_errors = [f"Unknown artifact: {aid}" for aid in decision.input_artifact_ids
                        if aid not in snapshot.artifacts]
    if action == "read_artifacts":
        if len(decision.input_artifact_ids) > 8:
            reference_errors.append("read_artifacts accepts at most 8 IDs per call")
        if len(set(decision.input_artifact_ids)) != len(decision.input_artifact_ids):
            reference_errors.append("read_artifacts requires distinct IDs")
        sections = p.get("source_sections", {})
        if not isinstance(sections, dict):
            reference_errors.append("source_sections must be an object")
        else:
            reference_errors.extend(f"source_sections key not selected: {aid}" for aid in sections
                                    if aid not in decision.input_artifact_ids)
    if reference_errors:
        raise RoleContractViolation("; ".join(reference_errors))
    hid = p.get("hypothesis_id")
    if action == "revise_candidate":
        validate_parameters(action, p)
        if hid not in snapshot.hypotheses or hid not in snapshot.artifacts:
            raise RoleContractViolation("revise_candidate requires an existing hypothesis_id")
        if snapshot.hypotheses[hid] == HypothesisState.PUBLISHED:
            raise RoleContractViolation("Published questions cannot be revised through candidate tools")
        if p.get("evidence_research_id"):
            if p.get("source_sections"):
                raise RoleContractViolation("Choose evidence_research_id OR source_sections for replacement evidence")
            from .source_question_inputs import completed_research_evidence
            completed_research_evidence(snapshot, p["evidence_research_id"])
            return
        if p.get("source_sections"):
            if any(aid not in snapshot.artifacts or snapshot.artifacts[aid].artifact_type != "evidence_material"
                   for aid in p["source_sections"]):
                raise RoleContractViolation("Revision source_sections must select saved evidence_material IDs")
            return
        anchor_ids = snapshot.artifacts[hid].payload.get("discovery_anchor_ids", [])
        if not anchor_ids or any(aid not in snapshot.artifacts or
                snapshot.artifacts[aid].artifact_type != "evidence_anchor" for aid in anchor_ids):
            raise RoleContractViolation("Candidate has no usable saved original anchors; select original source_sections with design_questions revision_of")
        return
    if action == "research_evidence":
        validate_parameters(action, p)
        if hid and hid not in snapshot.hypotheses:
            raise RoleContractViolation(
                f"research_evidence: hypothesis_id={hid!r} does not identify an existing candidate. "
                "A run ID or discovery opportunity/research_id is not a hypothesis_id. "
                "To investigate a direction before designing a candidate, omit hypothesis_id and keep "
                "the information_need, preferred_provider and material IDs. "
                "Only candidate verification needs an existing ID from hypotheses. No research was executed."
            )
        if any(snapshot.artifacts[aid].artifact_type != "evidence_material" for aid in decision.input_artifact_ids):
            raise RoleContractViolation("Research leads must be evidence_material IDs")
        return
    if action == "discover_questions":
        from .question_discovery import discovery_report_selection
        if discovery_report_selection(snapshot, decision) is not None:
            return
    if action in {"read_source_details", "synthesize_evidence", "discover_questions"}:
        limit = 3 if action == "read_source_details" else 6
        if not 1 <= len(decision.input_artifact_ids) <= limit or len(set(decision.input_artifact_ids)) != len(decision.input_artifact_ids):
            raise RoleContractViolation(f"Select 1-{limit} distinct evidence_material IDs")
        for aid in decision.input_artifact_ids:
            artifact = snapshot.artifacts[aid]
            if artifact.artifact_type != "evidence_material":
                raise RoleContractViolation(f"{aid} is not an evidence_material")
            if action == "read_source_details" and artifact.payload.get("provider_id") != "pubmed":
                raise RoleContractViolation("read_source_details currently supports PubMed materials only")
        key = "evidence_need" if action == "read_source_details" else "research_question"
        if action != "discover_questions" and (not isinstance(p.get(key), str) or not p[key].strip()):
            raise RoleContractViolation(f"{key} must explain the information needed")
        sections = p.get("source_sections", {})
        if not isinstance(sections, dict) or set(sections) - set(decision.input_artifact_ids):
            raise RoleContractViolation("source_sections must map selected material IDs to paragraph ID lists")
        if action == "read_source_details":
            if provider_ids is not None and "pubmed" not in provider_ids:
                raise RoleContractViolation("PubMed provider is unavailable")
            if p.get("purpose", "discovery") not in {"discovery", "verification"}:
                raise RoleContractViolation("Details purpose must be discovery or verification")
            if p.get("purpose") == "verification" and (hid not in snapshot.hypotheses or hid not in snapshot.artifacts):
                raise RoleContractViolation(
                    f"read_source_details: hypothesis_id={hid!r} does not identify an existing candidate. "
                    "A discovery opportunity/research_id is not a hypothesis_id. "
                    "To read full text before designing a candidate, call read_source_details with "
                    "purpose=discovery, omit hypothesis_id, and keep the same material IDs and evidence_need. "
                    "For candidate verification, copy an existing ID from hypotheses. No download was attempted."
                )
        return
    if action in {"review_candidate", "request_more_evidence", "verify_answer", "publish_question", "close_candidate"}:
        if not isinstance(hid, str) or hid not in snapshot.hypotheses or hid not in snapshot.artifacts:
            raise RoleContractViolation(
                f"{action}: hypothesis_id={hid!r} does not identify an existing candidate. "
                "Copy an ID from hypotheses; do not invent one. To investigate a research lead "
                "before designing a candidate, use retrieve_materials with purpose=discovery "
                "and the same provider/query/evidence_need, without hypothesis_id. No search was executed."
            )
        successors = candidate_successors(snapshot).get(hid, [])
        if successors and action in {"review_candidate", "verify_answer", "publish_question"}:
            raise RoleContractViolation(
                f"{hid} is a historical version superseded by {successors}. "
                "Review or verify the current successor instead; revision does not inherit approval. "
                "Historical artifacts remain readable.")
        if snapshot.hypotheses[hid] == HypothesisState.PUBLISHED:
            raise RoleContractViolation(
                f"{hid} is already PUBLISHED: publication succeeded, this is not a blocked or failed candidate. "
                "Do not republish or recreate the same question to fill the remaining quota. "
                "Choose another existing candidate or a distinct knowledge question; consult hypotheses and publication_readiness.")
        if snapshot.hypotheses[hid] in {HypothesisState.REJECTED, HypothesisState.INDETERMINATE}:
            raise RoleContractViolation(
                f"{hid} is {snapshot.hypotheses[hid].value}. If its recorded issue can be resolved, "
                "create a revised candidate with revision_of; otherwise continue another direction.")
    if action in {"retrieve_materials", "request_more_evidence"}:
        if not isinstance(p.get("provider_id"), str) or not p["provider_id"] or (provider_ids is not None and p["provider_id"] not in provider_ids):
            raise RoleContractViolation("Select an available provider_id")
        if not isinstance(p.get("query"), dict) or not p["query"]:
            raise RoleContractViolation(
                f"query must be a nonempty object for {p['provider_id']}; "
                f"expected shape: {_query_schema(p['provider_id'])}. No search was executed."
            )
        if not isinstance(p.get("evidence_need"), str) or not p["evidence_need"].strip():
            raise RoleContractViolation("evidence_need must be nonempty text explaining what to find. No search was executed.")
        if p["provider_id"] == "pubmed":
            term = p["query"].get("term") or p["query"].get("mention")
            if not isinstance(term, str) or not term.strip():
                raise RoleContractViolation('PubMed query requires nonempty text: {"term": "your search"}. No search was executed.')
        if "fetch_full_text" in p and not isinstance(p["fetch_full_text"], bool):
            raise RoleContractViolation("fetch_full_text must be boolean")
        if p.get("fetch_full_text") and p["provider_id"] != "pubmed":
            raise RoleContractViolation("Full-text retrieval is supported only by pubmed")
        for key in ("max_search_rounds", "max_queries_per_round"):
            if key in p and p[key] != 1:
                raise RoleContractViolation("ReAct search runs one query per action; choose the next query after observing its result")
        for key, value in p["query"].items():
            if key in {"symbol", "native_id", "mention", "term", "query"} and not isinstance(value, str):
                raise RoleContractViolation(f"query.{key} must be text, not a nested query")
        expected = "verification" if action == "request_more_evidence" else "discovery"
        if p.get("purpose", expected) != expected:
            raise RoleContractViolation(f"Use purpose={expected} for {action}")
        for key, upper in (("max_search_rounds", 8), ("max_queries_per_round", 4), ("material_limit", 50), ("preview_chars", 6000)):
            if key in p and (not isinstance(p[key], int) or not 1 <= p[key] <= upper):
                raise RoleContractViolation(f"{key} must be an integer from 1 to {upper}")
    if action == "read_artifacts":
        if len(decision.input_artifact_ids) > 8:
            raise RoleContractViolation("Read at most 8 artifacts per call")
        sections = p.get("source_sections", {})
        if not isinstance(sections, dict) or set(sections) - set(decision.input_artifact_ids):
            raise RoleContractViolation("source_sections must map selected material IDs to paragraph lists")
        if any(snapshot.artifacts[aid].artifact_type != "evidence_material" for aid in sections):
            raise RoleContractViolation("source_sections requires evidence_material IDs")
        if "paragraph_ids" in p and (len(decision.input_artifact_ids) != 1 or sections):
            raise RoleContractViolation("Use source_sections={material_id:[paragraph_ids]} to identify each source")
        for key, default in (("offset", 0), ("limit", 6000)):
            if not isinstance(p.get(key, default), int) or p.get(key, default) < (0 if key == "offset" else 1):
                raise RoleContractViolation(f"Invalid {key}")
    if action == "understand_facts":
        available = unprocessed_discovery(snapshot)
        if not decision.input_artifact_ids or not set(decision.input_artifact_ids) <= available:
            raise RoleContractViolation("Select unprocessed discovery material IDs")
        if "source_sections" in p:
            sections = p["source_sections"]
            if p.get("research_id") or not isinstance(sections, dict) or set(sections) != set(decision.input_artifact_ids):
                raise RoleContractViolation("Use source_sections mapping every selected material to paragraph IDs, OR research_id, not both")
        if p.get("research_id"):
            from .research_tools import design_research_context
            design_research_context(snapshot, p["research_id"], decision.input_artifact_ids)
    if action == "design_questions" and not p.get("revision_of"):
        from .research_tools import design_research_context
        context = design_research_context(snapshot, p.get("research_id"))
        if (not context or not context.get("approved_question") or context.get("question_review") != "ACCEPT"
                or context.get("question_review_version") != QUESTION_REVIEW_VERSION):
            raise RoleContractViolation("Before answering a new question, use discover_questions to formulate a public question and obtain reader acceptance. Select that opportunity's research_id. Existing sources remain available; do not re-retrieve them.")
    design_context = None
    if action == "design_questions" and p.get("research_id"):
        from .research_tools import design_research_context
        design_context = design_research_context(snapshot, p["research_id"])
        approved_question = (design_context.get("approved_question") or "").strip().casefold()
        if approved_question and not p.get("revision_of") and _question_already_has_candidate(
                snapshot.artifacts.values(), approved_question):
            raise RoleContractViolation(
                "Design NOT EXECUTED: this approved public question already has a candidate. "
                "Continue that candidate, or choose a different discovery opportunity; do not create duplicate answers."
            )
    if action == "design_questions" and p.get("research_id") and not p.get("claim_ids"):
        if "source_sections" in p:
            raise RoleContractViolation("Choose research_id OR source_sections; research_id loads its saved passages automatically")
        context = design_context
        if not context["source_passages"] or any(not s.get("excerpts") for s in context["source_passages"]):
            raise RoleContractViolation("Design NOT EXECUTED: synthesis has no original passages. Read source paragraphs and synthesize again.")
        if p.get("revision_of") and p["revision_of"] not in snapshot.hypotheses:
            raise RoleContractViolation("revision_of must reference an existing candidate")
        return
    if action == "design_questions" and "source_sections" in p:
        sections = p["source_sections"]
        if p.get("claim_ids") or p.get("research_id"):
            raise RoleContractViolation("Use source_sections OR claim_ids/research_id for design")
        if not isinstance(sections, dict) or not 1 <= len(sections) <= 6:
            raise RoleContractViolation("source_sections must select 1-6 original materials")
        for aid, paragraphs in sections.items():
            if aid not in snapshot.artifacts or snapshot.artifacts[aid].artifact_type != "evidence_material":
                raise RoleContractViolation("Select persisted evidence_material IDs")
            if paragraphs is not None and (not isinstance(paragraphs, list) or not paragraphs or any(not isinstance(v, str) for v in paragraphs)):
                raise RoleContractViolation("Use paragraph ID lists, or null for a complete short source")
        if p.get("revision_of") and p["revision_of"] not in snapshot.hypotheses:
            raise RoleContractViolation("revision_of must reference an existing candidate")
        return
    if action == "design_questions":
        ids = p.get("claim_ids", [])
        available = [a.artifact_id for a in snapshot.artifacts.values() if a.artifact_type == "candidate_claim"]
        if not available:
            raise RoleContractViolation(
                "design_questions was not executed: no candidate_claim facts exist. "
                "Materials and anchors are not claims; read_artifacts does not create claims. "
                "Choose understand_facts with input_artifact_ids from materials_without_extracted_claims "
                "to extract relevant materials, then use its returned claim IDs in claim_ids. "
                "If no unprocessed materials remain, inspect prior extraction results or retrieve new evidence."
            )
        if not isinstance(ids, list) or not ids or any(not isinstance(a, str) for a in ids):
            raise RoleContractViolation("claim_ids must be a nonempty list of IDs from design_inputs.available_claim_ids.")
        invalid = [f"{aid} ({snapshot.artifacts[aid].artifact_type if aid in snapshot.artifacts else 'unknown ID'})"
                   for aid in ids if aid not in available]
        if invalid:
            raise RoleContractViolation(
                "design_questions was not executed: claim_ids contains invalid inputs: " + "; ".join(invalid[:8]) +
                ". Copy candidate_claim IDs from design_inputs.available_claim_ids. "
                "To use new materials, first call understand_facts with their unprocessed material IDs."
            )
        if not isinstance(p.get("design_instruction", ""), str):
            raise RoleContractViolation("design_instruction must be text")
        if p.get("research_id"):
            from .research_tools import design_research_context
            material_ids = {snapshot.artifacts[anchor_id].payload["material_id"] for cid in ids
                            for anchor_id in snapshot.artifacts[cid].payload["supporting_anchor_ids"]}
            design_research_context(snapshot, p["research_id"], material_ids)
        if p.get("revision_of") and (not isinstance(p["revision_of"], str) or p["revision_of"] not in snapshot.hypotheses):
            raise RoleContractViolation("revision_of must identify an existing candidate")
    if action in {"verify_answer", "publish_question"} and not question_reader_accepts(snapshot, hid):
        raise RoleContractViolation(
            f"{action} NOT EXECUTED: this exact question has no current reader acceptance. "
            f"Call review_candidate with hypothesis_id={hid}; it checks the question without seeing the answer first. "
            "For NEEDS_EVIDENCE, preserve the question and research the stated gap; after obtaining new source passages, "
            "use revise_candidate with those source_sections and an instruction to keep the question, then re-review. "
            "For a wording or factual defect, revise or rediscover before answer verification/publication.")
    if action == "verify_answer":
        quality = latest_candidate_review(snapshot, hid)
        if quality and quality["reviews"][0]["disposition"] != "KEEP":
            review = quality["reviews"][0]
            raise RoleContractViolation(
                f"verify_answer NOT EXECUTED: latest candidate review {quality['artifact_id']} "
                f"is {review['disposition']}, not KEEP. Resolve this feedback before answer verification: "
                f"{review['rationale']} Do not treat a reader acceptance or older KEEP as approval of the current candidate.")
        mode = p.get("evidence_mode") or ("existing_sources" if snapshot.artifacts[hid].payload.get("evidence_basis") == "source_passages" else "retrieved")
        if mode not in {"retrieved", "existing_sources"}:
            raise RoleContractViolation("Unknown evidence_mode")
        if mode == "existing_sources":
            if p.get("provider_result_ids"):
                raise RoleContractViolation("Existing-source review does not fabricate retrieval attempts")
            if not verification_readiness(snapshot, hid, mode)["entry_ready"]:
                raise RoleContractViolation("Existing-source review requires active candidate and original anchors")
            return
        if snapshot.hypotheses[hid] not in {HypothesisState.PROPOSED, HypothesisState.VERIFICATION_PLANNED, HypothesisState.VERIFYING}:
            raise RoleContractViolation("Candidate already verified; read its proposal or create a revision")
        results = p.get("provider_result_ids", [])
        if not isinstance(results, list) or any(not isinstance(a, str) or a not in snapshot.artifacts or
                snapshot.artifacts[a].artifact_type != "provider_result" or
                snapshot.artifacts[a].metadata.get("hypothesis_id") != hid or
                snapshot.artifacts[a].metadata.get("purpose") != "verification" for a in results):
            raise RoleContractViolation("verify_answer NOT EXECUTED: provider_result_ids must belong to this candidate and have purpose=verification. "
                "Use verification_readiness.eligible_provider_result_ids, or omit the list. If empty, first research_evidence "
                "with hypothesis_id and information_need; discovery material/anchor/claim IDs cannot be used here.")
        if not verification_readiness(snapshot, hid, mode)["entry_ready"]:
            raise RoleContractViolation("verify_answer NOT EXECUTED: missing successful independent evidence retrieval for this candidate. "
                "Call research_evidence with this hypothesis_id, optional preferred_provider and information_need based "
                "only on the public question. Then inspect results and retry verify_answer. This is a missing prerequisite, "
                "not an answer-verification failure or a broken tool; repeated verify_answer calls cannot supply evidence.")
    if action == "publish_question":
        duplicate = duplicate_published_question(snapshot, hid)
        if duplicate:
            raise RoleContractViolation(f"Publication NOT EXECUTED: identical question already published as {duplicate}. "
                                        "Close this duplicate and pursue a distinct information need; changing the answer does not create a new question.")
        proposal = publication_for(snapshot, hid)
        if not proposal or snapshot.hypotheses[hid] != HypothesisState.RELEASE_REVIEWED:
            gaps = [a for a in snapshot.artifacts.values() if a.artifact_type == "verification_gap"
                    and a.payload.get("hypothesis_id") == hid and a.artifact_id.startswith("verification_gap.")]
            gap_ids = [a.artifact_id for a in sorted(gaps, key=lambda a: a.payload.get("round", 0))[-3:]]
            raise RoleContractViolation(
                f"Publication NOT EXECUTED: {hid} has no accepted answer/publication proposal; state={snapshot.hypotheses[hid].value}. "
                f"Read verification feedback {gap_ids} and resolve its concrete repair/evidence gaps, then verify_answer again. "
                "A final_review artifact may contain a failure, not approval. review_candidate KEEP and verification_readiness eligible "
                "do not mean the answer passed. Repeating publish_question cannot change this state.")
        latest = latest_candidate_review(snapshot, hid)
        if latest is not None and latest["reviews"][0]["disposition"] != "KEEP":
            review = latest["reviews"][0]
            raise RoleContractViolation(
                f"Publication NOT EXECUTED: latest review {latest['artifact_id']} is {review['disposition']}. "
                f"Resolve its feedback: {review['rationale']} Older KEEP or a passed answer check does not supersede it.")
        if latest is None:
            raise RoleContractViolation(
                f"Publication NOT EXECUTED: call review_candidate with hypothesis_id={hid} first. "
                "Its latest disposition must be KEEP. final_review from verify_answer is a different check, "
                "and including its ID cannot satisfy this prerequisite. Do not repeat publish_question before resolving this review.")
        if target_question_count(snapshot) >= snapshot.goal.question_count_upper_bound:
            raise RoleContractViolation("Requested target is already reached")


def candidate_successors(snapshot):
    """Project explicit revision links without deleting historical candidates."""
    successors = {}
    for artifact in snapshot.artifacts.values():
        if artifact.artifact_type != "question_design_notes":
            continue
        previous = artifact.payload.get("revision_of")
        if previous not in snapshot.hypotheses:
            continue
        replacements = [hid for hid in artifact.payload.get("new_hypothesis_ids", [])
                        if hid != previous and hid in snapshot.hypotheses and hid in snapshot.artifacts]
        if replacements:
            successors[previous] = list(dict.fromkeys([*successors.get(previous, []), *replacements]))
    return successors


def publication_readiness(snapshot, hypothesis_id):
    """Expose the existing publication prerequisites together, without new judgments."""
    reader_accepted = question_reader_accepts(snapshot, hypothesis_id)
    answer_verified = (snapshot.hypotheses[hypothesis_id] == HypothesisState.RELEASE_REVIEWED
                       and bool(publication_for(snapshot, hypothesis_id)))
    review = latest_candidate_review(snapshot, hypothesis_id)
    disposition = review["reviews"][0]["disposition"] if review else None
    blockers = []
    if hypothesis_id in candidate_successors(snapshot):
        blockers.append("superseded_by_revision")
    duplicate = duplicate_published_question(snapshot, hypothesis_id)
    if duplicate:
        blockers.append(f"identical_question_already_published:{duplicate}")
    if not reader_accepted:
        blockers.append("question_reader_acceptance_missing")
    if not answer_verified:
        blockers.append("answer_verification_acceptance_missing")
    if disposition != "KEEP":
        blockers.append(f"candidate_review_{disposition or 'missing'}")
    if target_question_count(snapshot) >= snapshot.goal.question_count_upper_bound:
        blockers.append("target_already_reached")
    return {"question_reader_accepted": reader_accepted, "answer_verified": answer_verified,
            "candidate_review_disposition": disposition, "publishable": not blockers,
            "blockers": blockers,
            "meaning": "Host publication prerequisites for this exact candidate. "
                "answer_verification_acceptance_missing means verify_answer has not accepted the existing candidate answer. "
                "design_questions/revise_candidate create drafts, not verification acceptance. "
                "Check verification_readiness and recorded verification gaps before choosing an action. "
                "A passed answer check cannot override a later REWRITE or missing candidate review."}


def candidate_choice_model(snapshot, model, aliases=None, *, design_ids=(), fresh_handoff=False):
    """Expose the saved sources and candidate targets available to the controller."""
    from .candidate_review_inputs import candidate_review_inputs
    from .question_discovery import is_research_report

    alias_by_id = {material_id: alias for alias, material_id in (aliases or {}).items()}
    material_options = [alias_by_id.get(a.artifact_id, a.artifact_id) for a in snapshot.artifacts.values()
                        if a.artifact_type == "evidence_material"]
    saved_reports = [a.artifact_id for a in snapshot.artifacts.values()
                     if is_research_report(a) and a.payload.get("source_passages")]
    targets = {"publish_question": [], "review_candidate": []}
    superseded = candidate_successors(snapshot)
    terminal = {HypothesisState.PUBLISHED, HypothesisState.REJECTED, HypothesisState.INDETERMINATE}
    for hid, state in snapshot.hypotheses.items():
        if state in terminal or hid in superseded:
            continue
        if publication_readiness(snapshot, hid)["publishable"]:
            targets["publish_question"].append(hid)
        previous = latest_candidate_review(snapshot, hid)
        if previous is None or previous.get("review_input_hash") != candidate_review_inputs(snapshot, hid, model)["review_input_hash"]:
            targets["review_candidate"].append(hid)

    class AvailableReactChoice(ReactChoice):
        @classmethod
        def model_json_schema(cls, *args, **kwargs):
            schema = super().model_json_schema(*args, **kwargs)
            branches = []
            for branch in schema["anyOf"]:
                action = branch["properties"]["action"]["const"]
                if action in targets:
                    if not targets[action]:
                        continue
                    branch["properties"]["parameters"]["properties"]["hypothesis_id"] = {
                        "type": "string", "enum": targets[action]}
                if action == "design_questions":
                    if not design_ids:
                        continue
                    parameters = TypeAdapter(ResearchDesign).json_schema()
                    parameters["properties"]["research_id"] = {"type": "string", "enum": list(design_ids)}
                    branch["properties"]["parameters"] = parameters
                if action == "research_evidence":
                    branch["properties"]["input_artifact_ids"] = (
                        {"type": "array", "items": {"type": "string", "enum": material_options}, "maxItems": 100}
                        if material_options else {"type": "array", "maxItems": 0}
                    )
                    if fresh_handoff:
                        parameters = branch["properties"]["parameters"]
                        parameters["properties"]["hypothesis_id"] = {"type": "string", "minLength": 1}
                        parameters["required"].append("hypothesis_id")
                if action == "discover_questions":
                    selections = []
                    for ids, limit in ((material_options, 6), (saved_reports, 1)):
                        if ids:
                            selections.append({"type": "array", "items": {"type": "string", "enum": ids},
                                               "minItems": 1, "maxItems": limit})
                    if not selections:
                        continue
                    branch["properties"]["input_artifact_ids"] = {"anyOf": selections}
                branches.append(branch)
            schema["anyOf"] = branches
            return schema

        @model_validator(mode="after")
        def require_available_candidate(self):
            if self.action in targets and self.parameters.get("hypothesis_id") not in targets[self.action]:
                raise ValueError(f"{self.action} has no available target with that ID; use the current tool targets and saved feedback")
            if self.action == "design_questions" and self.parameters.get("research_id") not in design_ids:
                raise ValueError("Use design_questions only with a reader-accepted research_id listed in unused_discovery_opportunities. "
                                 "Use discover_questions with a saved research report to find more, or revise_candidate for an existing candidate.")
            if fresh_handoff and self.action == "research_evidence" and not self.parameters.get("hypothesis_id"):
                raise ValueError("The last research action returned unused original passages. Use discover_questions with its report_id "
                                 "before starting another unrelated search. Candidate-specific verification research remains available.")
            return self

    return AvailableReactChoice, targets


class ReactMainAgent:
    def __init__(self, model, *, provider_ids, memory_root=None):
        self.model, self.provider_ids, self.memory_root = model, list(provider_ids), memory_root

    def decide(self, snapshot):
        if target_question_count(snapshot) >= snapshot.goal.question_count_upper_bound:
            digest = canonical_digest(snapshot.model_dump(mode="json"))
            return MainAgentDecision(
                decision_id=f"decision.{digest[:20]}",
                state_digest=digest,
                action="stop",
                input_artifact_ids=[],
                requested_budget={},
                expected_information_gain=0,
                action_parameters={
                    "workflow_version": REACT_WORKFLOW,
                    "phase": "react",
                    "stop_basis": "target_reached",
                },
                stop_reason="target_reached",
            )
        successors = candidate_successors(snapshot)
        artifacts = list(snapshot.artifacts.values())
        steps = sorted((a.payload for a in artifacts if a.artifact_type == "react_step"), key=lambda p: p["sequence"])
        aliases = material_aliases(snapshot, steps)
        opportunities = _unused_discovery_opportunities(snapshot, steps)
        handoffs = _discovery_handoffs(snapshot, steps)
        latest_results = steps[-1].get("result_ids", []) if steps else []
        fresh_handoff = any(item["report_id"] in latest_results for item in handoffs)
        choice_model, candidate_targets = candidate_choice_model(
            snapshot, self.model, aliases, design_ids=tuple(item["research_id"] for item in opportunities),
            fresh_handoff=fresh_handoff,
        )
        view = ObservationView(snapshot)
        # Search status and the actual provider errors are observable, not inferred from counts.
        results = [{"artifact_id": a.artifact_id, "metadata": a.metadata,
                    "request": a.payload.get("request"), "status": a.payload.get("terminal_status"),
                    "selection_status": a.payload.get("usage", {}).get("selection_status"),
                    "error_category": a.payload.get("error_category"),
                    "search_trace": a.payload.get("usage", {}).get("search_trace", []),
                    "detail_trace": a.payload.get("usage", {}).get("detail_trace", []),
                    "material_ids": a.payload.get("material_ids", [])}
                   for a in artifacts if a.artifact_type == "provider_result"]
        facts = [view.card(a) for a in artifacts if a.artifact_type == "candidate_claim"]
        index = [{"id": a.artifact_id, "type": a.artifact_type, **view.source_identity(a)} for a in artifacts
                 if a.artifact_type not in {"provider_result", "react_step", "candidate_claim",
                                            "evidence_anchor", "react_observation"}]
        research_history = [view.card(a) for a in artifacts if a.artifact_type not in {
            "provider_result", "react_step", "candidate_claim", "evidence_material", "evidence_anchor", "react_observation"}]
        observations = view.recent(steps)
        ledger = view.reading_ledger()
        visible_ids = sorted({source["material_id"] for obs in observations
                              for source in obs.get("payload", {}).get("source_views", [])
                              if source.get("content") and source.get("material_id")})
        # Restore tool outputs across pauses; token-budget compaction owns their lifetime.
        payload = {"stage": "exploration", "control_mode": REACT_WORKFLOW,
                   "goal": snapshot.goal.model_dump(mode="json"), "generation_strategy": question_strategy(snapshot.goal).payload(),
                   "available_providers": self.provider_ids, "tools": {k: v for k, v in TOOLS.items()
                       if k not in {"retrieve_materials", "request_more_evidence"}
                       and (k != "design_questions" or opportunities)
                       and (k not in candidate_targets or candidate_targets[k])},
                   "candidate_tool_targets": candidate_targets,
                   "candidate_tool_availability": "Only listed targets can use publish_question/review_candidate. "
                       "An unchanged review is already available in candidate_reviews; revise_candidate changes content. "
                       "Changed evidence or review instructions re-enable review automatically. Publication requires all existing prerequisites.",
                   "provider_guide": {pid: PROVIDER_GUIDE.get(pid, "Inspect provider schema and actual results") for pid in self.provider_ids},
                   "hypotheses": {h: {"state": s.value, "question": snapshot.artifacts[h].payload.get("question_intent"),
                                      "superseded_by": successors.get(h, [])}
                                  for h, s in snapshot.hypotheses.items() if h in snapshot.artifacts},
                   "candidate_revision_instruction": "superseded_by comes from committed revision links. "
                       "Those older versions remain as history, not independent pending questions. "
                       "Work on a successor with no further successor. Its review and evidence must be checked afresh; "
                       "a successor is not automatically correct. You may read any historical artifact.",
                   "materials_without_extracted_claims": sorted(unprocessed_discovery(snapshot)),
                   "unread_material_ids": [m["material_id"] for m in ledger["materials"]
                       if not m["returned_paragraph_ids"] and not m["whole_stored_content_returned"]],
                   "partially_read_material_ids": [m["material_id"] for m in ledger["materials"]
                       if m["returned_paragraph_ids"] and not m["whole_stored_content_returned"]],
                   "fully_read_material_ids": [m["material_id"] for m in ledger["materials"]
                       if m["whole_stored_content_returned"]],
                   "visible_source_text_ids": visible_ids,
                   "material_state_meaning": "unread/partially_read/fully_read describe text previously returned by read_artifacts, "
                       "not understanding. visible_source_text_ids identifies original passages in this request. "
                       "materials_without_extracted_claims means no complete fact extraction, NOT unread and NOT a reading to-do list. "
                       "Re-reading stored abstracts cannot obtain article full text; use read_source_details for that.",
                   "counted_toward_target": target_question_count(snapshot),
                   "published_count": sum(s == HypothesisState.PUBLISHED for s in snapshot.hypotheses.values()),
                   "budget": snapshot.budget.model_dump(mode="json"), "prior_searches": [
                       {**{k: v for k, v in step.items() if k not in {"parameters", "research_note", "rationale", "result_ids"}},
                        "result_ids": [aid for aid in step.get("result_ids", []) if aid in snapshot.artifacts
                                       and snapshot.artifacts[aid].artifact_type != "evidence_anchor"],
                        "omitted_anchor_count": sum(snapshot.artifacts[aid].artifact_type == "evidence_anchor"
                            for aid in step.get("result_ids", []) if aid in snapshot.artifacts),
                        "parameters": {k: v for k, v in step.get("parameters", {}).items()
                                       if k not in {"working_notebook", "research_note", "rationale"}}} for step in steps],
                   "working_notebook": latest_notebook(steps),
                   "execution_state": execution_state(snapshot, steps),
                   "reading_ledger": ledger,
                   "research_history": research_history,
                   "publication_readiness": {hid: publication_readiness(snapshot, hid)
                       for hid, state in snapshot.hypotheses.items() if state != HypothesisState.PUBLISHED and hid not in successors},
                   "verification_readiness": {hid: verification_readiness(snapshot, hid)
                       for hid, state in snapshot.hypotheses.items()
                       if hid not in successors and state in {HypothesisState.PROPOSED, HypothesisState.VERIFICATION_PLANNED, HypothesisState.VERIFYING}},
                   "working_notebook_contract": "Update working_notebook from actual latest tool results BEFORE choosing an action, including stop. "
                       "Replace the notebook with concise current_goal (research question), scoped findings with source_ids, "
                       "open_questions, explored_directions and next_step. Use only schema fields and length limits. "
                       "supported means source-supported, not independently verified. The selected action has not executed; "
                       "execution_state and reading_ledger contain host-owned execution facts. Notes are guidance, not evidence.",
                   "search_results": results, "facts": facts, "artifact_index": index,
                   "candidate_reviews": [review for hid in snapshot.hypotheses
                       if (review := latest_candidate_review(snapshot, hid)) is not None],
                   "candidate_review_contract": "These are the latest stored candidate judgments, not model notes. "
                       "A reader ACCEPT checks the question; it does not turn a candidate REWRITE/NEEDS_EVIDENCE into KEEP. "
                       "Use revise_candidate to actually change question/answer content using the saved evidence, or retrieve missing evidence. "
                       "Repeating review without changing inputs is not a repair.",
                   "discovery_inputs": {"research_handoffs": handoffs,
                       "contract": "Recent exploration reports with saved original passages not yet passed as a report. "
                       "Select one report_id as the sole input_artifact_ids entry in discover_questions; no source_sections are needed. "
                       "The tool supplies all of its selected originals, including the passages from shorter sources. "
                       "For an entity-only goal, consider a broad discovery pass on an available handoff before delegating "
                       "another broad search. When the last step already returned unused passages, use them before "
                       "starting another unrelated search. Candidate-specific verification research remains available."},
                   "design_inputs": {"available_claim_ids": [a["id"] for a in facts], "claim_count": len(facts),
                                     "unused_discovery_opportunities": opportunities,
                                     "repeated_verification_gaps": _repeated_verification_gaps(snapshot, successors),
                                     "repeated_gap_instruction": "If a listed gap has no new evidence or concrete repair, "
                                         "use an unused discovery opportunity before spending more actions on that gap.",
                                     "drafted_alternatives": [
                                         {"hypothesis_id": hid, "research_id": a.artifact_id,
                                          "question": a.payload.get("approved_question")}
                                         for a in artifacts if a.payload.get("origin") == "answer_reading_alternative"
                                         for hid in a.payload.get("draft_attempt", {}).get("hypothesis_ids", [])
                                         if snapshot.hypotheses.get(hid) in {HypothesisState.PROPOSED, HypothesisState.RELEASE_REVIEWED}
                                         and hid not in successors
                                     ],
                                     "drafted_alternative_instruction": "These alternatives already have answer drafts. "
                                         "Continue their candidate review and verification when the original question "
                                         "still lacks evidence. Select the hypothesis_id; do not draft them again.",
                                     "opportunity_instruction": "These reader-accepted question directions have saved source passages and no draft attempt yet. "
                                         "An entry with original_question_gap is a separate question generated from a directly "
                                         "observed relationship when the original mechanism lacked evidence. Draft such an alternative "
                                         "with its research_id before spending another action on the unchanged mechanism, unless you "
                                         "have new original evidence for the exact gap. A positive workflow state alone does not "
                                         "resolve original_question_gap. "
                                         "If one candidate remains stuck after a repair attempt, consider a different direction here. "
                                         "Before stopping with no viable directions, evaluate these directions and actual publication readiness.",
                                     "research_briefs": [{"research_id": a.artifact_id,
                                         "research_question": a.payload.get("research_question", ""),
                                         "question_focus": a.payload["assessment"].get("question_focus", ""),
                                         "ready_for_design": a.payload["assessment"].get("ready_for_design", False),
                                         "source_ids": a.payload.get("source_ids", [])}
                                         for a in artifacts if a.payload.get("status") == "model_assessment_not_verified_fact"],
                                     "contract": "For a new question, design_questions requires research_id from a reader-accepted discover_questions opportunity. research_id automatically loads its saved original passages and boundaries, without needing claims. Direct source_sections is only an alternative for revisions. The alternative claim_ids mode needs persisted candidate_claim IDs and a reader-accepted opportunity for new questions. "
                                     "understand_facts creates these from materials_without_extracted_claims; reading does not. "
                                     "artifact_index contains other artifacts, not design-ready facts."},
                   "latest_observations": observations, "latest_step": view.latest_step(steps),
                   "observation_history_contract": "latest_observations contains retained tool outputs across the run, newest first, "
                       "linked to prior_searches by step_sequence. Use returned content to continue the same task. "
                       "Older outputs may be summarized under token-budget pressure; summary is not original evidence. "
                       "Read indexed sources again when a specific needed passage is no longer visible.",
                   "research_note": steps[-1].get("research_note", "") if steps else "",
                   "note_status": "MODEL INTERPRETATION ONLY. execution_state and verification_readiness are host-owned facts and take precedence. Previous model-written note; may be stale. Latest tool outputs and actual stored questions take precedence.",
                   "research_method": "Delegate information-need discovery over selected originals to discover_questions; choose among its opportunities, "
                       "identify what the evidence supports and what remains unknown, and select the next action accordingly.",
                   "instruction": "Inspect actual observations and candidate states, update working_notebook, then choose one tool "
                       "to advance the current research question or resolve a candidate's specific gap. Give a brief rationale. "
                       "Before stop, reconcile counted_toward_target, goal.question_count_upper_bound, budget and remaining directions. "
                       "Do not present planned actions or model-written notes as completed work."}
        digest = canonical_digest(snapshot.model_dump(mode="json"))
        payload = translate_references(payload, {v: k for k, v in aliases.items()})
        payload["material_reference_instruction"] = (
            "Material IDs such as M0001 are stable handles for this run. Copy them exactly into "
            "input_artifact_ids, source_sections keys and notebook source_ids. Do not reconstruct hashes. "
            "read_artifacts accepts at most 8 distinct IDs. Unknown IDs are rejected, never guessed.")
        request = StructuredGenerationRequest(role="objective_react_agent", system_prompt_version=REACT_WORKFLOW,
            invocation_id=f"react.{snapshot.run_id}.{snapshot.state_revision}",
            idempotency_key=f"{snapshot.run_id}|react|{snapshot.state_revision}", payload=payload)
        if self.memory_root is not None:
            request = prepare_exploration_history(self.model, request, choice_model,
                Path(self.memory_root) / "react", snapshot.run_id)
        choice = None
        updated_note = latest_notebook(steps)
        try:
            choice = choice_model.model_validate(self.model.generate_structured(request, choice_model).parsed)
            choice = ReactChoice.model_validate(translate_references(choice.model_dump(mode="json"), aliases))
            if choice.working_notebook is not None:
                note = choice.working_notebook.model_dump(mode="json")
                # Keep missing references explicit rather than treating invented IDs as evidence.
                note["unresolved_source_ids"] = sorted({aid for f in note["findings"]
                    for aid in f["source_ids"] if aid not in snapshot.artifacts})
                note["observed_state_revision"] = snapshot.state_revision
                updated_note = note
            if choice.action in {"retrieve_materials", "request_more_evidence"}:
                raise RoleContractViolation("Delegate information_need and optional preferred_provider to research_evidence; research agent loads guidance and generates queries")
            if choice.action not in TOOLS:
                raise RoleContractViolation(f"Choose a tool from {list(TOOLS)}")
            action = MainAgentAction(choice.action)
            params = dict(choice.parameters)
            if action in {MainAgentAction.RETRIEVE_MATERIALS, MainAgentAction.REQUEST_MORE_EVIDENCE}:
                # PubMed has one unambiguous text query field. Preserve the exact search;
                # only adapt its representation, never change provider or research action.
                if params.get("provider_id") == "pubmed" and isinstance(params.get("query"), str):
                    params["query"] = {"term": params["query"]}
                if isinstance(params.get("searches"), list):
                    params["searches"] = [
                        {**item, "query": {"term": item["query"]}}
                        if isinstance(item, dict) and item.get("provider_id") == "pubmed" and isinstance(item.get("query"), str)
                        else item for item in params["searches"]]
                params.setdefault("purpose", "verification" if action == MainAgentAction.REQUEST_MORE_EVIDENCE else "discovery")
            if action == MainAgentAction.UNDERSTAND_FACTS:
                params["processing_ids"] = choice.input_artifact_ids
            if updated_note is not None:
                params["working_notebook"] = updated_note
            params.update(workflow_version=REACT_WORKFLOW, phase="react", rationale=choice.rationale, research_note=choice.research_note)
            params["material_aliases"] = aliases
            decision = MainAgentDecision(decision_id=f"decision.{digest[:20]}", state_digest=digest,
                action=action.value, target_subagent_role=_ROLE_BY_ACTION.get(action),
                input_artifact_ids=choice.input_artifact_ids, requested_budget={}, expected_information_gain=0.5,
                action_parameters=params, stop_reason=choice.rationale if action == MainAgentAction.STOP else None)
            validate_react_decision(snapshot, decision, self.provider_ids)
            return decision
        except (ValueError, ValidationError, RoleContractViolation) as exc:
            # Return the tool error to the same controller on the next turn. No route substitution.
            return MainAgentDecision(decision_id=f"decision.{digest[:20]}", state_digest=digest,
                action="tool_feedback", target_subagent_role=None, input_artifact_ids=[], requested_budget={},
                expected_information_gain=0, action_parameters={"workflow_version": REACT_WORKFLOW,
                    "material_aliases": aliases,
                    "working_notebook": updated_note,
                    "attempted_tool_call": choice.model_dump(mode="json") if choice is not None else None,
                    "error": str(exc),
                    "remediation": verification_readiness(snapshot, choice.parameters["hypothesis_id"])
                        if choice is not None and choice.action == "verify_answer"
                        and choice.parameters.get("hypothesis_id") in snapshot.hypotheses else None,
                    "rationale": "Attempted tool NOT EXECUTED; inspect missing prerequisites and replan"})
