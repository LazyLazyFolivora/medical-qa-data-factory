"""Bounded, checkpointed research loop delegated by the main controller."""
import json
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, TypeAdapter, model_validator

from ..knowledge_graph.errors import KnowledgeGraphError
from .react_call_contracts import Parameters, Research, Text

from ..domain.common import ContractModel
from ..llm.contracts import StructuredGenerationRequest
from ..llm.run_trace import trace_event
from .evidence_search_agent import _query_schema
from .material_aliases import material_aliases, translate_references
from .objective_question_runtime import ArtifactEnvelope
from .react_call_contracts import CONTRACTS, validate_parameters
from .react_main_agent import PROVIDER_GUIDE, REACT_WORKFLOW, TOOLS, validate_react_decision
from .react_observations import ObservationView
from .research_tools import material_passages
from .source_question_inputs import SELECTED_EVIDENCE_CHAR_BUDGET
from .subagent_contracts import RoleContractViolation, SubagentRole


RESEARCH_TOOLS = ("retrieve_materials", "read_source_details", "read_artifacts")


class SelectProvider(Parameters):
    provider_id: Text
    information_need: Text
    mention: Text | None = None


class ResearchFinding(ContractModel):
    observation: str = Field(max_length=800)
    source_ids: list[str] = Field(min_length=1, max_length=6)
    scope: str = Field(max_length=500)


class ReadingSummary(ContractModel):
    summary: str = Field(min_length=1, max_length=1600,
                         description="Brief reading note; aim for 2-4 sentences, preserve scope.")
    limitations: str = Field(max_length=800)


def _bounded_recent_read_sections(reads, content_size, *, max_sources=6,
                                  max_chars=SELECTED_EVIDENCE_CHAR_BUDGET):
    """Select recent read paragraphs without exceeding the research handoff budget."""
    selected_by_material: dict[str, list[str]] = {}
    total_chars = 0
    for view in reversed(reads):
        material_id = view["material_id"]
        if material_id not in selected_by_material:
            if len(selected_by_material) >= max_sources:
                continue
            selected_by_material[material_id] = []
        selected_paragraphs = selected_by_material[material_id]
        for paragraph_id in dict.fromkeys(view.get("returned_paragraph_ids", [])):
            if paragraph_id in selected_paragraphs:
                continue
            size = content_size(material_id, paragraph_id) + (2 if selected_paragraphs else 0)
            if total_chars + size > max_chars:
                continue
            selected_paragraphs.append(paragraph_id)
            total_chars += size
        if not selected_paragraphs:
            del selected_by_material[material_id]
    return dict(reversed(list(selected_by_material.items())))


class ResearchTurn(ContractModel):
    action: Literal["select_provider", "retrieve_materials", "read_source_details", "read_artifacts", "finish_research"]
    rationale: str = Field(min_length=1, max_length=1000)
    summary: str = Field(default="", max_length=2000,
                         description="Required non-empty research summary when finishing; optional for intermediate tool actions.")
    findings: list[ResearchFinding] = Field(default_factory=list, max_length=8)
    remaining_gaps: list[str] = Field(default_factory=list, max_length=8)
    input_artifact_ids: list[str] = Field(default_factory=list, max_length=8)
    parameters: dict = Field(default_factory=dict)
    source_sections: dict[str, list[str]] = Field(
        default_factory=dict, max_length=6,
        description="Already-read passages to hand off on finish_research. For read_artifacts, put paragraph selections in parameters.source_sections.",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_read_sections(cls, value):
        if (isinstance(value, dict) and value.get("action") == "read_artifacts"
                and value.get("source_sections") and isinstance(value.get("parameters", {}), dict)):
            params = dict(value.get("parameters", {}))
            sections = value["source_sections"]
            if params.get("source_sections") and params["source_sections"] != sections:
                raise ValueError("Conflicting source_sections; use parameters.source_sections for read_artifacts")
            params["source_sections"] = sections
            return {**value, "parameters": params, "source_sections": {}}
        return value

    @model_validator(mode="after")
    def require_final_summary(self):
        if self.action == "finish_research" and not self.summary.strip():
            raise ValueError("finish_research requires a non-empty summary of findings and remaining gaps")
        return self

    @model_validator(mode="before")
    @classmethod
    def normalize_input_ids(cls, value):
        if isinstance(value, dict) and value.get("action") == "load_provider_guide":
            value = {**value, "action": "select_provider"}  # Resume older pending source selections.
        if isinstance(value, dict) and isinstance(value.get("parameters"), dict):
            params = dict(value["parameters"])
            if "input_artifact_ids" in params:
                nested = params.pop("input_artifact_ids")
                if value.get("input_artifact_ids") and value["input_artifact_ids"] != nested:
                    raise ValueError("Conflicting input_artifact_ids; use the top-level list")
                return {**value, "input_artifact_ids": nested, "parameters": params}
        return value

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        schema = super().model_json_schema(*args, **kwargs)
        branches = []
        for action in ("select_provider", *RESEARCH_TOOLS, "finish_research"):
            params = CONTRACTS[action].json_schema() if action in RESEARCH_TOOLS else {"type": "object", "properties": {}, "additionalProperties": False}
            if action == "select_provider":
                params = TypeAdapter(SelectProvider).json_schema()
            schema.setdefault("$defs", {}).update(params.pop("$defs", {}))
            properties = {"action": {"const": action}, "parameters": params}
            required = ["action", "parameters"]
            if action == "finish_research":
                properties["summary"] = {"type": "string", "minLength": 1, "maxLength": 2000,
                                         "pattern": r"\S"}
                required.append("summary")
            if action in {"read_artifacts", "read_source_details"}:
                properties["input_artifact_ids"] = {"type": "array", "items": {"type": "string"},
                    "minItems": 1, "maxItems": 3 if action == "read_source_details" else 8}
                required.append("input_artifact_ids")
            branches.append({"properties": properties, "required": required})
        schema["anyOf"] = branches
        return schema


class ResearchSession:
    """Reuse production tools while keeping their child commits local until handoff.

    The parent decision remains pending during a pause. A checkpoint retains
    completed operations and the next pending tool, so resume does not restart
    the research or falsely mark the parent completed after the first search.
    """

    def __init__(self, loop, snapshot, decision):
        self.parent, self.base, self.decision = loop, snapshot, decision
        self.model, self.providers = loop.model, loop.providers
        self.persistence, self.goal = loop.persistence, loop.goal
        self.runtime = self
        self._empty_retrieval_streak = self._provider_failure_streak = 0
        self.path = Path(loop.runtime.root) / "objective_research" / f"{decision.decision_id}.json"
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "artifacts": {}, "history": [], "pending": None, "turn": None, "finished": False}

    def load_provider_guide(self, parameters):
        """Host-owned preparation, never an LLM tool or a research turn."""
        args = SelectProvider.model_validate(parameters)
        if args.provider_id not in self.providers:
            raise RoleContractViolation(f"Unavailable provider: {args.provider_id}")
        guide = {"description": PROVIDER_GUIDE.get(args.provider_id, ""),
                 "query": _query_schema(args.provider_id),
                 "information_need": args.information_need}
        guide["mention"] = args.mention
        reference = {"pubmed": "pubmed.md", "primekg": "primekg.md",
                     "ncbi_gene": "ncbi-gene.md"}.get(args.provider_id)
        if reference:
            guide["instructions"] = (Path(__file__).resolve().parents[3] / "skills/objective-research-agent/references" / reference).read_text(encoding="utf-8")
        if args.provider_id == "primekg":
            if not args.mention:
                raise RoleContractViolation("PrimeKG guide requires mention to inspect actual entity relations")
            guide["inventory"] = self.providers[args.provider_id].browse({"operation": "overview", "mention": args.mention})
            guide["guide_version"] = "primekg-navigation-v2"
        self.data.setdefault("loaded_provider_guides", {})[args.provider_id] = guide
        if self.data.get("provider_preparation_error", {}).get("provider_id") == args.provider_id:
            self.data.pop("provider_preparation_error", None)
        self.save()
        trace_event("research_provider_guide", parent_decision_id=self.decision.decision_id, parameters=parameters, guide=guide, loaded_by="host")

    def prepare_initial_provider(self):
        if self.data.get("initial_provider_prepared"):
            guide = self.data.get("loaded_provider_guides", {}).get("primekg")
            if guide and guide.get("guide_version") != "primekg-navigation-v2":
                try:
                    self.load_provider_guide({"provider_id": "primekg",
                        "information_need": guide.get("information_need") or self.decision.action_parameters["information_need"],
                        "mention": guide.get("mention") or self.goal.target_entity})
                except (RoleContractViolation, ValidationError, KnowledgeGraphError) as exc:
                    self.data["provider_preparation_error"] = {"provider_id": "primekg", "error": str(exc)}
                    self.save()
                    trace_event("research_provider_preparation_error", provider_id="primekg", error=str(exc))
            return
        provider_id = self.decision.action_parameters.get("preferred_provider")
        if provider_id:
            try:
                self.load_provider_guide({"provider_id": provider_id,
                    "information_need": self.decision.action_parameters["information_need"],
                    "mention": self.goal.target_entity})
            except (RoleContractViolation, ValidationError, KnowledgeGraphError) as exc:
                self.data["provider_preparation_error"] = {"provider_id": provider_id, "error": str(exc)}
                trace_event("research_provider_preparation_error", provider_id=provider_id, error=str(exc))
        pending = self.data.get("pending")
        if provider_id and isinstance(pending, dict) and pending.get("action") == "retrieve_materials":
            # An old pending query was generated before this initial preparation.
            # Preserve its trace, but regenerate under the now-supplied guidance.
            self.data["superseded_pending_before_guidance"] = self.data["pending"]
            self.data["pending"] = None
            trace_event("research_pending_query_superseded", parent_decision_id=self.decision.decision_id,
                        reason="Initial source guidance changed before execution")
        self.data["initial_provider_prepared"] = True
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def load(self):
        artifacts = {**self.base.artifacts, **{aid: ArtifactEnvelope.model_validate(a)
                     for aid, a in self.data["artifacts"].items()}}
        return self.base.model_copy(update={"artifacts": artifacts})

    def set_run_state(self, **kwargs):
        # A research subtask must never advance or finish the parent run state.
        return self.load()

    def _retrieve_materials(self, *args, **kwargs):
        from .agent_loop import ObjectiveQuestionAgentLoop
        return ObjectiveQuestionAgentLoop._retrieve_materials(self, *args, **kwargs)

    def _commit_child(self, snapshot, decision, slot_id, result, artifacts):
        self.data["artifacts"].update({a.artifact_id: a.model_dump(mode="json") for a in artifacts})
        self.data["history"].append({"action": decision.action, "parameters": decision.action_parameters,
                                     "result_ids": [a.artifact_id for a in artifacts],
                                     "execution_status": result.execution_status.value})
        self.data["pending"] = None
        self.save()
        trace_event("research_tool_result", parent_decision_id=self.decision.decision_id,
                    research_step=len(self.data["history"]), result=result.model_dump(mode="json"),
                    artifacts=[a.model_dump(mode="json") for a in artifacts])

    def read_views(self):
        return [v for a in self.data["artifacts"].values()
                for v in a["payload"].get("source_views", []) if v.get("excerpts")]

    def reading_memory(self):
        """Keep deduplicated reading coverage and cached LLM notes across turns."""
        snapshot = self.load()
        grouped = {}
        for view in self.read_views():
            if view.get("read_error"):
                continue
            aid = view["material_id"]
            entry = grouped.setdefault(aid, {"paragraph_ids": set(), "texts": []})
            texts = [e["text"] for e in view.get("excerpts", []) if e.get("text")]
            if not texts and view.get("content"):
                texts = [view["content"]]
            if not texts:
                continue
            entry["paragraph_ids"].update(view.get("returned_paragraph_ids", []))
            for text in texts:
                if text not in entry["texts"]:
                    entry["texts"].append(text)
        cache = self.data.setdefault("reading_summaries", {})
        notes = []
        for aid, entry in grouped.items():
            if not entry["texts"]:
                continue
            material = snapshot.artifacts[aid].payload
            paragraphs = sorted(entry["paragraph_ids"])
            digest = sha256(json.dumps([material.get("material_type"), paragraphs, entry["texts"]],
                                      ensure_ascii=False).encode("utf-8")).hexdigest()
            cached = cache.get(aid)
            if not cached or cached.get("read_digest") != digest:
                response = self.model.generate_structured(StructuredGenerationRequest(
                    role="objective_reading_summary", system_prompt_version="reading-summary-v1",
                    invocation_id=f"reading.{self.decision.decision_id}.{digest[:16]}",
                    idempotency_key=f"{self.base.run_id}|reading|{aid}|{digest}",
                    payload={"material_id": aid, "material_type": material.get("material_type"),
                             "source_record_id": material.get("source_record_id"),
                             "returned_paragraph_ids": paragraphs, "read_passages": entry["texts"]}), ReadingSummary)
                note = ReadingSummary.model_validate(response.parsed)
                cache[aid] = {"read_digest": digest, **note.model_dump(mode="json")}
                self.save()
                trace_event("research_reading_summary", material_id=aid, **cache[aid])
            notes.append({"material_id": aid, "provider_id": material.get("provider_id"),
                          "material_type": material.get("material_type"),
                          "returned_paragraph_ids": paragraphs,
                          "summary": cache[aid]["summary"], "limitations": cache[aid]["limitations"]})
        return {"meaning": "Coverage is recorded by tools; summaries are model-written memory, not evidence. "
                           "Empty findings do not mean unread. Use original passages for final claims.",
                "materials": notes}

    def payload(self, remaining):
        snapshot = self.load()
        view = ObservationView(snapshot)
        visible = set(self.decision.input_artifact_ids) | set(self.data["artifacts"])
        materials = [view.card(a, limit=500) for aid, a in snapshot.artifacts.items()
                     if aid in visible and a.artifact_type == "evidence_material"]
        observations = []
        for step in self.data["history"][-3:]:
            observations.append({"action": step["action"], "error": step.get("error"),
                "results": [view.content(snapshot.artifacts[aid]) for aid in step.get("result_ids", [])
                            if snapshot.artifacts[aid].artifact_type in {"react_observation", "provider_result"}]})
        hid = self.decision.action_parameters.get("hypothesis_id")
        return {"public_question": snapshot.artifacts[hid].payload.get("question_intent") if hid else None,
                "search_policy": "Search the public question and concrete evidence gap; never seed queries with the proposed answer." if hid else "Discover useful evidence for the information need.",
                "reading_priority": (
                    "Once a search returns promising materials, read the best matches before running "
                    "more queries. Choose the material IDs in materials; provider-result IDs describe searches "
                    "and are not sources to read. For broad exploration, hand off a first set of answerable "
                    "original passages as soon as you have them. Leave unsupported directions as gaps."
                ),
                "information_need": self.decision.action_parameters["information_need"],
                "entity": self.goal.target_entity, "remaining_steps": remaining,
                "preferred_provider": self.decision.action_parameters.get("preferred_provider"),
                "materials": materials[-80:], "observations": observations,
                "reading_memory": self.reading_memory(),
                "history": [{k: v for k, v in h.items() if k != "result_ids"} for h in self.data["history"][-12:]],
                "summary": self.data["turn"],
                "tools": {**{k: TOOLS[k] for k in RESEARCH_TOOLS},
                          "select_provider": "Choose another source (or a new PrimeKG entity): provider_id, information_need, optional mention. The host supplies its instructions before your next query decision. Do not include query parameters in this action.",
                          "finish_research": (
                              "Return this investigation only. Select already-read source_sections across reading pages, "
                              f"with at most {SELECTED_EVIDENCE_CHAR_BUDGET} characters in total; "
                              "summarize findings and unresolved gaps. Does not finish the main task."
                          )},
                "available_providers": {p: PROVIDER_GUIDE.get(p, "") for p in self.providers},
                "loaded_provider_guides": self.data.get("loaded_provider_guides", {}),
                "provider_preparation_error": self.data.get("provider_preparation_error")}

    def report(self, reason):
        from .agent_loop import _artifact
        turn = self.data["turn"] or {}
        reads = self.read_views()
        read_sections = {}
        for view in reads:
            read_sections.setdefault(view["material_id"], set()).update(view["returned_paragraph_ids"])
        sections = turn.get("source_sections", {}) if reason == "returned" else {}
        invalid_selection = any(
            not paragraph_ids or not set(paragraph_ids) <= read_sections.get(aid, set())
            for aid, paragraph_ids in sections.items()
        )
        use_read_fallback = (not sections or invalid_selection) and (
            reason == "step_budget_exhausted"
            or (reason == "returned" and not self.decision.action_parameters.get("hypothesis_id"))
        )
        if use_read_fallback:
            # Build an exploration handoff from actual reads if the model omitted
            # the selection or supplied locators outside its reading ledger.
            def paragraph_size(aid, paragraph_id):
                source = material_passages(
                    self, self.load(), aid, [paragraph_id],
                    max_paragraphs=None, max_chars=SELECTED_EVIDENCE_CHAR_BUDGET,
                )
                return len(source["content"])

            sections = _bounded_recent_read_sections(reads, paragraph_size)
        passages = []
        total = 0
        for aid, paragraph_ids in sections.items():
            available = read_sections.get(aid, set())
            if not paragraph_ids or not set(paragraph_ids) <= available:
                raise RoleContractViolation("Return only source_sections already read in this research session")
            source = material_passages(self, self.load(), aid, paragraph_ids,
                max_paragraphs=None, max_chars=SELECTED_EVIDENCE_CHAR_BUDGET)
            total += len(source["content"])
            if total > SELECTED_EVIDENCE_CHAR_BUDGET:
                raise RoleContractViolation(
                    f"Research handoff selected {total} characters through {aid}, exceeding "
                    f"{SELECTED_EVIDENCE_CHAR_BUDGET}; select fewer passages across all sources. "
                    "All previously read passages remain available for later questions."
                )
            passages.append({"material_id": aid, "excerpts": source["excerpts"]})
        findings = [f for f in turn.get("findings", []) if set(f["source_ids"]) <= read_sections.keys()]
        return _artifact({"artifact_id": f"research.{self.decision.decision_id}",
            "information_need": self.decision.action_parameters["information_need"],
            "completion_reason": reason, "steps": len(self.data["history"]),
            "summary": turn.get("summary", ""), "findings": findings,
            "remaining_gaps": turn.get("remaining_gaps", []),
            "tool_errors": [{"action": h["action"], "error": h["error"]} for h in self.data["history"] if h.get("error")],
            "material_ids": sorted({aid for aid, a in self.data["artifacts"].items() if a["artifact_type"] == "evidence_material"}),
            "read_material_ids": sorted(read_sections), "source_sections": sections, "source_passages": passages,
            "source_selection": ("recent_read_fallback" if use_read_fallback else "explicit") if passages else "none",
            **({"source_selection_reason": "model_selection_not_in_reading_ledger"} if invalid_selection else {}),
            "checkpoint": str(self.path), "status": "research_guidance_not_verified_evidence",
            "instruction": "Use original materials/paragraphs with discover_questions; findings are scoped model summaries, not verified facts. "
                           "An older discovery research_id still contains its old evidence, not these new passages. "
                           "To repair an existing candidate using this research, pass this report artifact_id as evidence_research_id to revise_candidate, or explicitly select source_sections "
                           "(include old passages too if needed); otherwise its old anchors will be reused. "
                           "step_budget_exhausted means only this delegation ended; unselected read excerpts remain in saved observations."},
            "react_observation", self.decision, SubagentRole.RESEARCH)


def research_evidence(loop, snapshot, decision):
    from .agent_loop import _success_result
    from .react_tools import execute_react_tool

    session = ResearchSession(loop, snapshot, decision)
    if loop.runtime.pause_requested:
        return
    session.prepare_initial_provider()
    limit = decision.action_parameters.get("max_steps", Research.model_fields["max_steps"].default)
    while not session.data["finished"] and len(session.data["history"]) < limit:
        if loop.runtime.pause_requested:
            session.save()
            return
        number = len(session.data["history"]) + 1
        if session.data["pending"] is None:
            aliases = material_aliases(session.load(), [])
            payload = translate_references(session.payload(limit - number + 1), {v: k for k, v in aliases.items()})
            response = loop.model.generate_structured(StructuredGenerationRequest(
                role="objective_research_agent", system_prompt_version="research-v1",
                invocation_id=f"research.{decision.decision_id}.{number}",
                idempotency_key=f"{snapshot.run_id}|research|{decision.decision_id}|{number}", payload=payload), ResearchTurn)
            turn = ResearchTurn.model_validate(translate_references(response.parsed, aliases))
            session.data["pending"] = turn.model_dump(mode="json")
            session.data["turn"] = turn.model_dump(mode="json")
            session.save()
        turn = ResearchTurn.model_validate(session.data["pending"])
        if loop.runtime.pause_requested:
            return
        try:
            if turn.action == "finish_research":
                session.report("returned")  # Validate selected read passages before accepting the handoff.
                session.data["finished"] = True
                session.data["pending"] = None
                session.save()
                break
            if turn.action == "select_provider":
                params = {**turn.parameters, "mention": turn.parameters.get("mention") or session.goal.target_entity}
                session.load_provider_guide(params)
                session.data["history"].append({"action": "select_provider", "parameters": turn.parameters, "result_ids": [], "execution_status": "SUCCEEDED"})
                session.data["pending"] = None
                session.save()
                continue
            try:
                validate_parameters(turn.action, turn.parameters)
            except ValueError as exc:
                # Model argument errors belong in the existing repair loop;
                # execution errors below must still propagate normally.
                raise RoleContractViolation(
                    f"Invalid parameters for {turn.action}: {exc}. Query NOT executed. "
                    "Correct the parameters and try again."
                ) from exc
            if turn.action == "retrieve_materials":
                searches = turn.parameters.get("searches", [turn.parameters])
                missing = {item.get("provider_id") for item in searches} - set(session.data.get("loaded_provider_guides", {}))
                if missing:
                    for provider_id in sorted(missing, key=str):
                        session.load_provider_guide({"provider_id": provider_id, "information_need": decision.action_parameters["information_need"], "mention": session.goal.target_entity})
                    raise RoleContractViolation("Source selection received; host loaded its guidance. Query NOT executed. Generate query parameters again using the supplied guidance.")
            hid = decision.action_parameters.get("hypothesis_id")
            params = {**turn.parameters, "workflow_version": REACT_WORKFLOW, "purpose": "verification" if hid else "discovery",
                      "rationale": turn.rationale}
            if hid:
                params["hypothesis_id"] = hid
            child = decision.model_copy(update={"decision_id": f"{decision.decision_id}.research.{number}",
                "action": "request_more_evidence" if hid and turn.action == "retrieve_materials" else turn.action, "input_artifact_ids": turn.input_artifact_ids,
                "target_subagent_role": None, "action_parameters": params})
            validate_react_decision(session.load(), child, list(loop.providers))
            trace_event("research_action", parent_decision_id=decision.decision_id,
                        research_step=number, decision=child.model_dump(mode="json"))
            if turn.action == "retrieve_materials":
                session._retrieve_materials(session.load(), child)
            else:
                execute_react_tool(session, session.load(), child)
        except (RoleContractViolation, ValidationError, KnowledgeGraphError) as exc:
            session.data["history"].append({"action": turn.action, "error": str(exc), "result_ids": []})
            session.data["pending"] = None
            session.save()
            trace_event("research_tool_feedback", parent_decision_id=decision.decision_id, error=str(exc))
    report = session.report("returned" if session.data["finished"] else "step_budget_exhausted")
    artifacts = [ArtifactEnvelope.model_validate(a) for a in session.data["artifacts"].values()]
    artifacts.append(report)
    result = _success_result(report.artifact_id, [a.artifact_id for a in artifacts], "research_returned")
    loop._commit_child(loop.runtime.load(), decision, result.subtask_id, result, artifacts)
