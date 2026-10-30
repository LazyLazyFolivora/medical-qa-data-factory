"""Stable generation contracts.

These models never import LangGraph types: the graph state is an internal
routing detail, while everything here is a versioned public contract.
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import Field, TypeAdapter, model_validator

from ..domain.common import ContractModel, UtcDateTime
from ..domain.questions import AnswerForm, GoldAnswer

SCHEMA_VERSION_PATHS = "evidence-path-v1"
GENERATION_SPEC_VERSION = "generation-spec-v1"
QA_ITEM_VERSION = "qa-item-v1"
PLANNER_TRAJECTORY_VERSION = "planner-trajectory-v1"
PLANNER_STEP_VERSION = "planner-step-v1"


#: Hop policy. The MVP default is 3 and the hard system ceiling is 4. These
#: are policy values, not schema literals: raising the safety ceiling must not
#: require rewriting EvidencePath, LogicalQuery, Verifier or grouping.
DEFAULT_MAX_HOPS = 3
MAX_HOPS_CEILING = 4


class StopReason(str, Enum):
    TARGET_REACHED = "target_reached"
    EVIDENCE_EXHAUSTED = "evidence_exhausted"
    NO_EVIDENCE = "no_evidence"
    NO_VALID_CLAIMS = "no_valid_claims"
    NO_CANDIDATE_PROPOSED = "no_candidate_proposed"
    ALL_CANDIDATES_HARD_REJECTED = "all_candidates_hard_rejected"
    ALL_CANDIDATES_TERMINAL = "all_candidates_terminal"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INVALID_UPSTREAM_IDENTITY = "invalid_upstream_identity"
    PROVIDER_ERROR = "provider_error"
    FATAL_ERROR = "fatal_error"
    # ReAct loop stop reasons. The fixed DAG never emits these.
    EVIDENCE_SUFFICIENT = "evidence_sufficient"
    NO_NEW_EVIDENCE = "no_new_evidence"
    NO_VALID_ACTION = "no_valid_action"
    DUPLICATE_ACTION = "duplicate_action"
    MODEL_ERROR = "model_error"
    HUMAN_INPUT_REQUIRED = "human_input_required"

    @classmethod
    def priority(cls, reason: "StopReason") -> int:
        """Lower wins. Safety/identity failures beat everything else."""
        order = {
            cls.FATAL_ERROR: 0,
            cls.INVALID_UPSTREAM_IDENTITY: 1,
            cls.BUDGET_EXHAUSTED: 2,
            cls.TARGET_REACHED: 3,
            cls.EVIDENCE_SUFFICIENT: 4,
            cls.NO_NEW_EVIDENCE: 5,
            cls.PROVIDER_ERROR: 6,
            cls.MODEL_ERROR: 7,
            cls.EVIDENCE_EXHAUSTED: 8,
            cls.NO_VALID_ACTION: 9,
            cls.NO_VALID_CLAIMS: 10,
            cls.NO_CANDIDATE_PROPOSED: 11,
            cls.ALL_CANDIDATES_HARD_REJECTED: 12,
            cls.ALL_CANDIDATES_TERMINAL: 13,
            cls.DUPLICATE_ACTION: 14,
            cls.NO_EVIDENCE: 15,
            cls.HUMAN_INPUT_REQUIRED: 16,
        }
        return order.get(reason, 99)

    @classmethod
    def dominant(cls, reasons: list[str]) -> str | None:
        """Pick the highest-priority reason from candidates."""
        if not reasons:
            return None
        return min(
            (cls(item) for item in reasons if item), key=cls.priority
        ).value

    def episode_state(self) -> "EpisodeState":
        """The terminal state this reason maps onto."""
        if self in (
            StopReason.FATAL_ERROR,
            StopReason.INVALID_UPSTREAM_IDENTITY,
        ):
            return EpisodeState.FAILED
        if self is StopReason.HUMAN_INPUT_REQUIRED:
            return EpisodeState.PARTIAL
        return EpisodeState.PARTIAL


class EpisodeState(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class RelationSemantics(str, Enum):
    ASSOCIATION = "association"
    DIRECTIONAL_FACT = "directional_fact"
    GENERIC_RELATION = "generic_relation"


class UpstreamEntitySeed(ContractModel):
    """Identity handed over by the data upstream. Never re-disambiguated here."""

    provider_id: str = Field(min_length=1)
    native_id: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    raw_mention: str | None = None


class DifficultyTarget(str, Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class AnswerType(str, Enum):
    ENTITY = "entity"
    SHORT_FACT = "short_fact"


class EvidenceSource(str, Enum):
    KNOWLEDGE_GRAPH = "knowledge_graph"
    LITERATURE = "literature"


class LiteraturePolicy(str, Enum):
    FORBIDDEN = "forbidden"
    WHEN_NEEDED = "when_needed"
    REQUIRED = "required"


class ReasoningConstraints(ContractModel):
    min_hops: int = Field(default=1, ge=1, le=MAX_HOPS_CEILING)
    max_hops: int = Field(default=2, ge=1, le=MAX_HOPS_CEILING)

    @model_validator(mode="after")
    def ordered_bounds(self) -> "ReasoningConstraints":
        if self.min_hops > self.max_hops:
            raise ValueError("min_hops must not exceed max_hops")
        return self


class EvidenceConstraints(ContractModel):
    min_total_evidence: int = Field(default=1, ge=1)
    min_kg_evidence: int = Field(default=1, ge=0)
    min_literature_evidence: int = Field(default=0, ge=0)
    literature_policy: LiteraturePolicy = LiteraturePolicy.WHEN_NEEDED


class GenerationSpec(ContractModel):
    """What a data-production batch asks for.

    This is deliberately separate from ``SafetyBudget``. A difficulty target
    must never become a tool-call budget, and exhausting a budget must never be
    reported as a harder question.
    """

    schema_version: str = GENERATION_SPEC_VERSION
    spec_version: str = GENERATION_SPEC_VERSION
    question_count: int = Field(default=3, ge=1, le=20)
    difficulty_targets: list[DifficultyTarget] = Field(
        default_factory=lambda: [
            DifficultyTarget.EASY,
            DifficultyTarget.MEDIUM,
        ],
        min_length=1,
    )
    allowed_answer_types: list[AnswerType] = Field(
        default_factory=lambda: [AnswerType.ENTITY],
        min_length=1,
    )
    allowed_sources: list[EvidenceSource] = Field(
        default_factory=lambda: [
            EvidenceSource.KNOWLEDGE_GRAPH,
            EvidenceSource.LITERATURE,
        ],
        min_length=1,
    )
    reasoning_constraints: ReasoningConstraints = Field(
        default_factory=ReasoningConstraints
    )
    evidence_constraints: EvidenceConstraints = Field(
        default_factory=EvidenceConstraints
    )
    language: str = Field(default="zh-CN", min_length=2)
    question_style_ids: list[str] = Field(
        default_factory=lambda: ["layperson-search-v1"],
        min_length=1,
        max_length=3,
    )
    variants_per_blueprint: int = Field(default=1, ge=1, le=3)
    minimum_style_score: float = Field(default=0.65, ge=0, le=1)
    #: Require CTD/RAG evidence enrichment before question planning.
    evidence_enrichment: bool = False
    difficulty_policy_version: str = "difficulty-v1"
    label_policy_version: str = "planner-label-v1"

    @model_validator(mode="after")
    def evidence_constraints_are_coherent(self) -> "GenerationSpec":
        if len(set(self.difficulty_targets)) != len(self.difficulty_targets):
            raise ValueError("difficulty_targets must be unique")
        if len(set(self.question_style_ids)) != len(self.question_style_ids):
            raise ValueError("question_style_ids must be unique")
        if self.evidence_constraints.min_total_evidence < (
            self.evidence_constraints.min_kg_evidence
            + self.evidence_constraints.min_literature_evidence
        ):
            raise ValueError(
                "min_total_evidence must cover the per-source minimums"
            )
        if (
            self.evidence_constraints.literature_policy
            is LiteraturePolicy.FORBIDDEN
            and (
                EvidenceSource.LITERATURE in self.allowed_sources
                or self.evidence_constraints.min_literature_evidence > 0
            )
        ):
            raise ValueError(
                "literature cannot be allowed or required when the policy forbids it"
            )
        if (
            self.evidence_constraints.literature_policy
            is LiteraturePolicy.REQUIRED
            and EvidenceSource.LITERATURE not in self.allowed_sources
        ):
            raise ValueError(
                "the required literature policy needs literature in allowed_sources"
            )
        if (
            self.evidence_constraints.literature_policy
            is LiteraturePolicy.REQUIRED
            and self.evidence_constraints.min_literature_evidence < 1
        ):
            raise ValueError(
                "a required literature policy needs min_literature_evidence >= 1"
            )
        return self


class SafetyBudget(ContractModel):
    """Run protection only. It carries no notion of question difficulty."""

    max_tool_calls: int = Field(default=20, ge=1, le=50)
    max_wall_time_seconds: int = Field(default=180, ge=1, le=1800)
    max_edges_observed: int = Field(default=500, ge=1, le=1000)
    max_model_attempts: int = Field(default=30, ge=1, le=100)


class EntityGenerationRequest(ContractModel):
    task_id: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    seed: UpstreamEntitySeed
    question_count: int = Field(default=5, ge=1, le=20)
    #: Requirements-v2 generation target. ``None`` keeps the legacy
    #: single-hop request path readable and makes the v2 behaviour explicit.
    generation_spec: GenerationSpec | None = None
    #: Safety limits may be supplied independently from the generation spec.
    #: The legacy fields below remain authoritative when this is omitted.
    safety_budget: SafetyBudget | None = None
    policy_version: str = "entity-generation-v1"
    #: Hop ceiling, governed by RunPolicy rather than by a literal in the
    #: schema. The MVP default is 3 and the system safety ceiling is 4; a
    #: request may tighten either bound but never widen it.
    max_hops: int = Field(default=DEFAULT_MAX_HOPS, ge=1, le=MAX_HOPS_CEILING)
    max_tool_calls: int = Field(default=10, ge=1, le=50)
    max_edges_observed: int = Field(default=200, ge=1, le=1000)
    max_agent_steps: int = Field(default=20, ge=1, le=100)
    max_wall_time_seconds: int = Field(default=180, ge=1, le=1800)
    # Reserved for a later round; never used as a gate in this version.
    max_cost_amount: float | None = None
    max_total_tokens: int | None = None

    @model_validator(mode="after")
    def resolved_count_is_consistent(self) -> "EntityGenerationRequest":
        if (
            self.generation_spec is not None
            and self.generation_spec.question_count != self.question_count
        ):
            raise ValueError(
                "question_count must equal generation_spec.question_count"
            )
        return self

    def resolved_generation_spec(self) -> GenerationSpec:
        """Materialise the effective spec without mutating the request."""
        if self.generation_spec is not None:
            return self.generation_spec
        return GenerationSpec(question_count=self.question_count)

    def resolved_safety_budget(self) -> SafetyBudget:
        if self.safety_budget is not None:
            return self.safety_budget
        return SafetyBudget(
            max_tool_calls=self.max_tool_calls,
            max_wall_time_seconds=self.max_wall_time_seconds,
            max_edges_observed=self.max_edges_observed,
            max_model_attempts=self.max_agent_steps,
        )


class GenerationEpisode(ContractModel):
    episode_id: str = Field(min_length=1)
    episode_version: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    seed: UpstreamEntitySeed
    state: EpisodeState = EpisodeState.RUNNING
    policy_version: str = Field(min_length=1)
    question_target: int = Field(ge=1)
    step_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)
    qa_item_ids: list[str] = Field(default_factory=list)
    planner_trajectory_id: str | None = None
    usage_ids: list[str] = Field(default_factory=list)
    stop_reason: StopReason | None = None
    started_at: UtcDateTime
    completed_at: UtcDateTime | None = None


class AgentStep(ContractModel):
    step_id: str = Field(min_length=1)
    step_version: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    node: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    status: Literal["started", "completed", "failed", "skipped"]
    input_digest: str = Field(min_length=1)
    output_artifact_ids: list[str] = Field(default_factory=list)
    tool_observation_ids: list[str] = Field(default_factory=list)
    model_invocation_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    retryable: bool = False
    elapsed_ms: int | None = Field(default=None, ge=0)
    started_at: UtcDateTime
    completed_at: UtcDateTime | None = None

    @model_validator(mode="after")
    def terminal_status_has_completion(self) -> "AgentStep":
        if self.status in ("completed", "failed") and self.completed_at is None:
            raise ValueError("terminal step requires completed_at")
        if self.status == "started" and self.completed_at is not None:
            raise ValueError("a started step must not carry completed_at")
        return self


class ToolObservation(ContractModel):
    observation_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    operation_key: str = Field(min_length=1)
    request_digest: str = Field(min_length=1)
    result_artifact_ids: list[str] = Field(default_factory=list)
    row_count: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(ge=0)
    error_code: str | None = None
    # ReAct loop fields. The fixed DAG only needs the legacy ones above, so
    # every field added here has a default and old records stay readable.
    decision_id: str | None = None
    action_type: ExplorationActionType | None = None
    status: Literal["success", "empty", "warning", "error", "rejected"] | None = None
    discovered_entity_ids: list[str] = Field(default_factory=list)
    new_evidence_ids: list[str] = Field(default_factory=list)
    new_path_ids: list[str] = Field(default_factory=list)
    rows_examined: int = Field(default=0, ge=0)
    truncated: bool = False
    retryable: bool = False
    message: str = ""
    provider_trace_id: str | None = None
    provider_snapshot_id: str | None = None
    artifact_summaries: list[dict[str, Any]] = Field(default_factory=list)


class ExplorationActionType(str, Enum):
    """Whitelisted tool actions. Nothing else may reach the executor."""

    QUERY_NEIGHBORS = "query_neighbors"
    EXPAND_ENTITY = "expand_entity"
    QUERY_PATHS = "query_paths"
    QUERY_CTD = "query_ctd"
    RETRIEVE_LITERATURE = "retrieve_literature"
    FINISH = "finish"


class _NeighborFields(ContractModel):
    """Shared filter fields for the two bounded-neighbor actions."""

    subject_entity_id: str = Field(min_length=1)
    direction: Literal["incoming", "outgoing", "both"]
    relations: list[str] = Field(default_factory=list)
    target_types: list[str] = Field(default_factory=list)
    max_results: int = Field(default=50, ge=1, le=100)


class NeighborAction(_NeighborFields):
    action_type: Literal[ExplorationActionType.QUERY_NEIGHBORS] = (
        ExplorationActionType.QUERY_NEIGHBORS
    )
    cursor: str | None = None


class ExpandAction(_NeighborFields):
    action_type: Literal[ExplorationActionType.EXPAND_ENTITY] = (
        ExplorationActionType.EXPAND_ENTITY
    )


class PathAction(ContractModel):
    action_type: Literal[ExplorationActionType.QUERY_PATHS] = (
        ExplorationActionType.QUERY_PATHS
    )
    start_entity_id: str = Field(min_length=1)
    target_entity_id: str | None = None
    #: Bounded by the same policy ceiling as the request. A two-hop cap was a
    #: leftover of the two-hop-only MVP; N-hop is now first-class.
    max_hops: int = Field(default=2, ge=1, le=MAX_HOPS_CEILING)
    relations: list[str] = Field(default_factory=list)
    target_types: list[str] = Field(default_factory=list)
    max_paths: int = Field(default=10, ge=1, le=50)


class CtdAction(ContractModel):
    """Bounded lookup in the pinned CTD chemical-disease snapshot."""

    action_type: Literal[ExplorationActionType.QUERY_CTD] = (
        ExplorationActionType.QUERY_CTD
    )
    chemical_entity_id: str = Field(min_length=1)
    disease_name: str | None = Field(default=None, min_length=1)
    direct_evidence: list[str] = Field(default_factory=list)
    max_results: int = Field(default=20, ge=1, le=50)


class LiteratureAction(ContractModel):
    action_type: Literal[ExplorationActionType.RETRIEVE_LITERATURE] = (
        ExplorationActionType.RETRIEVE_LITERATURE
    )
    query: str = Field(min_length=1)
    entity_ids: list[str] = Field(default_factory=list)
    #: Requirements-v2 binds retrieval to a server-built evidence need. The
    #: legacy ``entity_ids`` form remains readable for older scripted runs.
    evidence_need_id: str | None = None
    max_results: int = Field(default=10, ge=1, le=50)


class FinishAction(ContractModel):
    action_type: Literal[ExplorationActionType.FINISH] = ExplorationActionType.FINISH
    stop_reason: StopReason


ExplorationAction = Annotated[
    Union[
        NeighborAction,
        ExpandAction,
        PathAction,
        CtdAction,
        LiteratureAction,
        FinishAction,
    ],
    Field(discriminator="action_type"),
]

#: A validated view of the discriminated union. ``Annotated[Union[...],
#: Field(discriminator=...)]`` is type metadata, so validating a raw payload
#: needs a TypeAdapter; ``ExplorationAction.model_validate`` is not valid.
EXPLORATION_ACTION_ADAPTER: TypeAdapter[ExplorationAction] = TypeAdapter(
    ExplorationAction
)


def validate_action(payload: Any) -> ExplorationAction:
    """Validate a raw action payload against the whitelisted union."""
    return EXPLORATION_ACTION_ADAPTER.validate_python(payload)


class ExplorationDecision(ContractModel):
    """One structured decision per turn. Never free text.

    ``expected_information`` and ``evidence_gap`` are short audit notes, not a
    stored chain of thought: they say what the action is for, not how the model
    reasoned to get there.
    """

    decision_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    turn: int = Field(ge=1)
    action: ExplorationAction
    expected_information: str = Field(min_length=1)
    evidence_gap: str = Field(min_length=1)
    cited_observation_ids: list[str] = Field(default_factory=list)
    action_hash: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    created_at: UtcDateTime


class QuestionBlueprint(ContractModel):
    blueprint_id: str = Field(min_length=1)
    claim_ids: list[str] = Field(min_length=1)
    focus_entity_id: str = Field(min_length=1)
    answer_entity_id: str = Field(min_length=1)
    answer_endpoint: Literal["head", "tail"]
    question_type: Literal[
        "single_fact",
        "constrained_list",
        "two_hop_relation",
        "mechanism_clue",
        "evidence_verification",
    ] = "single_fact"
    answer_form: AnswerForm
    relation_semantics: RelationSemantics
    #: Natural-QA intent supplied by the evidence-enrichment layer. This does
    #: not change the existing GoldAnswer contract or answer_mode.
    question_intent: str | None = None
    semantic_skeleton: str | None = None
    expected_route: Literal["kg", "literature", "hybrid"] = "kg"
    required_evidence_ids: list[str] = Field(min_length=1)
    # Multi-hop fields. Defaults keep old single-hop blueprints readable and
    # make the single-hop case the explicit default, not an accident.
    hop_complexity: int = Field(default=1, ge=1, le=2)
    required_path_ids: list[str] = Field(default_factory=list)
    intermediate_entity_ids: list[str] = Field(default_factory=list)
    leakage_constraints: list[str] = Field(default_factory=list)
    logical_query: dict = Field(default_factory=dict)
    composition_policy_version: str | None = None
    #: The entity type of the answer. It is part of the review group key, so
    #: it must be recorded explicitly rather than derived from an id.
    answer_entity_type: str | None = None

    @model_validator(mode="after")
    def answer_is_not_the_focus(self) -> "QuestionBlueprint":
        if self.answer_entity_id == self.focus_entity_id:
            raise ValueError("the answer must not be the focus entity itself")
        if self.hop_complexity == 2:
            # A two-hop answer must be carried by an ordered path, not by a
            # bag of edges that happen to contain two items.
            if not self.required_path_ids:
                raise ValueError("a two-hop blueprint requires required_path_ids")
            if len(self.required_evidence_ids) < 2:
                raise ValueError(
                    "a two-hop blueprint requires at least two evidence ids"
                )
            if len(self.intermediate_entity_ids) < 1:
                raise ValueError(
                    "a two-hop blueprint requires an intermediate entity"
                )
        return self
        return self


class QuestionDraft(ContractModel):
    draft_id: str = Field(min_length=1)
    blueprint_id: str = Field(min_length=1)
    question_text: str = Field(min_length=1)
    candidate_answer: GoldAnswer
    claim_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    generation_episode_id: str = Field(min_length=1)
    literature_evidence_ids: list[str] = Field(default_factory=list)
    question_type: Literal[
        "single_fact", "constrained_entity", "relation_chain"
    ] = "single_fact"
    language: str = "zh-CN"
    style_skill_id: str | None = None
    style_skill_version: str | None = None
    style_profile: str | None = None
    style_score: float | None = Field(default=None, ge=0, le=1)
    question_intent: str | None = None
    semantic_skeleton: str | None = None


class VerificationResult(ContractModel):
    draft_id: str = Field(min_length=1)
    verdict: Literal["accepted", "needs_revision", "rejected"]
    reason_codes: list[str] = Field(default_factory=list)
    message: str = ""
    blocking: bool = False
    # Optional so the append-only collection stays listable per episode.
    episode_id: str | None = None


class RejectionRecord(ContractModel):
    rejection_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    draft_id: str | None = None
    stage: Literal[
        "schema",
        "reference",
        "relation_semantics",
        "style",
        "enrichment",
        "verifier",
    ]
    reason_codes: list[str] = Field(default_factory=list)
    message: str = ""
    blocking: bool = False
    created_at: UtcDateTime


class EvidenceSufficiencyDecision(ContractModel):
    """Structured judgement about whether the evidence can carry a question.

    The verdict is advisory for the orchestrator: deterministic guardrails in
    the graph still make the final routing call.
    """

    decision_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    turn: int = Field(ge=1)
    verdict: Literal["insufficient", "sufficient", "generate"]
    evidence_gaps: list[str] = Field(default_factory=list)
    cited_evidence_ids: list[str] = Field(default_factory=list)
    cited_path_ids: list[str] = Field(default_factory=list)
    suggested_action_type: ExplorationActionType | None = None
    stop_reason: StopReason | None = None
    rationale: str = Field(min_length=1)
    guardrail_codes: list[str] = Field(default_factory=list)
    created_at: UtcDateTime


class EvidencePathStep(ContractModel):
    """One direction-aware hop inside an ordered path."""

    position: int = Field(ge=0)
    evidence_id: str = Field(min_length=1)
    traversal_direction: Literal["forward", "reverse"]
    from_entity_id: str = Field(min_length=1)
    relation: str = Field(min_length=1)
    to_entity_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    query_trace_id: str = Field(min_length=1)


class EvidencePath(ContractModel):
    """An ordered, replayable multi-hop path.

    A bag of two edges is not a path. Every step must be verified against the
    evidence registry, and the traversal result of one step must be exactly
    the start of the next.
    """

    schema_version: str = SCHEMA_VERSION_PATHS
    path_id: str = Field(min_length=1)
    path_version: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    start_entity_id: str = Field(min_length=1)
    end_entity_id: str = Field(min_length=1)
    steps: list[EvidencePathStep] = Field(min_length=2, max_length=MAX_HOPS_CEILING)
    intermediate_entity_ids: list[str] = Field(min_length=1)
    source_evidence_ids: list[str] = Field(min_length=2)
    path_hash: str = Field(min_length=1)
    query_trace_ids: list[str] = Field(min_length=2)

    @model_validator(mode="after")
    def check_structure(self) -> "EvidencePath":
        positions = [step.position for step in self.steps]
        if positions != list(range(len(self.steps))):
            raise ValueError("step positions must be contiguous from zero")
        if self.steps[0].from_entity_id != self.start_entity_id:
            raise ValueError("the first step must start at start_entity_id")
        if self.steps[-1].to_entity_id != self.end_entity_id:
            raise ValueError("the last step must end at end_entity_id")
        # Continuity: the traversal result of each step must be the start of
        # the next one.
        for left, right in zip(self.steps, self.steps[1:]):
            if left.to_entity_id != right.from_entity_id:
                raise ValueError(
                    "path is broken: step "
                    f"{left.position} ends at {left.to_entity_id} but step "
                    f"{right.position} starts at {right.from_entity_id}"
                )
        expected_intermediate = [
            step.to_entity_id for step in self.steps[:-1]
        ]
        if self.intermediate_entity_ids != expected_intermediate:
            raise ValueError(
                "intermediate_entity_ids must be derived from the step joints"
            )
        if self.source_evidence_ids != [step.evidence_id for step in self.steps]:
            raise ValueError(
                "source_evidence_ids must equal the position-ordered step evidence ids"
            )
        if len(set(self.source_evidence_ids)) != len(self.source_evidence_ids):
            # The same edge used twice is not two hops; it is a self-referential
            # loop masquerading as a path.
            raise ValueError("a path must not reuse the same evidence id")
        # A path may not stitch edges from different providers or snapshots.
        providers = {step.provider_id for step in self.steps}
        snapshots = {step.snapshot_id for step in self.steps}
        if len(providers) != 1 or len(snapshots) != 1:
            raise ValueError("a path may not cross providers or snapshots")
        if self.provider_id not in providers or self.snapshot_id not in snapshots:
            raise ValueError("path provider/snapshot must match every step")
        if sorted(self.query_trace_ids) != sorted(
            step.query_trace_id for step in self.steps
        ):
            raise ValueError("query_trace_ids must match the step query traces")
        return self


class GenerationRunResult(ContractModel):
    task_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    status: Literal["completed", "partial", "failed"]
    bundle_ids: list[str] = Field(default_factory=list)
    qa_item_ids: list[str] = Field(default_factory=list)
    planner_trajectory_id: str | None = None
    claim_ids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)
    rejection_record_ids: list[str] = Field(default_factory=list)
    stop_reason: str = Field(min_length=1)
    target_question_count: int = Field(default=0, ge=0)
    target_difficulties_satisfied: bool = False
