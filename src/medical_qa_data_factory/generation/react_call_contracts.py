"""Action-specific structural contracts, shared by decoding and local parsing.

Artifact existence and research/business decisions remain runtime checks.
"""
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


Text = Annotated[str, Field(min_length=1)]


class Parameters(BaseModel):
    # Optional tool features remain accepted; required arguments are typed here.
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class Search(Parameters):
    provider_id: Text
    query: dict[str, Any] = Field(min_length=1)
    evidence_need: Text
    material_limit: int = Field(default=10, ge=1, le=50)
    fetch_full_text: bool = False


class BatchSearch(Parameters):
    searches: list[Search] = Field(min_length=1, max_length=6)


class CandidateSearch(Search):
    hypothesis_id: Text


class CandidateBatchSearch(BatchSearch):
    hypothesis_id: Text


class Candidate(Parameters):
    hypothesis_id: Text


class CloseCandidate(Candidate):
    closure_reason: Text


class Stop(Parameters):
    stop_basis: Literal["target_reached", "no_viable_directions", "resource_limit"] = Field(
        description="Why the whole run should stop. target_reached requires the actual counted_toward_target "
                    "to meet goal.question_count_upper_bound. Otherwise explain exhausted directions or "
                    "the concrete resource limit in rationale; finishing current candidates is not target completion."
    )


class Details(Parameters):
    evidence_need: Text
    retry_reason: Text | None = Field(default=None,
        description="Explain why retrying a previously failed full-text acquisition may now succeed; otherwise its saved failure is reused.")


class Synthesis(Parameters):
    research_question: Text


class Discovery(Parameters):
    focus: str = Field(default="", max_length=1200)
    source_sections: dict[str, list[Text]] = Field(default_factory=dict, max_length=6,
        description="Optional paragraph selection for explicit material IDs. Omit when selecting a research report.")


class Research(Parameters):
    hypothesis_id: Text | None = None
    preferred_provider: Text | None = None
    information_need: Text
    max_steps: int = Field(default=24, ge=1, le=64)


class DesignParameters(Parameters):
    model_config = ConfigDict(extra="forbid")
    design_instruction: str = ""
    revision_of: Text | None = None
    workflow_version: str | None = None
    evidence_sufficiency_reason: str = Field(min_length=1, max_length=1600,
        description="Brief evidence-grounded readiness explanation: intended question/answer scope, which selected sources support it, "
                    "and remaining gaps that do not affect this scope. Explain each source's contribution; no unsupported cross-source synthesis. "
                    "This is a research judgment, not a verification result or private reasoning.")


class SourceDesign(DesignParameters):
    source_sections: dict[str, list[Text] | None] = Field(min_length=1, max_length=6)


class ClaimDesign(DesignParameters):
    claim_ids: list[Text] = Field(min_length=1)
    research_id: Text | None = None


class ResearchDesign(DesignParameters):
    research_id: Text


class CandidateRevision(Parameters):
    hypothesis_id: Text
    revision_instruction: Text
    revision_target: Literal["answer", "question_and_answer"] = Field(
        default="question_and_answer",
        description="Choose answer when the question remains valid: its wording is preserved. Choose question_and_answer only to repair an identified question defect.",
    )
    source_sections: dict[str, list[Text] | None] | None = Field(default=None, min_length=1, max_length=6,
        description="Optional complete replacement evidence selection after research; include old passages too if still needed. Omit to reuse original anchors.")
    evidence_research_id: Text | None = Field(default=None,
        description="Alternatively select a research_evidence report: use its actually read, selected original passages as complete replacement evidence. Not a discovery opportunity ID. Mutually exclusive with source_sections.")


CONTRACTS = {
    "research_evidence": TypeAdapter(Research),
    "discover_questions": TypeAdapter(Discovery),
    "retrieve_materials": TypeAdapter(Search | BatchSearch),
    "request_more_evidence": TypeAdapter(CandidateSearch | CandidateBatchSearch),
    "read_artifacts": TypeAdapter(Parameters),
    "understand_facts": TypeAdapter(Parameters),
    "design_questions": TypeAdapter(SourceDesign | ClaimDesign | ResearchDesign),
    "revise_candidate": TypeAdapter(CandidateRevision),
    "review_candidate": TypeAdapter(Candidate),
    "verify_answer": TypeAdapter(Candidate),
    "publish_question": TypeAdapter(Candidate),
    "close_candidate": TypeAdapter(CloseCandidate),
    "read_source_details": TypeAdapter(Details),
    "synthesize_evidence": TypeAdapter(Synthesis),
    "stop": TypeAdapter(Stop),
}


def validate_parameters(action, parameters):
    checked = parameters
    if action in {"retrieve_materials", "request_more_evidence"}:
        def compatible_query(item):
            if isinstance(item, dict) and item.get("provider_id") == "pubmed" and isinstance(item.get("query"), str):
                return {**item, "query": {"term": item["query"]}}
            return item
        checked = compatible_query(parameters)
        if isinstance(parameters.get("searches"), list):
            checked = {**parameters, "searches": [compatible_query(item) for item in parameters["searches"]]}
            for index, item in enumerate(checked["searches"]):
                try:
                    Search.model_validate(item)
                except ValueError as exc:
                    raise ValueError(f"searches[{index}]: {exc}") from exc
    contract = CONTRACTS.get(action)
    if contract is not None:
        # Validate only: do not manufacture defaults, queries or a different action.
        contract.validate_python(checked)
    if action in {"retrieve_materials", "request_more_evidence"}:
        if "searches" in parameters and any(k in parameters for k in ("provider_id", "query")):
            raise ValueError("Choose searches OR provider_id/query; do not mix search forms")


def action_schema(schema):
    """Expose the same parameter types to the provider, not merely in tool prose."""
    definitions = schema.setdefault("$defs", {})
    branches = []
    for action, contract in CONTRACTS.items():
        params = contract.json_schema()
        definitions.update(params.pop("$defs", {}))
        branch = {"type": "object", "additionalProperties": False,
                  "properties": {**schema["properties"], "action": {"const": action}, "parameters": params},
                  "required": list(dict.fromkeys([*schema.get("required", []), "action", "parameters"]))}
        if action in {"understand_facts", "read_source_details", "synthesize_evidence", "discover_questions"}:
            branch["properties"]["input_artifact_ids"] = {"type": "array", "items": {"type": "string"},
                "minItems": 1, "maxItems": 3 if action == "read_source_details" else 100 if action == "understand_facts" else 6}
            branch["required"].append("input_artifact_ids")
        if action == "discover_questions":
            branch["properties"]["input_artifact_ids"]["description"] = (
                "Choose either one research_evidence report ID with selected original passages or 1-6 material IDs."
            )
        if action == "read_artifacts":
            branch["properties"]["input_artifact_ids"] = {"type": "array",
                "items": {"type": "string"}, "maxItems": 8}
        branches.append(branch)
    schema["anyOf"] = branches
    return schema
