"""LangGraph orchestration.

Every node persists its own business artifacts *before* returning, so a crash
between the artifact write and the checkpoint cannot duplicate a question on
resume: ids are derived deterministically from the episode and item key, and
rewriting identical content is an idempotent success.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import END, StateGraph

from ..domain import (
    ActorType,
    ChangeRecord,
    EndpointKind,
    EvidenceBundle,
    GenerationMode,
    KGEvidence,
    LiteratureEvidence,
    QuestionSpec,
    QuestionTaxonomy,
    QuestionVariant,
    ToolSnapshot,
    ValidationCheck,
    ValidationLevel,
    ValidationSeverity,
    ValidationStatus,
)
from ..domain.common import new_id, utc_now
from ..domain.questions import (
    AnswerForm,
    Answerability,
    EvidenceRequirements,
    GoldAnswer,
)
from ..domain.questions import Route as QuestionRoute
from ..knowledge_graph import EntityLookup, NeighborQuery
from ..knowledge_graph.contracts import GraphEntity
from ..knowledge_graph.errors import KnowledgeGraphError
from ..machine_validation import apply_machine_validation
from ..repository import LocalArtifactRepository, RecordAlreadyExistsError
from .budgets import GenerationBudget
from .actions import RunPolicy
from .claims import RuleBasedClaimBuilder
from .candidate_assessment import GraphTaskCandidateAssessor
from .contracts import (
    AgentStep,
    EntityGenerationRequest,
    EpisodeState,
    GenerationEpisode,
    QuestionBlueprint,
    QuestionDraft,
    RejectionRecord,
    StopReason,
    ToolObservation,
    UpstreamEntitySeed,
)
from .ctd_execution import CtdEvidenceExecutor
from .curation import curate_evidence
from .evidence_needs import build_need_for_edge
from .grouping import REVIEW_GROUP_VERSION
from .literature_execution import LiteratureEvidenceExecutor
from .planning import QuestionPlanner
from .executor import Executor, ToolCallError
from .entity_planner import StructuredEntityQuestionPlanner
from .exploration import AgentDecisionRecord, ValidationFeedbackRecord
from .planner import StructuredModelPlanner
from .react_graph import ReactContext, build_react_graph
from .repository import VOLATILE_FIELDS, GenerationRepository
from .routing import (
    Route,
    after_build_claims,
    after_collect_neighbors,
    after_curate_evidence,
    after_machine_validate,
    after_persist_draft_questions,
    after_verify_questions,
    after_verify_seed,
)
from .semantics import RelationSemanticsPolicy
from .state import EntityGenerationState
from .skills import build_default_skill_registry
from .training import (
    PlannerStep,
    PlannerTrajectory,
    TrajectoryQuality,
    build_qa_item,
    derive_training_samples,
    stable_training_id,
)
from .training_repository import TrainingRepository
from .task_contracts import ValidatedTask
from .task_realization import TaskQuestionWriter, TaskTextSemanticVerifier
from .verification import RuleBasedAnswerVerifier
from .writing import RuleBasedQuestionWriter

VERIFICATION_POLICY_VERSION = "generation-verification-v1"
RETRYABLE_ERROR_CODES = frozenset({"kg_unavailable", "kg_query_timeout", "kg_query_error"})


def digest_of(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def stable_id(episode_id: str, artifact_type: str, logical_key: str) -> str:
    """Deterministic identity: the same input always yields the same id."""
    seed = digest_of([episode_id, artifact_type, logical_key])[:24]
    return f"{artifact_type}.{seed}"


def validated_task_realization_stop_reason(
    state: EntityGenerationState,
    *,
    accepted_question_ids: list[str],
) -> str:
    """Describe why the validated-task path did or did not publish a question.

    An empty candidate set is not equivalent to a set of candidates that all
    failed validation.  Keeping those cases separate makes the run actionable:
    the former needs better exploration/candidate construction, while the
    latter needs a different hypothesis or validation evidence.
    """

    if accepted_question_ids:
        return StopReason.EVIDENCE_SUFFICIENT.value
    statuses = dict(state.get("react_candidate_statuses", {}))
    if not statuses:
        return StopReason.NO_CANDIDATE_PROPOSED.value
    if all(status == "rejected" for status in statuses.values()):
        return StopReason.ALL_CANDIDATES_HARD_REJECTED.value
    return str(
        state.get("react_stop_reason")
        or state.get("stop_reason")
        or StopReason.EVIDENCE_EXHAUSTED.value
    )


def literature_enrichment_id(
    episode_id: str, edge_id: str, retrieval_query: str
) -> str:
    """Identify an edge/query attempt without colliding across retries."""

    query_digest = hashlib.sha256(
        retrieval_query.encode("utf-8")
    ).hexdigest()
    return stable_id(
        episode_id,
        "enrichment",
        f"{edge_id}|{query_digest}",
    )


def error_code_of(exc: Exception) -> str:
    return str(getattr(exc, "code", "internal_error"))


def is_retryable(exc: Exception) -> bool:
    """Retryability is declared per error code, never inferred from the type."""
    return error_code_of(exc) in RETRYABLE_ERROR_CODES


@dataclass
class GenerationContext:
    """Injected dependencies. Never stored in graph state."""

    request: EntityGenerationRequest
    repository: LocalArtifactRepository
    episodes: GenerationRepository
    provider: Any
    policy: RelationSemanticsPolicy = field(default_factory=RelationSemanticsPolicy)
    claim_builder: Any = field(default_factory=RuleBasedClaimBuilder)
    planner: Any = field(default_factory=QuestionPlanner)
    writer: Any = field(default_factory=RuleBasedQuestionWriter)
    verifier: Any = field(default_factory=RuleBasedAnswerVerifier)
    style_judge: Any = None
    evidence_enricher: Any = None
    # Optional structured-model port. ``rule-based`` runs without one.
    model: Any = None
    model_config_id: str = "rule-based"
    agentic_exploration: bool = False
    #: Use the v2 decision contract that can propose TaskCandidate objects.
    entity_question_loop: bool = False
    literature_provider: Any = None
    literature_provider_id: str | None = None
    literature_knowledge_base_ids: tuple[str, ...] = ()
    literature_snapshot_id: str | None = None
    budget: GenerationBudget | None = None
    tool_name: str = "kg_primekg"

    def __post_init__(self) -> None:
        if self.budget is None:
            self.budget = GenerationBudget.from_request(self.request)


def save_artifact(
    repository: LocalArtifactRepository,
    collection: str,
    record_id: str,
    version: str,
    record: Any,
) -> str:
    """Idempotent write: identical content is success, divergence is a conflict."""
    try:
        repository.save(collection, record_id, version, record)
        return "written"
    except RecordAlreadyExistsError:
        existing = repository.load(collection, record_id, version, type(record))
        # Wall-clock fields legitimately differ between a run and its replay;
        # they are excluded so a genuine content change is still detected.
        if content_digest(existing) != content_digest(record):
            raise IdempotencyConflict(f"{collection}/{record_id}/{version} diverged")
        return "existing"


def content_digest(record: Any) -> str:
    """Digest of a record's content, ignoring wall-clock timestamps."""
    payload = record.model_dump(mode="json")

    def _strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: _strip(item)
                for key, item in value.items()
                if key not in VOLATILE_FIELDS and not key.startswith("change")
            }
        if isinstance(value, list):
            return [_strip(item) for item in value]
        return value

    return digest_of(_strip(payload))


class IdempotencyConflict(RuntimeError):
    code = "idempotency_conflict"


class NodeRecorder:
    """Writes an AgentStep pair (``started`` then terminal) for each attempt."""

    def __init__(self, context: GenerationContext) -> None:
        self.context = context

    def begin(self, state: EntityGenerationState, node: str, payload: Any) -> str:
        episode_id = state["episode_id"]
        sequence = state.get("sequence", 0) + 1
        step_id = stable_id(episode_id, "step", f"{node}|{sequence}")
        # On replay a completed node must not rewrite its step. Reusing the
        # existing record keeps the audit trail append-only and makes a
        # re-run idempotent instead of conflicting on the new timestamp.
        try:
            existing = self.context.episodes.load_step(step_id, f"{step_id}.v1")
        except Exception:
            existing = None
        if existing is not None:
            return step_id
        self.context.episodes.save_step(
            AgentStep(
                step_id=step_id,
                step_version=f"{step_id}.v1",
                episode_id=episode_id,
                node=node,
                sequence=sequence,
                status="started",
                input_digest=digest_of(payload),
                started_at=utc_now(),
            )
        )
        return step_id

    def finish(
        self,
        state: EntityGenerationState,
        node: str,
        step_id: str,
        *,
        status: str = "completed",
        output_artifact_ids: list[str] | None = None,
        observation_ids: list[str] | None = None,
        error_code: str | None = None,
        retryable: bool = False,
        elapsed_ms: int = 0,
    ) -> None:
        started = self.context.episodes.load_step(step_id, f"{step_id}.v1")
        # A replayed node must not rewrite its terminal record.
        try:
            self.context.episodes.load_step(step_id, f"{step_id}.v2")
        except Exception:
            pass
        else:
            return
        self.context.episodes.save_step(
            AgentStep(
                step_id=step_id,
                step_version=f"{step_id}.v2",
                episode_id=state["episode_id"],
                node=node,
                sequence=started.sequence,
                status=status,  # type: ignore[arg-type]
                input_digest=started.input_digest,
                output_artifact_ids=output_artifact_ids or [],
                tool_observation_ids=observation_ids or [],
                error_code=error_code,
                retryable=retryable,
                elapsed_ms=elapsed_ms,
                started_at=started.started_at,
                completed_at=utc_now(),
            )
        )


def initial_state(request: EntityGenerationRequest, episode_id: str) -> EntityGenerationState:
    return EntityGenerationState(
        request=request.model_dump(mode="json"),
        episode_id=episode_id,
        task_id=request.task_id,
        current_node="verify_seed",
        seed_ref=None,
        seed_entity=None,
        bundle_id=None,
        bundle_version=None,
        evidence_ids=[],
        claim_ids=[],
        blueprints=[],
        drafts={},
        draft_question_ids=[],
        question_blueprints={},
        accepted_question_ids=[],
        rejected_question_ids=[],
        rejection_record_ids=[],
        revision_counts={"used": 0, "pending": 0},
        tool_calls=0,
        edges_observed=0,
        steps=0,
        sequence=0,
        stop_reason=None,
        errors=[],
        edges=[],
    )


def with_state(state: EntityGenerationState, **updates: Any) -> EntityGenerationState:
    payload = dict(state)
    payload["steps"] = payload.get("steps", 0) + 1
    payload["sequence"] = payload.get("sequence", 0) + 1
    payload.update(updates)
    return payload  # type: ignore[return-value]


def merge_blueprint_map(
    state: EntityGenerationState, updates: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Accumulate a mapping without dropping previously stored entries."""
    return {**state.get("blueprint_by_question_id", {}), **updates}


def seed_entity_ref(seed: UpstreamEntitySeed, entity: dict[str, Any]) -> Any:
    from ..domain import EntityRef, LinkStatus

    canonical_id = str(entity.get("canonical_id"))
    return EntityRef(
        entity_ref_id=f"entity-{canonical_id}",
        raw_mention=seed.raw_mention or str(entity.get("name")),
        canonical_name=str(entity.get("name")),
        canonical_id=canonical_id,
        namespace=seed.provider_id,
        entity_type=str(entity.get("entity_type") or seed.entity_type),
        aliases=[],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source=seed.provider_id,
    )


def evidence_bundle_v1(
    context: GenerationContext,
    entity: dict[str, Any],
    evidence: list[KGEvidence],
    literature_evidence: list[LiteratureEvidence] | None = None,
) -> EvidenceBundle:
    request = context.request
    snapshot = context.provider.snapshot()
    bundle_id = stable_id(request.workspace, "bundle", request.task_id)
    tool_snapshots = [
        ToolSnapshot(
            tool_name=context.tool_name,
            tool_version=snapshot.adapter_version,
            data_snapshot_id=snapshot.data_snapshot_id,
            config_hash=snapshot.config_hash,
            endpoint_kind=EndpointKind.PYTHON,
            retrieved_at=utc_now(),
        )
    ]
    if literature_evidence:
        literature_snapshot = next(
            (
                str(item.location["provider_snapshot_id"])
                for item in literature_evidence
                if item.location.get("provider_snapshot_id")
            ),
            context.literature_snapshot_id,
        )
        tool_snapshots.append(
            ToolSnapshot(
                tool_name=f"literature_{context.literature_provider_id or 'unknown'}",
                tool_version="literature-v1",
                data_snapshot_id=literature_snapshot,
                config_hash=digest_of(
                    {
                        "provider": context.literature_provider_id,
                        "knowledge_bases": list(
                            context.literature_knowledge_base_ids
                        ),
                        "snapshot": literature_snapshot,
                    }
                ),
                endpoint_kind=EndpointKind.HTTP,
                retrieved_at=utc_now(),
            )
        )
    return EvidenceBundle(
        bundle_id=bundle_id,
        bundle_version=f"{bundle_id}.v1",
        task_id=request.task_id,
        seed_entities=[seed_entity_ref(request.seed, entity)],
        kg_evidence=evidence,
        literature_evidence=list(literature_evidence or []),
        tool_snapshots=tool_snapshots,
        status=ValidationLevel.DRAFT,
        created_at=utc_now(),
    )


def kg_evidence_from_edge(
    edge: Any,
    *,
    direction: str,
    tool_name: str,
    snapshot: Any = None,
    query_trace: Any = None,
) -> KGEvidence:
    """Map one graph edge onto the shared KGEvidence contract.

    The stable ``source_record_id`` is the graph edge id, which is what makes
    deduplication and downstream reference resolution reproducible.
    """
    from ..domain import EvidenceDirection

    metadata = dict(edge.metadata)
    if edge.source:
        # PrimeKG's ``source`` is the upstream citation, not the provider.
        metadata["upstream_source"] = edge.source
    if snapshot is not None:
        metadata["snapshot_id"] = snapshot.data_snapshot_id
        metadata["adapter_version"] = snapshot.adapter_version
    if query_trace is not None:
        metadata["query_trace_id"] = query_trace.query_id
        metadata["records_returned"] = query_trace.records_returned
        metadata["truncated"] = query_trace.truncated
    return KGEvidence(
        evidence_id=f"kg.{digest_of([edge.provider_id, edge.edge_id])[:24]}",
        head=edge.head.ref(source=edge.provider_id),
        relation=edge.relation,
        tail=edge.tail.ref(source=edge.provider_id),
        direction=(
            EvidenceDirection.FORWARD
            if direction == "outgoing"
            else EvidenceDirection.BACKWARD
        ),
        source_database=edge.provider_id,
        source_record_id=edge.edge_id,
        confidence=1.0,
        retrieval_action={
            "provider_id": edge.provider_id,
            "tool_name": tool_name,
            "query_direction": direction,
        },
        retrieved_at=utc_now(),
        metadata=metadata,
    )

def entity_index(bundle: EvidenceBundle) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for seed in bundle.seed_entities:
        if seed.canonical_id:
            index[seed.canonical_id] = seed
    for edge in bundle.kg_evidence:
        for ref in (edge.head, edge.tail):
            if ref.canonical_id:
                index[ref.canonical_id] = ref
    return index


def native_index(bundle: EvidenceBundle) -> dict[str, str]:
    """Map canonical ids to the provider's native ids, when the edge has one."""
    natives: dict[str, str] = {}
    for edge in bundle.kg_evidence:
        for side, column in ((edge.head, "native_head_id"), (edge.tail, "native_tail_id")):
            canonical_id = side.canonical_id
            native = edge.metadata.get(column)
            if canonical_id and native:
                natives[canonical_id] = str(native)
    return natives


def _agent_evidence_summary(
    evidence: list[KGEvidence], seed_entity_id: str
) -> dict[str, Any]:
    """A bounded, relation-aware view for the next Planner decision."""
    relation_counts: dict[str, int] = {}
    seed_edges: list[dict[str, Any]] = []
    sample_edges: list[dict[str, Any]] = []
    for item in evidence:
        relation_counts[item.relation] = relation_counts.get(item.relation, 0) + 1
        edge = {
            "evidence_id": item.evidence_id,
            "head_id": item.head.canonical_id,
            "head_name": item.head.canonical_name,
            "relation": item.relation,
            "tail_id": item.tail.canonical_id,
            "tail_name": item.tail.canonical_name,
            "direction": item.direction.value,
        }
        if len(sample_edges) < 20:
            sample_edges.append(edge)
        if (
            seed_entity_id
            in {item.head.canonical_id, item.tail.canonical_id}
            and len(seed_edges) < 10
        ):
            seed_edges.append(edge)
    return {
        "total_evidence": len(evidence),
        "seed_linked_evidence_count": len(seed_edges),
        "relation_counts": dict(sorted(relation_counts.items())),
        "seed_linked_edges": seed_edges,
        "sample_edges": sample_edges,
    }


def persist_training_projection(
    context: GenerationContext,
    state: EntityGenerationState,
    *,
    final_state: EpisodeState,
) -> tuple[list[str], str, bool]:
    """Project finalized questions into QAItems and one PlannerTrajectory.

    The business QuestionSpec and EvidenceBundle remain the source of truth.
    A trajectory is always persisted, including an unsuccessful episode, so
    policy rejections and exhausted runs remain analysable training data.
    """
    episode_id = state["episode_id"]
    request = context.request
    spec = request.resolved_generation_spec()
    spec_id = stable_training_id(
        "generation-spec",
        episode_id,
        spec.model_dump(mode="json"),
        prefix="generation-spec",
    )
    trajectory_id = stable_training_id(
        "planner-trajectory", episode_id, prefix="trajectory"
    )
    bundle = None
    if state.get("bundle_id") and state.get("bundle_version"):
        bundle = context.repository.load_bundle(
            state["bundle_id"], state["bundle_version"]
        )
    discovered = set(state.get("react_known_entity_ids", []))
    seed_entity_id = (
        (state.get("seed_entity") or {}).get("canonical_id")
        or request.seed.native_id
    )
    if bundle is not None:
        if bundle.seed_entities:
            seed_entity_id = (
                bundle.seed_entities[0].canonical_id or seed_entity_id
            )
        discovered.update(
            value
            for edge in bundle.kg_evidence
            for value in (
                edge.head.canonical_id,
                edge.tail.canonical_id,
            )
            if value
        )
    steps = [
        PlannerStep.model_validate(item)
        for item in state.get("react_planner_steps", [])
    ]
    qa_items: list[Any] = []
    if bundle is not None:
        for question_id in state.get("accepted_question_ids", []):
            question = context.repository.load_latest_question(question_id)
            try:
                qa_items.append(
                    build_qa_item(
                        question=question,
                        bundle=bundle,
                        episode_id=episode_id,
                        trajectory_id=trajectory_id,
                        seed_entity_id=seed_entity_id,
                        discovered_entity_ids=discovered,
                        generation_spec=spec,
                        validation_level=(
                            "machine_validated"
                            if question.validation_level
                            is ValidationLevel.MACHINE_VALIDATED
                            else "candidate"
                        ),
                    )
                )
            except ValueError:
                # A candidate that misses the requested hops, sources or
                # evidence minimum is not promoted into a formal QAItem.
                continue
    terminal_reason = (
        state.get("stop_reason")
        or (
            StopReason.EVIDENCE_SUFFICIENT.value
            if qa_items
            else StopReason.EVIDENCE_EXHAUSTED.value
        )
    )
    if qa_items:
        quality = TrajectoryQuality.ACCEPTED
    elif final_state is EpisodeState.FAILED:
        quality = TrajectoryQuality.FAILED
    elif terminal_reason == StopReason.BUDGET_EXHAUSTED.value:
        quality = TrajectoryQuality.INCOMPLETE
    else:
        quality = TrajectoryQuality.REJECTED
    trajectory = PlannerTrajectory(
        trajectory_id=trajectory_id,
        episode_id=episode_id,
        seed_entity_id=seed_entity_id,
        generation_spec_id=spec_id,
        model_config_id=context.model_config_id,
        steps=steps,
        terminal_reason=terminal_reason,
        generated_question_ids=[item.question_id for item in qa_items],
        trajectory_quality=quality,
        label_policy_version=spec.label_policy_version,
        created_at=utc_now(),
    )
    repository = TrainingRepository(str(context.repository.root))
    repository.save_trajectory(trajectory)
    for item in qa_items:
        repository.save_qa_item(item)
    for sample in derive_training_samples(trajectory):
        repository.save_sample(sample)
    targets = {item.value for item in spec.difficulty_targets}
    satisfied = (
        len(qa_items) >= spec.question_count
        and targets <= {item.difficulty for item in qa_items}
    )
    return [item.question_id for item in qa_items], trajectory_id, satisfied


def build_graph(
    context: GenerationContext, recorder: NodeRecorder, *, checkpointer: Any = None
) -> Any:
    request = context.request
    policy = context.policy

    def observe(
        state: EntityGenerationState,
        *,
        node: str,
        operation_key: str,
        request_digest: str,
        row_count: int,
        elapsed_ms: int,
        error_code: str | None = None,
    ) -> str:
        observation = ToolObservation(
            observation_id=stable_id(state["episode_id"], "observation", operation_key),
            episode_id=state["episode_id"],
            tool_name=context.tool_name,
            operation_key=operation_key,
            request_digest=request_digest,
            row_count=row_count,
            elapsed_ms=elapsed_ms,
            error_code=error_code,
        )
        context.episodes.save_observation(observation)
        return observation.observation_id

    # ---------------- verify_seed ----------------
    def verify_seed(state: EntityGenerationState) -> EntityGenerationState:
        budget = context.budget
        assert budget is not None
        budget.spend_step()
        seed = request.seed
        step_id = recorder.begin(state, "verify_seed", seed.model_dump(mode="json"))
        started = time.monotonic()
        try:
            resolution = context.provider.resolve_entity(
                EntityLookup(
                    mention=seed.canonical_name,
                    native_id=seed.native_id,
                    entity_types=[seed.entity_type],
                    limit=10,
                )
            )
        except Exception as exc:  # provider boundary
            budget.spend_tool_call()
            elapsed = int((time.monotonic() - started) * 1000)
            recorder.finish(
                state,
                "verify_seed",
                step_id,
                status="failed",
                observation_ids=[
                    observe(
                        state,
                        node="verify_seed",
                        operation_key=f"{state['episode_id']}|verify_seed|1",
                        request_digest=digest_of(seed.model_dump(mode="json")),
                        row_count=0,
                        elapsed_ms=elapsed,
                        error_code=error_code_of(exc),
                    )
                ],
                error_code=error_code_of(exc),
                retryable=is_retryable(exc),
                elapsed_ms=elapsed,
            )
            return with_state(
                state,
                stop_reason=(
                    StopReason.PROVIDER_ERROR.value
                    if is_retryable(exc)
                    else StopReason.FATAL_ERROR.value
                ),
                errors=[
                    *state.get("errors", []),
                    {
                        "node": "verify_seed",
                        "code": error_code_of(exc),
                        "message": str(exc),
                    },
                ],
            )
        budget.spend_tool_call()
        elapsed = int((time.monotonic() - started) * 1000)
        observation_id = observe(
            state,
            node="verify_seed",
            operation_key=f"{state['episode_id']}|verify_seed|1",
            request_digest=digest_of(seed.model_dump(mode="json")),
            row_count=len(resolution.candidates),
            elapsed_ms=elapsed,
        )
        # Identity must match native id *and* entity type together. A name
        # mismatch only records a warning; it never re-resolves the entity.
        matches = [
            item
            for item in resolution.candidates
            if item.entity.native_id == seed.native_id
            and str(item.entity.entity_type or "").lower().replace("_", "-")
            == seed.entity_type.lower().replace("_", "-")
        ]
        if len(matches) != 1:
            recorder.finish(
                state,
                "verify_seed",
                step_id,
                status="failed",
                observation_ids=[observation_id],
                error_code="invalid_upstream_identity",
                elapsed_ms=elapsed,
            )
            return with_state(
                state,
                stop_reason=StopReason.INVALID_UPSTREAM_IDENTITY.value,
                errors=[
                    *state.get("errors", []),
                    {
                        "node": "verify_seed",
                        "code": "invalid_upstream_identity",
                        "message": (
                            "upstream identity must match exactly one graph entity "
                            f"with native_id={seed.native_id!r} and "
                            f"entity_type={seed.entity_type!r}; matched {len(matches)}"
                        ),
                    },
                ],
            )
        entity = matches[0].entity
        errors = list(state.get("errors", []))
        if entity.name != seed.canonical_name:
            errors.append(
                {
                    "node": "verify_seed",
                    "code": "upstream_name_mismatch",
                    "message": (
                        f"upstream name {seed.canonical_name!r} differs from graph "
                        f"name {entity.name!r}; the graph identity wins"
                    ),
                }
            )
        recorder.finish(
            state,
            "verify_seed",
            step_id,
            observation_ids=[observation_id],
            elapsed_ms=elapsed,
        )
        return with_state(
            state,
            seed_ref=seed.model_dump(mode="json"),
            seed_entity=entity.model_dump(mode="json"),
            errors=errors,
        )

    # ---------------- collect_neighbors ----------------
    def collect_neighbors(state: EntityGenerationState) -> EntityGenerationState:
        budget = context.budget
        assert budget is not None
        budget.spend_step()
        entity = GraphEntity.model_validate(state["seed_entity"])
        step_id = recorder.begin(
            state, "collect_neighbors", {"entity": entity.canonical_id, "hop": 1}
        )
        started = time.monotonic()
        outgoing_budget, incoming_budget = budget.split_edge_budget()
        collected: list[dict[str, Any]] = []
        observation_ids: list[str] = []
        failures: list[str] = []
        # Each direction is queried separately so it keeps its true direction
        # and its own share of the edge budget.
        for direction, share in (("outgoing", outgoing_budget), ("incoming", incoming_budget)):
            if share <= 0:
                continue
            query = NeighborQuery(entity=entity, direction=direction, limit=share)
            attempt_started = time.monotonic()
            operation_key = f"{state['episode_id']}|collect_neighbors|{direction}|1"
            try:
                result = context.provider.neighbors(query)
            except KnowledgeGraphError as exc:
                budget.spend_tool_call()
                failures.append(error_code_of(exc))
                observation_ids.append(
                    observe(
                        state,
                        node="collect_neighbors",
                        operation_key=operation_key,
                        request_digest=digest_of(query.model_dump(mode="json")),
                        row_count=0,
                        elapsed_ms=int((time.monotonic() - attempt_started) * 1000),
                        error_code=error_code_of(exc),
                    )
                )
                continue
            budget.spend_tool_call()
            # edges_observed counts raw rows before deduplication.
            raw_rows = int(result.trace.rows_examined or 0)
            budget.spend_edges(raw_rows)
            observation_ids.append(
                observe(
                    state,
                    node="collect_neighbors",
                    operation_key=operation_key,
                    request_digest=digest_of(query.model_dump(mode="json")),
                    row_count=raw_rows,
                    elapsed_ms=int((time.monotonic() - attempt_started) * 1000),
                )
            )
            for edge in result.edges:
                collected.append(
                    kg_evidence_from_edge(
                        edge,
                        direction=direction,
                        tool_name=context.tool_name,
                        snapshot=result.snapshot,
                        query_trace=result.trace,
                    ).model_dump(mode="json")
                )
        elapsed = int((time.monotonic() - started) * 1000)
        if failures and not collected:
            recorder.finish(
                state,
                "collect_neighbors",
                step_id,
                status="failed",
                observation_ids=observation_ids,
                error_code=failures[0],
                retryable=True,
                elapsed_ms=elapsed,
            )
            return with_state(
                state,
                stop_reason=StopReason.PROVIDER_ERROR.value,
                errors=[
                    *state.get("errors", []),
                    {
                        "node": "collect_neighbors",
                        "code": failures[0],
                        "message": "neighbour query failed for every direction",
                    },
                ],
            )
        recorder.finish(
            state,
            "collect_neighbors",
            step_id,
            observation_ids=observation_ids,
            elapsed_ms=elapsed,
        )
        exhausted = budget.exhausted_reason()
        stop_reason = None
        if not collected:
            stop_reason = StopReason.NO_EVIDENCE.value
        elif exhausted:
            stop_reason = exhausted.value
        return with_state(
            state,
            edges=collected,
            tool_calls=budget.tool_calls,
            edges_observed=budget.edges_observed,
            stop_reason=stop_reason,
        )

    # ---------------- agent_explore ----------------
    def agent_explore(state: EntityGenerationState) -> EntityGenerationState:
        """Let the structured model choose bounded KG queries turn by turn."""
        budget = context.budget
        assert budget is not None
        budget.spend_step()
        seed = GraphEntity.model_validate(state["seed_entity"])
        generation_spec = request.resolved_generation_spec()
        safety = request.resolved_safety_budget()
        allow_literature = (
            request.generation_spec is not None
            and "literature"
            in {item.value for item in generation_spec.allowed_sources}
            and generation_spec.evidence_constraints.literature_policy.value
            != "forbidden"
            and (
                context.literature_provider is not None
                or context.evidence_enricher is not None
            )
        )
        step_id = recorder.begin(
            state,
            "agent_explore",
            {"entity": seed.canonical_id, "max_hops": request.max_hops},
        )
        started = time.monotonic()

        class CapturingKG:
            def __init__(self) -> None:
                self.entities: dict[str, GraphEntity] = {seed.canonical_id: seed}
                self.evidence: dict[str, KGEvidence] = {}
                self.needs: dict[str, Any] = {}
                self.satisfied_need_ids: set[str] = set()

            def neighbors(self, sealed_request: dict[str, Any]) -> tuple[list[Any], str]:
                operation = sealed_request.get("operation")
                if operation not in {"query_neighbors", "expand_entity"}:
                    raise ToolCallError(
                        "kg_capability_unavailable",
                        f"formal generation does not support {operation!r} yet",
                    )
                subject_id = str(sealed_request["subject_entity_id"])
                try:
                    subject = self.entities[subject_id]
                except KeyError as exc:
                    raise ToolCallError(
                        "unknown_entity",
                        f"entity {subject_id!r} is not available to the KG adapter",
                    ) from exc
                if budget.remaining_tool_calls <= 0 or budget.remaining_edge_budget <= 0:
                    raise ToolCallError("budget_exhausted", "generation budget is exhausted")
                limit = min(
                    int(sealed_request.get("max_results", 50)),
                    budget.remaining_edge_budget,
                )
                query = NeighborQuery(
                    entity=subject,
                    direction=sealed_request.get("direction", "both"),
                    relations=list(sealed_request.get("relations", [])),
                    target_types=list(sealed_request.get("target_types", [])),
                    limit=limit,
                    cursor=sealed_request.get("cursor"),
                )
                try:
                    result = context.provider.neighbors(query)
                finally:
                    # Failed provider calls still consume a tool attempt.
                    budget.spend_tool_call()
                budget.spend_edges(int(result.trace.rows_examined or 0))
                observed_edges: list[Any] = []
                for edge in result.edges:
                    self.entities[edge.head.canonical_id] = edge.head
                    self.entities[edge.tail.canonical_id] = edge.tail
                    direction = (
                        "outgoing"
                        if edge.head.canonical_id == subject_id
                        else "incoming"
                    )
                    evidence = kg_evidence_from_edge(
                        edge,
                        direction=direction,
                        tool_name=context.tool_name,
                        snapshot=result.snapshot,
                        query_trace=result.trace,
                    )
                    if evidence.evidence_id in self.evidence:
                        # A broader query may already have persisted this edge.
                        # Keep the first append-only record instead of treating
                        # a later, narrower lookup as new evidence.
                        continue
                    self.evidence[evidence.evidence_id] = evidence
                    # Persist before the observation can cite this id. The
                    # later bundle is a curated view over these raw records.
                    save_artifact(
                        context.repository,
                        "kg_evidence",
                        evidence.evidence_id,
                        evidence.evidence_id,
                        evidence,
                    )
                    if allow_literature:
                        try:
                            need = build_need_for_edge(
                                evidence,
                                run_id=state["episode_id"],
                                episode_id=state["episode_id"],
                            )
                        except Exception:
                            need = None
                        if need is not None:
                            self.needs[need.need_id] = need
                    observed_edges.append(
                        edge.model_copy(update={"edge_id": evidence.evidence_id})
                    )
                return observed_edges, result.trace.query_id

        kg = CapturingKG()
        ctd_index = getattr(context.evidence_enricher, "ctd_index", None)

        ctd_executor = (
            CtdEvidenceExecutor(
                index=ctd_index,
                on_attempt=budget.spend_tool_call,
                on_record=lambda artifact_id, record: save_artifact(
                    context.repository,
                    "ctd_evidence",
                    artifact_id,
                    record.content_hash,
                    record,
                ),
            )
            if ctd_index is not None
            else None
        )

        literature_evidence: dict[str, LiteratureEvidence] = {}
        literature_executor = None

        class CapturingLiterature:
            def search(self, sealed_request: dict[str, Any]):
                nonlocal literature_executor
                if literature_executor is not None:
                    try:
                        evidence_ids, trace_id, snapshot_id = (
                            literature_executor.search(sealed_request)
                        )
                    finally:
                        budget.spend_tool_call()
                elif context.evidence_enricher is not None:
                    need_ids = [
                        str(item)
                        for item in sealed_request.get("need_ids", [])
                    ]
                    if not need_ids or need_ids[0] not in kg.needs:
                        raise ToolCallError(
                            "need_not_found",
                            "the sealed PubMed action has no open evidence need",
                        )
                    need = kg.needs[need_ids[0]]
                    edge = kg.evidence.get(need.kg_evidence_id or "")
                    if edge is None:
                        raise ToolCallError(
                            "kg_evidence_not_found",
                            "the PubMed need has no persisted KG evidence",
                        )
                    answer_entity_id = (
                        edge.tail.canonical_id
                        if edge.head.canonical_id == seed.canonical_id
                        else edge.head.canonical_id
                    ) or ""
                    try:
                        result = context.evidence_enricher.enrich(
                            edge,
                            seed_entity_id=seed.canonical_id,
                            answer_entity_id=answer_entity_id,
                            retrieval_query=str(
                                sealed_request.get("query") or ""
                            )
                            or None,
                        )
                    finally:
                        budget.spend_tool_call()
                    executed_query = str(
                        sealed_request.get("query") or ""
                    )
                    enrichment_id = literature_enrichment_id(
                        state["episode_id"], edge.evidence_id, executed_query
                    )
                    save_artifact(
                        context.repository,
                        "evidence_enrichment",
                        enrichment_id,
                        enrichment_id,
                        result,
                    )
                    if not result.literature_evidence:
                        raise ToolCallError(
                            "pubmed_evidence_insufficient",
                            "; ".join(result.reason_codes)
                            or "PubMed returned no verified supporting passage",
                        )
                    evidence_ids = []
                    for evidence in result.literature_evidence:
                        save_artifact(
                            context.repository,
                            "literature_evidence",
                            evidence.evidence_id,
                            evidence.evidence_id,
                            evidence,
                        )
                        literature_evidence[evidence.evidence_id] = evidence
                        evidence_ids.append(evidence.evidence_id)
                    kg.satisfied_need_ids.add(need.need_id)
                    trace_id = f"pubmed.{enrichment_id}"
                    snapshot_id = (
                        result.pubmed_corpus_manifest.corpus_digest
                        if result.pubmed_corpus_manifest is not None
                        else None
                    )
                else:
                    raise ToolCallError(
                        "literature_unavailable",
                        "no formal literature or PubMed provider is configured",
                    )
                for evidence_id in evidence_ids:
                    if evidence_id not in literature_evidence:
                        literature_evidence[evidence_id] = (
                            context.repository.load(
                                "literature_evidence",
                                evidence_id,
                                evidence_id,
                                LiteratureEvidence,
                            )
                        )
                return evidence_ids, trace_id, snapshot_id

        if allow_literature and context.literature_provider is not None:
            literature_executor = LiteratureEvidenceExecutor(
                provider=context.literature_provider,
                repository_root=str(context.repository.root),
                episode_id=state["episode_id"],
                run_id=state["episode_id"],
                knowledge_base_ids=context.literature_knowledge_base_ids,
                needs=kg.needs,
                evidence_by_id=kg.evidence,
                provider_id=context.literature_provider_id,
                on_satisfied=lambda need_id: kg.satisfied_need_ids.add(need_id),
            )
        planner = StructuredModelPlanner(context.model)
        available_tools = ["kg_neighbors", "none"]
        available_actions = [
            "query_neighbors",
            "expand_entity",
            "finish",
        ]
        if ctd_index is not None:
            available_tools.append("ctd_lookup")
            available_actions.insert(2, "query_ctd")
        if allow_literature:
            available_tools.append("literature_search")
            available_actions.insert(2, "retrieve_literature")
        evidence_constraints = generation_spec.evidence_constraints
        entity_planner = (
            StructuredEntityQuestionPlanner(context.model)
            if context.entity_question_loop and context.model is not None
            else None
        )

        def persist_validated_task(validated: ValidatedTask) -> None:
            save_artifact(
                context.repository,
                "validated_tasks",
                validated.task.task_id,
                validated.validation_report_id,
                validated,
            )

        def persist_validation_report(report: Any) -> None:
            save_artifact(
                context.repository,
                "validation_reports",
                report.report_id,
                report.report_id,
                report,
            )

        def persist_agent_decision(turn: int, decision: Any) -> None:
            context.episodes.save_agent_decision(
                AgentDecisionRecord(
                    record_id=stable_id(
                        state["episode_id"], "agent-decision", str(turn)
                    ),
                    episode_id=state["episode_id"],
                    turn=turn,
                    decision=decision,
                )
            )

        def persist_validation_feedback(turn: int, feedback: Any) -> None:
            context.episodes.save_validation_feedback(
                ValidationFeedbackRecord(
                    record_id=stable_id(
                        state["episode_id"],
                        "validation-feedback",
                        f"{turn}|{feedback.candidate_id}",
                    ),
                    episode_id=state["episode_id"],
                    turn=turn,
                    feedback=feedback,
                )
            )

        candidate_assessor = (
            GraphTaskCandidateAssessor(
                context.provider,
                on_validated=persist_validated_task,
                on_validation_report=persist_validation_report,
            )
            if entity_planner is not None
            else None
        )
        react = build_react_graph(
            ReactContext(
                planner=planner,
                entity_planner=entity_planner,
                executor=Executor(
                    kg=kg,
                    ctd=ctd_executor,
                    literature=(
                        CapturingLiterature() if allow_literature else None
                    ),
                ),
                policy=RunPolicy(
                    max_turns=safety.max_model_attempts,
                    max_tool_calls=safety.max_tool_calls,
                    max_results_per_query=min(50, safety.max_edges_observed),
                    max_hops=request.max_hops,
                    remaining_edge_budget=budget.remaining_edge_budget,
                    available_tool_names=tuple(available_tools),
                    allowed_action_types=tuple(available_actions),
                    remaining_tool_calls=budget.remaining_tool_calls,
                    min_total_evidence=(
                        evidence_constraints.min_total_evidence
                        if request.generation_spec is not None
                        else 1
                    ),
                    min_kg_evidence=(
                        evidence_constraints.min_kg_evidence
                        if request.generation_spec is not None
                        else 0
                    ),
                    min_literature_evidence=(
                        evidence_constraints.min_literature_evidence
                        if request.generation_spec is not None
                        else 0
                    ),
                ),
                run_id=state["episode_id"],
                episode_id=state["episode_id"],
                seed_entity_id=seed.canonical_id,
                seed_entity_name=seed.name,
                seed_entity_type=seed.entity_type,
                skill_registry=(
                    build_default_skill_registry()
                    if entity_planner is not None
                    else None
                ),
                available_tools=tuple(available_tools),
                available_action_types=tuple(available_actions),
                generation_spec=(
                    generation_spec if request.generation_spec is not None else None
                ),
                open_needs_provider=(
                    lambda: [
                        need.model_dump(mode="json")
                        for need_id, need in kg.needs.items()
                        if need_id not in kg.satisfied_need_ids
                    ]
                    if allow_literature
                    else []
                ),
                evidence_summary_provider=(
                    lambda: _agent_evidence_summary(
                        list(kg.evidence.values()), seed.canonical_id
                    )
                ),
                remaining_edge_budget_provider=(
                    lambda: budget.remaining_edge_budget
                ),
                on_observation=context.episodes.save_observation,
                on_planner_step=TrainingRepository(
                    str(context.repository.root)
                ).save_step,
                on_agent_decision=persist_agent_decision,
                on_validation_feedback=persist_validation_feedback,
                on_skill_invocation=context.episodes.save_skill_invocation,
                on_skill_outcome=context.episodes.save_skill_outcome,
                model_config_id=context.model_config_id,
                max_turns=safety.max_model_attempts,
                max_questions=request.question_count,
                candidate_assessor=candidate_assessor,
                validation_snapshot_id=(
                    context.provider.snapshot().data_snapshot_id
                    if entity_planner is not None
                    else None
                ),
            )
        )
        final = react.invoke(
            react.initial_state(state["request"]),
            config={"recursion_limit": safety.max_model_attempts * 6 + 10},
        )
        # The outer graph accounts for the node itself above. Each additional
        # model turn is also an agent step for the shared generation budget.
        for _ in range(max(int(final.get("react_turn", 0)) - 1, 0)):
            budget.spend_step()
        evidence = [item.model_dump(mode="json") for item in kg.evidence.values()]
        literature_payload = [
            item.model_dump(mode="json") for item in literature_evidence.values()
        ]
        exploration_stop = final.get("stop_reason") or StopReason.NO_EVIDENCE.value
        observation_ids = [
            str(item["observation_id"])
            for item in final.get("react_observations", [])
        ]
        elapsed = int((time.monotonic() - started) * 1000)
        recorder.finish(
            state,
            "agent_explore",
            step_id,
            status="completed" if evidence else "failed",
            observation_ids=observation_ids,
            error_code=None if evidence else final.get("stop_reason"),
            elapsed_ms=elapsed,
        )
        updates = {
            key: final[key]
            for key in (
                "react_turn",
                "react_policy",
                "react_observations",
                "react_known_entity_ids",
                "react_action_hashes",
                "react_sealed_actions",
                "react_rejections",
                "react_planner_steps",
                "react_stagnation_count",
                "react_stop_reason",
                "react_candidates",
                "react_candidate_statuses",
                "react_validation_feedback",
                "react_agent_decisions",
                "react_open_gaps",
                "react_gap_candidate_ids",
                "react_published_task_ids",
                "react_entity_loop_active",
                "react_skill_invocations",
                "react_skill_outcomes",
            )
            if key in final
        }
        updates["react_stop_reason"] = exploration_stop
        return with_state(
            state,
            **updates,
            edges=evidence,
            literature_evidence=literature_payload,
            tool_calls=budget.tool_calls,
            edges_observed=budget.edges_observed,
            stop_reason=exploration_stop,
            errors=(
                state.get("errors", [])
                if evidence
                else [
                    *state.get("errors", []),
                    {
                        "node": "agent_explore",
                        "code": final.get("stop_reason") or "no_evidence",
                        "message": (
                            final.get("_proposal_error")
                            or "model-driven exploration produced no evidence"
                        ),
                    },
                ]
            ),
        )

    # ---------------- curate_evidence ----------------
    def curate_evidence_node(state: EntityGenerationState) -> EntityGenerationState:
        budget = context.budget
        assert budget is not None
        budget.spend_step()
        step_id = recorder.begin(
            state, "curate_evidence", {"hop": 1, "rows": len(state.get("edges", []))}
        )
        started = time.monotonic()
        seed_canonical_id = str(state["seed_entity"].get("canonical_id"))
        raw = [KGEvidence.model_validate(item) for item in state.get("edges", [])]
        report = curate_evidence(raw, seed_canonical_id=seed_canonical_id, policy=policy)
        evidence = (
            list({item.evidence_id: item for item in raw}.values())
            if state.get("react_entity_loop_active", False)
            else [item.evidence for item in report.curated]
        )
        literature = [
            LiteratureEvidence.model_validate(item)
            for item in state.get("literature_evidence", [])
        ]
        bundle = evidence_bundle_v1(
            context,
            state["seed_entity"],
            evidence,
            literature_evidence=literature,
        )
        # FR-017: the bundle is saved before the node returns and before any
        # checkpoint is written.
        save_artifact(
            context.repository, "bundles", bundle.bundle_id, bundle.bundle_version, bundle
        )
        recorder.finish(
            state,
            "curate_evidence",
            step_id,
            output_artifact_ids=[bundle.bundle_id],
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        return with_state(
            state,
            bundle_id=bundle.bundle_id,
            bundle_version=bundle.bundle_version,
            evidence_ids=[item.evidence_id for item in evidence],
            literature_evidence=[
                item.model_dump(mode="json") for item in literature
            ],
            stop_reason=(
                StopReason.NO_EVIDENCE.value
                if not evidence and not literature
                else None
            ),
        )

    # ---------------- realize_validated_tasks ----------------
    def realize_validated_tasks(
        state: EntityGenerationState,
    ) -> EntityGenerationState:
        """Write only independently validated TaskCandidate objects.

        This is the v2 publication path.  It deliberately bypasses the old
        blueprint writer so a candidate validated by the ReAct loop cannot be
        replaced by a different, heuristically generated question downstream.
        """

        budget = context.budget
        assert budget is not None
        budget.spend_step()
        task_ids = list(state.get("react_published_task_ids", []))
        step_id = recorder.begin(
            state,
            "realize_validated_tasks",
            {"validated_task_ids": task_ids},
        )
        started = time.monotonic()
        bundle = context.repository.load_bundle(
            state["bundle_id"], state["bundle_version"]
        )
        request_contract = EntityGenerationRequest.model_validate(
            state["request"]
        )
        writer = TaskQuestionWriter()
        semantic_verifier = TaskTextSemanticVerifier()
        accepted: list[str] = []
        rejected: list[str] = []
        rejection_ids: list[str] = []

        for validated_task_id in task_ids:
            validated = context.repository.load_latest(
                "validated_tasks", validated_task_id, ValidatedTask
            )
            question_id = stable_id(
                state["episode_id"], "question", validated.task.task_id
            )
            try:
                variant = writer.write(validated, qid=question_id)
                semantic = semantic_verifier.verify(variant, validated)
            except Exception as exc:
                semantic = None
                reason_codes = ["validated_task_realization_failed"]
                message = f"{type(exc).__name__}: {exc}"
            else:
                reason_codes = list(semantic.reason_codes)
                message = "; ".join(reason_codes)

            if semantic is None or not semantic.passed:
                rejection = RejectionRecord(
                    rejection_id=stable_id(
                        state["episode_id"],
                        "rejection",
                        f"{validated.task.task_id}|task_text_semantics",
                    ),
                    episode_id=state["episode_id"],
                    draft_id=question_id,
                    stage="task_text_semantic_verification",
                    reason_codes=reason_codes or ["semantic_verification_failed"],
                    message=message or "question text changed task semantics",
                    blocking=True,
                    created_at=utc_now(),
                )
                context.episodes.save_rejection(rejection)
                rejection_ids.append(rejection.rejection_id)
                rejected.append(question_id)
                continue

            save_artifact(
                context.repository,
                "task_text_semantic_reports",
                semantic.variant_id,
                semantic.verifier_version,
                semantic,
            )
            task = validated.task
            answer_ids = list(validated.answer_contract.canonical_answer_ids)
            aliases = list(validated.answer_contract.accepted_aliases)
            short_answer = aliases[0] if aliases else answer_ids[0]
            relations = [item.relation for item in task.query.steps]
            directions = [item.direction.value for item in task.query.steps]
            constraint_entity_ids = [
                item.terminal_entity_id for item in task.query.constraints
            ]
            question = QuestionSpec(
                question_id=question_id,
                logical_question_id=stable_id(
                    request_contract.workspace,
                    "logical",
                    task.query.dependency_digest,
                ),
                question_version=f"{question_id}.draft",
                bundle_ids=[bundle.bundle_id],
                generation_mode=GenerationMode.AUTOMATIC,
                canonical_question=variant.question_text,
                question_variants=[
                    QuestionVariant(
                        variant_id=variant.variant_id,
                        text=variant.question_text,
                        language=variant.language,
                        persona="system",
                        style="validated-task",
                        generation_source="agent",
                        semantic_check_status="passed",
                    )
                ],
                taxonomy=QuestionTaxonomy(
                    domain_intent=task.question_type,
                    answer_form=AnswerForm.ENTITY,
                    reasoning_pattern=["constrained_lookup", "complete_world"],
                    hop_complexity=(
                        len(task.query.steps)
                        + sum(
                            len(item.steps) for item in task.query.constraints
                        )
                    ),
                    difficulty_tags=["world_validated"],
                ),
                query_intent={
                    "route": QuestionRoute.KG.value,
                    "question_type": task.question_type,
                    "task_id": task.task_id,
                    "candidate_id": task.candidate_id,
                    "query_id": task.query.query_id,
                    "query_digest": task.query.dependency_digest,
                    "validation_environment_id": (
                        task.validation_environment.environment_id
                    ),
                    "validation_report_id": validated.validation_report_id,
                    "provider_id": request_contract.seed.provider_id,
                    "seed_entity_id": task.seed_entity_id,
                    "seed_entity_type": request_contract.seed.entity_type,
                    "relations": relations,
                    "directions": directions,
                    "answer_entity_type": task.query.answer_entity_type,
                    "intermediate_entity_ids": constraint_entity_ids,
                    "answer_candidate_count": len(answer_ids),
                },
                answerability=Answerability.ANSWERABLE,
                gold_answer=GoldAnswer(
                    answer_mode=AnswerForm.ENTITY,
                    values=[
                        {
                            "provider_id": request_contract.seed.provider_id,
                            "canonical_id": answer_id,
                            "entity_type": task.query.answer_entity_type,
                            "canonical_name": (
                                aliases[index]
                                if index < len(aliases)
                                else answer_id
                            ),
                        }
                        for index, answer_id in enumerate(answer_ids)
                    ],
                    short_answer=short_answer,
                    acceptable_variants=aliases,
                    confidence=1.0,
                ),
                evidence_requirements=EvidenceRequirements(
                    required_claim_ids=list(validated.gold_claim_ids),
                    required_kg_evidence_ids=list(
                        task.candidate_evidence_ids
                    ),
                    sufficiency_rule_version="validated-task-v2",
                ),
                expected_route=QuestionRoute.KG,
                verified_route=QuestionRoute.KG,
                validation_level=ValidationLevel.DRAFT,
                split_group_id=stable_id(
                    request_contract.workspace,
                    "split",
                    task.query.dependency_digest,
                ),
                review_group_key=digest_of(
                    {
                        "seed": task.seed_entity_id,
                        "relations": relations,
                        "directions": directions,
                        "constraints": constraint_entity_ids,
                        "answer_entity_type": task.query.answer_entity_type,
                    }
                ),
                review_group_version=REVIEW_GROUP_VERSION,
                generation_episode_id=state["episode_id"],
                created_at=utc_now(),
                changes=[
                    ChangeRecord(
                        change_id=new_id("change"),
                        actor_type=ActorType.AGENT,
                        operation="validated_task_realization",
                        summary=(
                            "written from an independently validated task"
                        ),
                        created_at=utc_now(),
                        parent_version=None,
                        new_version=f"{question_id}.draft",
                    )
                ],
            )
            save_artifact(
                context.repository,
                "questions",
                question.question_id,
                question.question_version,
                question,
            )
            machine_version = (
                f"{question_id}.mv-{VERIFICATION_POLICY_VERSION}"
            )
            validation_checks = [
                ValidationCheck(
                    check_id=stable_id(
                        question_id,
                        "check",
                        validated.validation_report_id,
                    ),
                    check_type="complete_world_task_validation",
                    status=ValidationStatus.PASSED,
                    severity=ValidationSeverity.BLOCKING,
                    message=(
                        "candidate answer, cardinality, constraints and "
                        "competitors passed complete-world validation"
                    ),
                    validator_version="task-validator-v2",
                    artifact_ids=[validated.validation_report_id],
                    question_id=question_id,
                    evaluated_question_version=question.question_version,
                    input_digest=task.query.dependency_digest,
                    evaluated_at=validated.validated_at,
                ),
                ValidationCheck(
                    check_id=stable_id(
                        question_id,
                        "check",
                        semantic.variant_id,
                    ),
                    check_type="task_text_semantic_equivalence",
                    status=ValidationStatus.PASSED,
                    severity=ValidationSeverity.BLOCKING,
                    message="question wording preserves the validated task semantics",
                    validator_version=semantic.verifier_version,
                    artifact_ids=[semantic.variant_id],
                    question_id=question_id,
                    evaluated_question_version=question.question_version,
                    input_digest=(
                        semantic.reconstructed_query_digest
                        or task.query.dependency_digest
                    ),
                    evaluated_at=semantic.created_at,
                ),
            ]
            machine_validated = question.model_copy(
                update={
                    "question_version": machine_version,
                    "validation_level": ValidationLevel.MACHINE_VALIDATED,
                    "validation_checks": validation_checks,
                    "changes": [
                        *question.changes,
                        ChangeRecord(
                            change_id=new_id("change"),
                            actor_type=ActorType.AGENT,
                            operation="validated_task_publication",
                            summary=(
                                "complete-world task and text semantics passed"
                            ),
                            created_at=utc_now(),
                            parent_version=question.question_version,
                            new_version=machine_version,
                        ),
                    ],
                }
            )
            save_artifact(
                context.repository,
                "questions",
                machine_validated.question_id,
                machine_validated.question_version,
                machine_validated,
            )
            if machine_validated.validation_level is ValidationLevel.REJECTED:
                rejected.append(question_id)
            else:
                accepted.append(question_id)

        recorder.finish(
            state,
            "realize_validated_tasks",
            step_id,
            output_artifact_ids=accepted,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        return with_state(
            state,
            accepted_question_ids=accepted,
            rejected_question_ids=rejected,
            rejection_record_ids=[
                *state.get("rejection_record_ids", []),
                *rejection_ids,
            ],
            stop_reason=validated_task_realization_stop_reason(
                state,
                accepted_question_ids=accepted,
            ),
        )

    # ---------------- build_claims ----------------
    def build_claims(state: EntityGenerationState) -> EntityGenerationState:
        budget = context.budget
        assert budget is not None
        budget.spend_step()
        step_id = recorder.begin(
            state, "build_claims", {"evidence": state.get("evidence_ids", [])}
        )
        started = time.monotonic()
        # Reload v1 from the repository rather than trusting graph state.
        bundle_v1 = context.repository.load_bundle(
            state["bundle_id"], state["bundle_version"]
        )
        seed_canonical_id = str(state["seed_entity"].get("canonical_id"))
        report = curate_evidence(
            list(bundle_v1.kg_evidence), seed_canonical_id=seed_canonical_id, policy=policy
        )
        enrichment_results: list[Any] = []
        enrichment_ids: list[str] = []
        enrichment_literature: list[Any] = []
        if context.evidence_enricher is not None:
            for item in report.curated[: max(request.question_count * 2, 4)]:
                edge = item.evidence
                answer_entity_id = (
                    edge.tail.canonical_id or ""
                    if item.answer_endpoint == "tail"
                    else edge.head.canonical_id or ""
                )
                result = context.evidence_enricher.enrich(
                    edge,
                    seed_entity_id=seed_canonical_id,
                    answer_entity_id=answer_entity_id,
                )
                enrichment_results.append(result)
                enrichment_id = stable_id(
                    state["episode_id"], "enrichment", edge.evidence_id
                )
                save_artifact(
                    context.repository,
                    "evidence_enrichment",
                    enrichment_id,
                    enrichment_id,
                    result,
                )
                enrichment_ids.append(enrichment_id)
                for literature in result.literature_evidence:
                    save_artifact(
                        context.repository,
                        "literature_evidence",
                        literature.evidence_id,
                        literature.evidence_id,
                        literature,
                    )
                    enrichment_literature.append(literature)
                if result.status.value != "supported":
                    rejection = RejectionRecord(
                        rejection_id=stable_id(
                            state["episode_id"],
                            "rejection",
                            f"{edge.evidence_id}|enrichment",
                        ),
                        episode_id=state["episode_id"],
                        draft_id=None,
                        stage="enrichment",
                        reason_codes=list(result.reason_codes)
                        or [result.status.value],
                        message=(
                            f"evidence enrichment rejected edge "
                            f"{edge.evidence_id}: {result.status.value}"
                        ),
                        blocking=True,
                        created_at=utc_now(),
                    )
                    context.episodes.save_rejection(rejection)
        claims = context.claim_builder.build_from_curated(
            report.curated, len(report.curated)
        )
        bundle_v2 = bundle_v1.model_copy(
            update={
                "bundle_version": f"{bundle_v1.bundle_id}.v2",
                "claims": claims,
                "literature_evidence": [
                    *bundle_v1.literature_evidence,
                    *enrichment_literature,
                ],
                "created_at": utc_now(),
            }
        )
        save_artifact(
            context.repository, "bundles", bundle_v2.bundle_id, bundle_v2.bundle_version, bundle_v2
        )
        if context.evidence_enricher is not None:
            blueprints = context.planner.plan(
                report.curated,
                enrichment_results,
                seed_canonical_id=seed_canonical_id,
                question_count=request.question_count,
            )
        else:
            blueprints = context.planner.plan(
                report.curated,
                seed_canonical_id=seed_canonical_id,
                question_count=request.question_count,
            )
        recorder.finish(
            state,
            "build_claims",
            step_id,
            output_artifact_ids=[bundle_v2.bundle_id],
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        return with_state(
            state,
            bundle_version=bundle_v2.bundle_version,
            claim_ids=[item.claim_id for item in claims],
            blueprints=[item.model_dump(mode="json") for item in blueprints],
            enrichment_ids=enrichment_ids,
            enriched_claims=[
                result.claim.model_dump(mode="json")
                for result in enrichment_results
                if result.claim is not None
            ],
            stop_reason=StopReason.NO_VALID_CLAIMS.value if not claims else None,
        )

    # ---------------- plan_questions ----------------
    def plan_questions(state: EntityGenerationState) -> EntityGenerationState:
        budget = context.budget
        assert budget is not None
        budget.spend_step()
        blueprints = [
            QuestionBlueprint.model_validate(item) for item in state.get("blueprints", [])
        ]
        step_id = recorder.begin(
            state,
            "plan_questions",
            {"blueprints": [item.blueprint_id for item in blueprints]},
        )
        recorder.finish(state, "plan_questions", step_id)
        return with_state(
            state, question_blueprints={item.blueprint_id: item.model_dump(mode="json") for item in blueprints}
        )

    # ---------------- write_questions ----------------
    def write_questions(state: EntityGenerationState) -> EntityGenerationState:
        budget = context.budget
        assert budget is not None
        budget.spend_step()
        blueprints = [
            QuestionBlueprint.model_validate(item)
            for item in state.get("question_blueprints", {}).values()
        ]
        step_id = recorder.begin(
            state, "write_questions", {"blueprints": len(blueprints)}
        )
        started = time.monotonic()
        bundle = context.repository.load_bundle(
            state["bundle_id"], state["bundle_version"]
        )
        drafts = context.writer.write(
            blueprints,
            evidence=list(bundle.kg_evidence),
            entities=entity_index(bundle),
            episode_id=state["episode_id"],
            natives=native_index(bundle),
            literature_evidence=list(bundle.literature_evidence),
            generation_spec=(
                request.generation_spec
                if request.generation_spec is not None
                else None
            ),
        )
        for rejected in getattr(context.writer, "last_rejections", []):
            record = RejectionRecord(
                rejection_id=stable_id(
                    state["episode_id"],
                    "rejection",
                    f"{rejected['draft_id']}|writer_contract",
                ),
                episode_id=state["episode_id"],
                draft_id=str(rejected["draft_id"]),
                stage="schema",
                reason_codes=["writer_contract_violation"],
                message=(
                    f"{rejected.get('skill_id')}:{rejected.get('style_profile')}: "
                    f"{rejected.get('message')}"
                ),
                blocking=True,
                created_at=utc_now(),
            )
            context.episodes.save_rejection(record)
        if context.style_judge is not None:
            by_blueprint = {item.blueprint_id: item for item in blueprints}
            retained: list[Any] = []
            minimum_score = (
                request.generation_spec.minimum_style_score
                if request.generation_spec is not None
                else 0
            )
            for draft in drafts:
                blueprint = by_blueprint.get(draft.blueprint_id)
                if blueprint is None:
                    continue
                judgement = context.style_judge.assess(draft, blueprint)
                blocking = judgement.blocking or judgement.score < minimum_score
                if blocking:
                    record = RejectionRecord(
                        rejection_id=stable_id(
                            state["episode_id"],
                            "rejection",
                            f"{draft.draft_id}|style",
                        ),
                        episode_id=state["episode_id"],
                        draft_id=draft.draft_id,
                        stage="style",
                        reason_codes=judgement.reason_codes,
                        message=(
                            f"style score {judgement.score:.2f}; "
                            f"{judgement.feedback}"
                        ),
                        blocking=True,
                        created_at=utc_now(),
                    )
                    context.episodes.save_rejection(record)
                    continue
                retained.append(
                    draft.model_copy(update={"style_score": judgement.score})
                )
            drafts = retained
        recorder.finish(
            state, "write_questions", step_id, elapsed_ms=int((time.monotonic() - started) * 1000)
        )
        # Reaching this node after a verify pass is the one rewrite the policy
        # allows. Spending it here (rather than in the verifier) keeps the
        # ``after_verify_questions`` router able to route the first revision
        # back while the second one is hard rejected.
        revision_counts = dict(state.get("revision_counts") or {"used": 0, "pending": 0})
        if revision_counts.get("pending", 0) > 0:
            revision_counts["used"] = revision_counts.get("used", 0) + 1
            revision_counts["pending"] = 0
        return with_state(
            state,
            drafts={item.draft_id: item.model_dump(mode="json") for item in drafts},
            revision_counts=revision_counts,
            stop_reason=StopReason.EVIDENCE_EXHAUSTED.value if not drafts else None,
        )

    # ---------------- persist_draft_questions ----------------
    def persist_draft_questions(state: EntityGenerationState) -> EntityGenerationState:
        budget = context.budget
        assert budget is not None
        budget.spend_step()
        drafts = state.get("drafts", {})
        step_id = recorder.begin(
            state, "persist_draft_questions", {"drafts": sorted(drafts)}
        )
        started = time.monotonic()
        bundle = context.repository.load_bundle(
            state["bundle_id"], state["bundle_version"]
        )
        written: list[str] = []
        # Key blueprints by question id so the verifier can recover the exact
        # blueprint a persisted draft came from.
        blueprints_by_question: dict[str, dict[str, Any]] = {}
        for draft_id in sorted(drafts):
            spec = question_spec_from_draft(state, drafts[draft_id], bundle)
            save_artifact(
                context.repository,
                "questions",
                spec.question_id,
                spec.question_version,
                spec,
            )
            written.append(spec.question_id)
            blueprints_by_question[spec.question_id] = state["question_blueprints"][
                drafts[draft_id]["blueprint_id"]
            ]
        recorder.finish(
            state,
            "persist_draft_questions",
            step_id,
            output_artifact_ids=written,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        return with_state(
            state,
            draft_question_ids=written,
            blueprint_by_question_id=merge_blueprint_map(state, blueprints_by_question),
        )

    # ---------------- verify_questions ----------------
    def verify_questions(state: EntityGenerationState) -> EntityGenerationState:
        budget = context.budget
        assert budget is not None
        budget.spend_step()
        step_id = recorder.begin(
            state, "verify_questions", {"questions": state.get("draft_question_ids", [])}
        )
        started = time.monotonic()
        bundle = context.repository.load_bundle(
            state["bundle_id"], state["bundle_version"]
        )
        blueprints = {
            key: QuestionBlueprint.model_validate(value)
            for key, value in state.get("blueprint_by_question_id", {}).items()
        }
        accepted: list[str] = []
        rejected: list[str] = []
        revision_needed: list[str] = []
        rejection_ids: list[str] = []
        for question_id in state.get("draft_question_ids", []):
            spec = context.repository.load_question(question_id, f"{question_id}.draft")
            draft = draft_from_question(spec, state["episode_id"])
            blueprint = blueprints.get(question_id)
            if blueprint is None:
                continue
            if bundle.literature_evidence:
                result = context.verifier.verify_with_blueprint(
                    draft,
                    blueprint,
                    list(bundle.claims),
                    list(bundle.kg_evidence),
                    literature_evidence=list(bundle.literature_evidence),
                )
            else:
                result = context.verifier.verify_with_blueprint(
                    draft,
                    blueprint,
                    list(bundle.claims),
                    list(bundle.kg_evidence),
                )
            result = result.model_copy(update={"episode_id": state["episode_id"]})
            context.episodes.save_verification(result)
            if result.blocking:
                rejected.append(question_id)
                record = RejectionRecord(
                    rejection_id=stable_id(
                        state["episode_id"], "rejection", f"{draft.draft_id}|{result.verdict}"
                    ),
                    episode_id=state["episode_id"],
                    draft_id=draft.draft_id,
                    stage="verifier",
                    reason_codes=result.reason_codes,
                    message=result.message,
                    blocking=True,
                    created_at=utc_now(),
                )
                context.episodes.save_rejection(record)
                rejection_ids.append(record.rejection_id)
                continue
            if result.verdict == "needs_revision":
                revision_needed.append(question_id)
                record = RejectionRecord(
                    rejection_id=stable_id(
                        state["episode_id"], "rejection", f"{draft.draft_id}|revision"
                    ),
                    episode_id=state["episode_id"],
                    draft_id=draft.draft_id,
                    stage="verifier",
                    reason_codes=result.reason_codes,
                    message=result.message,
                    blocking=False,
                    created_at=utc_now(),
                )
                context.episodes.save_rejection(record)
                rejection_ids.append(record.rejection_id)
                continue
            # A non-blocking "rejected" is still a machine suggestion; a
            # structurally valid question still reaches the human queue.
            accepted.append(question_id)
        revisions = dict(state.get("revision_counts") or {})
        used = revisions.get("used", 0)
        revision_exhausted = used >= 1
        # A candidate that still needs a revision after the single allowed
        # rewrite is hard rejected: it must never reach the machine-validated
        # or human queue on the strength of a verdict that said "needs
        # revision".
        if revision_exhausted and revision_needed:
            for question_id in revision_needed:
                spec = context.repository.load_question(
                    question_id, f"{question_id}.draft"
                )
                draft = draft_from_question(spec, state["episode_id"])
                record = RejectionRecord(
                    rejection_id=stable_id(
                        state["episode_id"],
                        "rejection",
                        f"{draft.draft_id}|revision_exhausted",
                    ),
                    episode_id=state["episode_id"],
                    draft_id=draft.draft_id,
                    stage="verifier",
                    reason_codes=["revision_limit_exhausted"],
                    message=(
                        "candidate still needs a revision after the single allowed "
                        "rewrite; hard rejected"
                    ),
                    blocking=True,
                    created_at=utc_now(),
                )
                context.episodes.save_rejection(record)
                rejection_ids.append(record.rejection_id)
                rejected.append(question_id)
            revision_needed = []
            pending = 0
        else:
            pending = len(revision_needed)
        recorder.finish(
            state,
            "verify_questions",
            step_id,
            output_artifact_ids=sorted(set(accepted + rejected)),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        stop_reason = None
        if rejected and not accepted:
            stop_reason = StopReason.ALL_CANDIDATES_HARD_REJECTED.value
        return with_state(
            state,
            accepted_question_ids=sorted(set(accepted)),
            rejected_question_ids=sorted(set(rejected)),
            rejection_record_ids=[*state.get("rejection_record_ids", []), *rejection_ids],
            revision_counts={"used": used, "pending": pending},
            stop_reason=stop_reason,
        )

    # ---------------- machine_validate ----------------
    def machine_validate(state: EntityGenerationState) -> EntityGenerationState:
        budget = context.budget
        assert budget is not None
        budget.spend_step()
        step_id = recorder.begin(
            state, "machine_validate", {"questions": state.get("accepted_question_ids", [])}
        )
        started = time.monotonic()
        bundle = context.repository.load_bundle(
            state["bundle_id"], state["bundle_version"]
        )
        finalized: list[str] = []
        for question_id in state.get("accepted_question_ids", []):
            draft_spec = context.repository.load_question(question_id, f"{question_id}.draft")
            validated = apply_machine_validation(
                draft_spec, [bundle], actor_type=ActorType.AGENT
            )
            # The version id is derived from the parent so a replay can never
            # produce an endless chain of new versions.
            validated = validated.model_copy(
                update={
                    "question_version": f"{question_id}.mv-{VERIFICATION_POLICY_VERSION}"
                }
            )
            save_artifact(
                context.repository,
                "questions",
                validated.question_id,
                validated.question_version,
                validated,
            )
            finalized.append(validated.question_id)
        recorder.finish(
            state,
            "machine_validate",
            step_id,
            output_artifact_ids=finalized,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        exhausted = budget.exhausted_reason()
        if len(finalized) >= request.question_count:
            stop_reason = StopReason.TARGET_REACHED.value
        elif exhausted:
            stop_reason = exhausted.value
        else:
            stop_reason = StopReason.EVIDENCE_EXHAUSTED.value
        return with_state(
            state, accepted_question_ids=finalized, stop_reason=stop_reason
        )

    # ---------------- finalize ----------------
    def finalize_artifact_links(state: EntityGenerationState) -> EntityGenerationState:
        budget = context.budget
        assert budget is not None
        budget.spend_step()
        # This node only aggregates references; every artifact was saved by the
        # node that created it.
        step_id = recorder.begin(
            state, "finalize_artifact_links", {"questions": state.get("accepted_question_ids", [])}
        )
        recorder.finish(state, "finalize_artifact_links", step_id)
        return with_state(
            state,
            bundle_id=state.get("bundle_id"),
            bundle_version=state.get("bundle_version"),
        )

    def finalize(state: EntityGenerationState) -> EntityGenerationState:
        budget = context.budget
        assert budget is not None
        budget.spend_step()
        step_id = recorder.begin(state, "finalize", {"episode": state["episode_id"]})
        qa_ids, trajectory_id, targets_satisfied = persist_training_projection(
            context, state, final_state=EpisodeState.COMPLETED
        )
        final_state = (
            EpisodeState.COMPLETED if qa_ids else EpisodeState.PARTIAL
        )
        final_stop_reason = (
            state.get("stop_reason")
            if qa_ids
            else StopReason.ALL_CANDIDATES_HARD_REJECTED.value
        )
        projected_state = {
            **state,
            "qa_item_ids": qa_ids,
            "planner_trajectory_id": trajectory_id,
            "target_difficulties_satisfied": targets_satisfied,
            "stop_reason": final_stop_reason,
        }
        write_episode(context, projected_state, final_state)
        recorder.finish(state, "finalize", step_id)
        return with_state(
            projected_state,
            qa_item_ids=qa_ids,
            planner_trajectory_id=trajectory_id,
            target_difficulties_satisfied=targets_satisfied,
        )

    def finalize_partial(state: EntityGenerationState) -> EntityGenerationState:
        budget = context.budget
        assert budget is not None
        budget.spend_step()
        step_id = recorder.begin(
            state, "finalize_partial", {"episode": state["episode_id"]}
        )
        qa_ids, trajectory_id, targets_satisfied = persist_training_projection(
            context, state, final_state=EpisodeState.PARTIAL
        )
        projected_state = {
            **state,
            "qa_item_ids": qa_ids,
            "planner_trajectory_id": trajectory_id,
            "target_difficulties_satisfied": targets_satisfied,
        }
        write_episode(context, projected_state, EpisodeState.PARTIAL)
        recorder.finish(state, "finalize_partial", step_id)
        return with_state(
            projected_state,
            qa_item_ids=qa_ids,
            planner_trajectory_id=trajectory_id,
            target_difficulties_satisfied=targets_satisfied,
        )

    def fail(state: EntityGenerationState) -> EntityGenerationState:
        budget = context.budget
        assert budget is not None
        budget.spend_step()
        step_id = recorder.begin(state, "fail", {"episode": state["episode_id"]})
        qa_ids, trajectory_id, targets_satisfied = persist_training_projection(
            context, state, final_state=EpisodeState.FAILED
        )
        projected_state = {
            **state,
            "qa_item_ids": qa_ids,
            "planner_trajectory_id": trajectory_id,
            "target_difficulties_satisfied": targets_satisfied,
        }
        write_episode(context, projected_state, EpisodeState.FAILED)
        recorder.finish(
            state,
            "fail",
            step_id,
            status="failed",
            error_code=state.get("stop_reason"),
        )
        return with_state(
            projected_state,
            qa_item_ids=qa_ids,
            planner_trajectory_id=trajectory_id,
            target_difficulties_satisfied=targets_satisfied,
        )

    graph = StateGraph(EntityGenerationState)
    graph.add_node("verify_seed", verify_seed)
    graph.add_node("collect_neighbors", collect_neighbors)
    graph.add_node("agent_explore", agent_explore)
    graph.add_node("curate_evidence", curate_evidence_node)
    graph.add_node("realize_validated_tasks", realize_validated_tasks)
    graph.add_node("build_claims", build_claims)
    graph.add_node("plan_questions", plan_questions)
    graph.add_node("write_questions", write_questions)
    graph.add_node("persist_draft_questions", persist_draft_questions)
    graph.add_node("verify_questions", verify_questions)
    graph.add_node("machine_validate", machine_validate)
    graph.add_node("finalize_artifact_links", finalize_artifact_links)
    graph.add_node("finalize", finalize)
    graph.add_node("finalize_partial", finalize_partial)
    graph.add_node("fail", fail)

    graph.set_entry_point("verify_seed")
    graph.add_conditional_edges(
        "verify_seed",
        after_verify_seed,
        {
            Route.FAIL: "fail",
            Route.NEXT: (
                "agent_explore" if context.agentic_exploration else "collect_neighbors"
            ),
        },
    )
    graph.add_conditional_edges(
        "agent_explore",
        after_collect_neighbors,
        {
            Route.FAIL: "fail",
            Route.FINALIZE_PARTIAL: "finalize_partial",
            Route.NEXT: "curate_evidence",
        },
    )
    graph.add_conditional_edges(
        "collect_neighbors",
        after_collect_neighbors,
        {
            Route.FAIL: "fail",
            Route.FINALIZE_PARTIAL: "finalize_partial",
            Route.NEXT: "curate_evidence",
        },
    )
    graph.add_conditional_edges(
        "curate_evidence",
        lambda state: (
            Route.FINALIZE_PARTIAL
            if after_curate_evidence(state) == Route.FINALIZE_PARTIAL
            else (
                "realize_validated_tasks"
                if (
                    context.entity_question_loop
                    and state.get("react_entity_loop_active", False)
                )
                else Route.NEXT
            )
        ),
        {
            Route.FINALIZE_PARTIAL: "finalize_partial",
            Route.NEXT: "build_claims",
            "realize_validated_tasks": "realize_validated_tasks",
        },
    )
    graph.add_conditional_edges(
        "realize_validated_tasks",
        lambda state: (
            Route.NEXT
            if state.get("accepted_question_ids")
            else Route.FINALIZE_PARTIAL
        ),
        {
            Route.NEXT: "finalize_artifact_links",
            Route.FINALIZE_PARTIAL: "finalize_partial",
        },
    )
    graph.add_conditional_edges(
        "build_claims",
        after_build_claims,
        {
            Route.FAIL: "fail",
            Route.FINALIZE_PARTIAL: "finalize_partial",
            Route.NEXT: "plan_questions",
        },
    )
    graph.add_edge("plan_questions", "write_questions")
    graph.add_conditional_edges(
        "write_questions",
        lambda state: (
            Route.FINALIZE_PARTIAL
            if state.get("stop_reason") == StopReason.EVIDENCE_EXHAUSTED.value
            else Route.NEXT
        ),
        {
            Route.FINALIZE_PARTIAL: "finalize_partial",
            Route.NEXT: "persist_draft_questions",
        },
    )
    graph.add_conditional_edges(
        "persist_draft_questions",
        after_persist_draft_questions,
        {
            Route.FAIL: "fail",
            Route.FINALIZE_PARTIAL: "finalize_partial",
            Route.NEXT: "verify_questions",
        },
    )
    graph.add_conditional_edges(
        "verify_questions",
        after_verify_questions,
        {
            Route.REWRITE: "write_questions",
            Route.FINALIZE_PARTIAL: "finalize_partial",
            Route.NEXT: "machine_validate",
        },
    )
    graph.add_conditional_edges(
        "machine_validate",
        after_machine_validate,
        {
            Route.FINALIZE_PARTIAL: "finalize_partial",
            Route.NEXT: "finalize_artifact_links",
        },
    )
    graph.add_edge("finalize_artifact_links", "finalize")
    graph.add_edge("finalize", END)
    graph.add_edge("finalize_partial", END)
    graph.add_edge("fail", END)
    return graph.compile(checkpointer=checkpointer)


def _review_group_key_for_draft(
    request: EntityGenerationRequest,
    draft: QuestionDraft,
    blueprint: QuestionBlueprint | None,
    relation: str,
) -> str | None:
    """The review group key of a draft question.

    The key is derived from the seed identity and the ordered relation
    chain, so two candidates that ask the same structural question land in
    the same group. A draft without a relation has no group.
    """
    from .grouping import GroupStep, group_key_for_path

    relations: list[str] = []
    directions: list[str] = []
    if blueprint is not None:
        # The multi-hop blueprint carries the path's ordered chain in its
        # logical query; the single-hop one carries a single relation.
        for step in (blueprint.logical_query or {}).get("steps", []) or []:
            relations.append(str(step.get("relation", "")))
            directions.append(str(step.get("direction", "forward")))
    if not relations and relation:
        relations = [relation]
        directions = ["forward"]
    if not relations:
        return None
    steps = [
        GroupStep(position=index, relation=relation_name, direction=direction)
        for index, (relation_name, direction) in enumerate(
            zip(relations, directions)
        )
    ]
    return group_key_for_path(
        seed_provider_id=request.seed.provider_id,
        seed_entity_id=request.seed.native_id,
        steps=steps,
        answer_entity_type=(
            blueprint.answer_entity_type if blueprint is not None else None
        ),
    )


def question_spec_from_draft(
    state: EntityGenerationState, payload: dict[str, Any], bundle: EvidenceBundle
) -> QuestionSpec:
    request = EntityGenerationRequest.model_validate(state["request"])
    draft = QuestionDraft.model_validate(payload)
    question_id = stable_id(
        state["episode_id"], "question", f"{draft.blueprint_id}|{draft.draft_id}"
    )
    blueprints = {
        key: QuestionBlueprint.model_validate(value)
        for key, value in state.get("question_blueprints", {}).items()
    }
    blueprint = blueprints.get(draft.blueprint_id)
    relation = next(
        (
            item.relation
            for item in bundle.kg_evidence
            if item.evidence_id in draft.evidence_ids
        ),
        "",
    )
    version = f"{question_id}.draft"
    group_key = _review_group_key_for_draft(request, draft, blueprint, relation)
    hop_count = max(len(draft.evidence_ids), 1)
    expected_route = (
        QuestionRoute.HYBRID
        if draft.literature_evidence_ids
        else QuestionRoute.KG
    )
    split_group_id = stable_id(
        request.workspace,
        "split",
        json.dumps(
            {
                "seed": request.seed.native_id,
                "seed_type": request.seed.entity_type,
                "answer_ids": sorted(
                    str(item.get("canonical_id"))
                    for item in draft.candidate_answer.values
                    if item.get("canonical_id")
                ),
                "kg_relations": [
                    item.relation
                    for item in bundle.kg_evidence
                    if item.evidence_id in draft.evidence_ids
                ],
            },
            sort_keys=True,
        ),
    )
    return QuestionSpec(
        question_id=question_id,
        logical_question_id=stable_id(state["episode_id"], "logical", draft.blueprint_id),
        question_version=version,
        bundle_ids=[bundle.bundle_id],
        generation_mode=GenerationMode.AUTOMATIC,
        canonical_question=draft.question_text,
        question_variants=[
            QuestionVariant(
                variant_id=f"{question_id}.v1",
                text=draft.question_text,
                language=draft.language,
                persona="system",
                style=draft.style_profile or "neutral",
                generation_source="agent",
                # The semantic check has not been evaluated yet; "warning"
                # is the contract's way of saying "not asserted as passed".
                semantic_check_status="warning",
            )
        ],
        taxonomy=QuestionTaxonomy(
            domain_intent=draft.question_type,
            answer_form=draft.candidate_answer.answer_mode,
            reasoning_pattern=(
                ["two_hop", "relation_chain"]
                if hop_count >= 2
                else ["single_hop", "relation_lookup"]
            ),
            hop_complexity=hop_count,
            difficulty_tags=[
                (blueprint.relation_semantics.value if blueprint else "unknown")
            ],
        ),
        query_intent={
            "route": expected_route.value,
            "question_type": draft.question_type,
            "question_intent": draft.question_intent,
            "semantic_skeleton": draft.semantic_skeleton,
            "answer_candidate_count": (
                blueprint.logical_query.get("answer_candidate_count", 1)
                if blueprint is not None
                else 1
            ),
            "provider_id": request.seed.provider_id,
            "relation": relation,
            "style_skill_id": draft.style_skill_id,
            "style_skill_version": draft.style_skill_version,
            "style_profile": draft.style_profile,
            "style_score": draft.style_score,
            # The structured fields the review grouping is computed from. The
            # group key is derived from these, never from the question text.
            "seed_entity_id": request.seed.native_id,
            "seed_entity_type": request.seed.entity_type,
            "relations": [relation] if relation else [],
            "directions": ["forward"] if relation else [],
            # The answer's entity type, when the blueprint knows it. It is
            # part of the group key, so it must be the same value the key
            # was computed from.
            "answer_entity_type": (
                blueprint.answer_entity_type
                if blueprint is not None
                else None
            ),
            "intermediate_entity_ids": list(
                blueprint.intermediate_entity_ids if blueprint else []
            ),
            "required_literature_evidence_ids": list(
                draft.literature_evidence_ids
            ),
            "evidence_constraints": (
                request.generation_spec.evidence_constraints.model_dump(mode="json")
                if request.generation_spec is not None
                else {}
            ),
        },
        review_group_key=group_key,
        review_group_version=REVIEW_GROUP_VERSION,
        answerability=Answerability.ANSWERABLE,
        gold_answer=draft.candidate_answer,
        evidence_requirements=EvidenceRequirements(
            required_claim_ids=draft.claim_ids,
            required_kg_evidence_ids=draft.evidence_ids,
            required_literature_evidence_ids=list(
                draft.literature_evidence_ids
            ),
            sufficiency_rule_version=(
                request.generation_spec.spec_version
                if request.generation_spec is not None
                else VERIFICATION_POLICY_VERSION
            ),
        ),
        expected_route=expected_route,
        verified_route=QuestionRoute.UNKNOWN,
        validation_level=ValidationLevel.DRAFT,
        split_group_id=split_group_id,
        generation_episode_id=state["episode_id"],
        created_at=utc_now(),
        changes=[
            ChangeRecord(
                change_id=new_id("change"),
                actor_type=ActorType.AGENT,
                operation="generation_draft",
                summary=f"generated from blueprint {draft.blueprint_id}",
                created_at=utc_now(),
                parent_version=None,
                new_version=version,
            )
        ],
    )


def draft_from_question(spec: QuestionSpec, episode_id: str) -> QuestionDraft:
    """Recover the draft contract from the persisted draft QuestionSpec."""
    return QuestionDraft(
        draft_id=spec.question_id,
        blueprint_id=stable_id(episode_id, "blueprint", spec.question_id),
        question_text=spec.canonical_question,
        candidate_answer=spec.gold_answer,
        claim_ids=list(spec.evidence_requirements.required_claim_ids),
        evidence_ids=list(spec.evidence_requirements.required_kg_evidence_ids),
        literature_evidence_ids=list(
            spec.evidence_requirements.required_literature_evidence_ids
        ),
        question_type=str(
            spec.query_intent.get(
                "question_type", spec.taxonomy.domain_intent
            )
        ),
        language=(
            spec.question_variants[0].language
            if spec.question_variants
            else "zh-CN"
        ),
        style_skill_id=spec.query_intent.get("style_skill_id"),
        style_skill_version=spec.query_intent.get("style_skill_version"),
        style_profile=spec.query_intent.get("style_profile"),
        style_score=spec.query_intent.get("style_score"),
        question_intent=spec.query_intent.get("question_intent"),
        semantic_skeleton=spec.query_intent.get("semantic_skeleton"),
        generation_episode_id=episode_id,
    )


def write_episode(
    context: GenerationContext, state: EntityGenerationState, final_state: EpisodeState
) -> GenerationEpisode:
    request = context.request
    episode_id = state["episode_id"]
    existing = context.episodes.artifacts.list_versions("episodes", episode_id)
    stop_reason = state.get("stop_reason")
    episode = GenerationEpisode(
        episode_id=episode_id,
        episode_version=f"{episode_id}.v{len(existing) + 1}",
        task_id=request.task_id,
        seed=request.seed,
        state=final_state,
        policy_version=request.policy_version,
        question_target=request.question_count,
        step_ids=[
            item.step_id for item in context.episodes.list_steps_by_episode(episode_id)
        ],
        evidence_ids=list(state.get("evidence_ids", [])),
        claim_ids=list(state.get("claim_ids", [])),
        question_ids=list(state.get("accepted_question_ids", [])),
        qa_item_ids=list(state.get("qa_item_ids", [])),
        planner_trajectory_id=state.get("planner_trajectory_id"),
        stop_reason=StopReason(stop_reason) if stop_reason else None,
        started_at=utc_now(),
        completed_at=utc_now(),
    )
    context.episodes.save_episode(episode)
    return episode

