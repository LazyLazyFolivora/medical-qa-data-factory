"""Service layer: runs one episode and supports resume.

The provider and checkpointer are closed in ``finally`` on both the success and
the exception path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from enum import Enum

from ..domain import GenerationMode, GenerationTask, TaskStatus
from ..domain.common import new_id, utc_now
from ..knowledge_graph import provider_from_config
from ..knowledge_graph.errors import KnowledgeGraphError
from ..repository import LocalArtifactRepository
from .contracts import (
    EntityGenerationRequest,
    EpisodeState,
    GenerationRunResult,
    StopReason,
    UpstreamEntitySeed,
)
from .graph import GenerationContext, NodeRecorder, build_graph, initial_state
from .repository import GenerationRepository

CHECKPOINT_FILENAME = "generation.sqlite"
EVIDENCE_CONFIG_ENV = "MEDICAL_QA_EVIDENCE_CONFIG"


#: Assembly modes the service can build components for. ``rule-based`` is the
#: deterministic single-hop regression baseline; it is not a ReAct agent and
#: must never be labelled as one. ``fake`` drives a preset model double and is
#: only valid once a ReAct planner consumes it. ``structured`` talks to a real
#: configured provider.
SUPPORTED_MODEL_MODES: tuple[str, ...] = ("rule-based", "fake", "structured")


class ModelCapability(str, Enum):
    """What a mode can honestly claim about itself."""

    DETERMINISTIC_BASELINE = "deterministic_baseline"
    SCRIPTED_MODEL = "scripted_model"
    REAL_MODEL = "real_model"


class GenerationError(RuntimeError):
    """Carries a stable code and an honest retryable flag."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.message = message

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


def checkpoint_path(workspace: str | Path) -> Path:
    return Path(workspace) / "checkpoints" / CHECKPOINT_FILENAME


def episode_id_for(request: EntityGenerationRequest) -> str:
    """The ``rule-based`` episode id. Kept for callers that only ever run
    the deterministic baseline; other modes must use
    :func:`episode_id_for_mode`."""
    from .graph import stable_id

    return episode_id_for_mode(request, "rule-based")


def episode_id_for_mode(
    request: EntityGenerationRequest, model_mode: str
) -> str:
    """Episode identity, isolated per model mode.

    The mode is part of the identity on purpose: ``rule-based``, ``fake`` and
    ``structured`` must never write into the same episode, checkpoint thread
    or artifact set, or a run in one mode would be read as a run in another.
    """
    from .graph import stable_id

    return stable_id(request.workspace, "episode", f"{model_mode}|{request.task_id}")


def ensure_generation_task(
    repository: LocalArtifactRepository, request: EntityGenerationRequest
) -> GenerationTask:
    """Create the task on first run; never overwrite an existing one."""
    try:
        return repository.load_latest_task(request.task_id)
    except Exception:
        pass
    seed = request.seed
    from ..domain import EntityRef, LinkStatus

    entity = EntityRef(
        entity_ref_id=f"entity-{seed.provider_id}:{seed.entity_type}:{seed.native_id}",
        raw_mention=seed.raw_mention or seed.canonical_name,
        canonical_name=seed.canonical_name,
        canonical_id=f"{seed.provider_id}:{seed.entity_type}:{seed.native_id}",
        namespace=seed.provider_id,
        entity_type=seed.entity_type,
        aliases=[],
        link_status=LinkStatus.EXACT,
        link_score=1.0,
        source="upstream",
    )
    task = GenerationTask.create(
        task_id=request.task_id,
        generation_mode=GenerationMode.AUTOMATIC,
        seed_entities=[entity],
        target_question_count=request.question_count,
    )
    repository.save_task(task)
    return task


def model_capability(mode: str) -> ModelCapability:
    """Report what a mode can honestly claim. Used by CLI and UI labels."""
    return {
        "rule-based": ModelCapability.DETERMINISTIC_BASELINE,
        "fake": ModelCapability.SCRIPTED_MODEL,
        "structured": ModelCapability.REAL_MODEL,
    }[mode]


def build_model_components(
    mode: str,
    *,
    model: Any = None,
    generation_spec: Any = None,
) -> dict[str, Any]:
    """Assemble the baseline or model-driven exploration components.

    ``rule-based`` and ``fake`` are deliberately not the same assembly: the
    baseline calls no model at all, while ``fake`` requires a model double to
    be supplied by the caller. The real ``structured`` mode wires in the
    configured provider adapter and fails fast when the configuration is
    missing instead of silently falling back to rules.
    """
    from .claims import RuleBasedClaimBuilder
    from .planning import EnrichedEntityQuestionPlanner, QuestionPlanner
    from .semantics import RelationSemanticsPolicy
    from .style_evaluation import StructuredStyleJudge
    from .verification import (
        RuleBasedAnswerVerifier,
        StructuredAnswerVerifier,
    )
    from .writing import (
        RuleBasedQuestionWriter,
        StructuredQuestionWriter,
    )

    if mode not in SUPPORTED_MODEL_MODES:
        raise GenerationError(
            "capability_error", f"unsupported model mode: {mode!r}"
        )
    if mode == "structured" and model is None:
        # The real provider is assembled from the registry, and a missing or
        # unusable configuration fails fast. There is no fallback to rules: a
        # run that silently degraded would still be labelled ``structured``.
        #
        from ..llm.errors import ModelError
        from ..llm.registry import build_model_port

        try:
            model = build_model_port()
        except ModelError as exc:
            raise GenerationError(
                "model_config_error", exc.message, retryable=exc.retryable
            ) from exc
    policy = RelationSemanticsPolicy()
    components = {
        "policy": policy,
        "claim_builder": RuleBasedClaimBuilder(),
        "planner": QuestionPlanner(),
        "verifier": RuleBasedAnswerVerifier(),
        "writer": RuleBasedQuestionWriter(),
    }
    if generation_spec is not None and model is not None:
        components["writer"] = StructuredQuestionWriter(model, policy=policy)
        components["verifier"] = StructuredAnswerVerifier(model, policy=policy)
        components["style_judge"] = StructuredStyleJudge(model)
        if generation_spec.evidence_enrichment:
            components["planner"] = EnrichedEntityQuestionPlanner()
    if mode == "fake":
        # A scripted model must be provided by the caller; a mode that cannot
        # call a model must not accept one.
        return {
            **components,
            "model": model,
            "model_required": True,
            "agentic_exploration": True,
        }
    if mode == "structured":
        return {
            **components,
            "model": model,
            "model_required": True,
            "agentic_exploration": True,
        }
    return {**components, "model_required": False, "agentic_exploration": False}


def assert_model_mode(mode: str) -> ModelCapability:
    """Validate the requested mode before any episode work begins."""
    if mode not in SUPPORTED_MODEL_MODES:
        raise GenerationError(
            "capability_error", f"unsupported model mode: {mode!r}"
        )
    return model_capability(mode)


class EntityGenerationService:
    """Owns the episode lifecycle, checkpoints, and provider shutdown."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        provider_id: str | None = None,
        model_mode: str = "rule-based",
        model: Any = None,
        literature_provider: Any = None,
        literature_provider_id: str | None = None,
        literature_knowledge_base_ids: tuple[str, ...] = (),
        literature_snapshot_id: str | None = None,
        evidence_enricher: Any = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.provider_id = provider_id
        self.model_mode = model_mode
        # Injected model port. ``rule-based`` never uses it.
        self.model = model
        self.literature_provider = literature_provider
        self.literature_provider_id = literature_provider_id
        self.literature_knowledge_base_ids = literature_knowledge_base_ids
        self.literature_snapshot_id = literature_snapshot_id
        self._injected_evidence_enricher = evidence_enricher
        self.repository = LocalArtifactRepository(self.workspace)
        self.episodes = GenerationRepository(self.workspace)

    # ---- lifecycle ----------------------------------------------------

    def _checkpointer(self) -> Any:
        from langgraph.checkpoint.sqlite import SqliteSaver

        path = checkpoint_path(self.workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        return SqliteSaver.from_conn_string(str(path))

    def run(
        self,
        request: EntityGenerationRequest,
        *,
        _episode_id: str | None = None,
        _resume_from_checkpoint: bool = False,
    ) -> GenerationRunResult:
        # The requested mode is validated before any artifact is written, so
        # an unsupported mode can never produce a misleading run.
        assert_model_mode(self.model_mode)
        task = ensure_generation_task(self.repository, request)
        episode_id = _episode_id or episode_id_for_mode(request, self.model_mode)
        if episode_id not in self.episodes.list_episode_ids():
            self._write_initial_episode(request, episode_id)
        components = build_model_components(
            self.model_mode,
            model=self.model,
            generation_spec=request.generation_spec,
        )
        if components.pop("model_required", False) and components.get("model") is None:
            # A mode that needs a model must not silently degrade to rules and
            # still claim it ran with a model.
            raise GenerationError(
                "capability_error",
                f"model mode {self.model_mode!r} requires an injected model port",
            )
        provider = None
        saver_cm = None
        try:
            try:
                provider = provider_from_config(self.provider_id)
            except KnowledgeGraphError as exc:
                # Configuration problems surface with their own stable code so
                # the CLI can pick the documented exit status.
                raise GenerationError(
                    exc.code, exc.message, retryable=exc.retryable
                ) from exc
            literature = self._resolve_literature_provider(request)
            evidence_enricher = self._resolve_evidence_enricher(
                request, components.get("model")
            )
            self._write_run_metadata(request, episode_id, provider, literature)
            context = GenerationContext(
                request=request,
                repository=self.repository,
                episodes=self.episodes,
                provider=provider,
                tool_name=f"kg_{provider.provider_id}",
                model_config_id=(
                    str(getattr(self.model, "model_config_id", self.model_mode))
                    if self.model is not None
                    else self.model_mode
                ),
                literature_provider=(
                    literature[0] if literature is not None else None
                ),
                literature_provider_id=(
                    literature[1] if literature is not None else None
                ),
                literature_knowledge_base_ids=(
                    tuple(literature[2]) if literature is not None else ()
                ),
                literature_snapshot_id=(
                    literature[3] if literature is not None else None
                ),
                evidence_enricher=evidence_enricher,
                **components,
            )
            recorder = NodeRecorder(context)
            saver_cm = self._checkpointer()
            # The compiled graph is built inside the saver context so the
            # checkpointer is bound before the first node runs.
            with saver_cm as checkpointer:
                saver_cm = None  # ownership moved to the ``with`` block
                compiled = build_graph(context, recorder, checkpointer=checkpointer)
                config = {
                    "configurable": {
                        "thread_id": self.episodes.graph_thread_id(episode_id)
                    },
                    "recursion_limit": (
                        request.resolved_safety_budget().max_model_attempts
                        + 10
                    ),
                }
                if _resume_from_checkpoint:
                    snapshot = compiled.get_state(config)
                    if not snapshot.values:
                        raise GenerationError(
                            "checkpoint_not_found",
                            f"no checkpoint state exists for episode {episode_id}",
                        )
                    final = (
                        compiled.invoke(None, config=config)
                        if snapshot.next
                        else dict(snapshot.values)
                    )
                else:
                    final = compiled.invoke(
                        initial_state(request, episode_id), config=config
                    )
            return self._result(request, task, final)
        except GenerationError:
            raise
        finally:
            if saver_cm is not None:
                saver_cm.__exit__(None, None, None)
            if provider is not None:
                provider.close()

    def resume(
        self, request: EntityGenerationRequest, episode_id: str
    ) -> GenerationRunResult:
        if episode_id not in self.episodes.list_episode_ids():
            raise GenerationError(
                "episode_not_found", f"no episode record for {episode_id}"
            )
        if not checkpoint_path(self.workspace).exists():
            raise GenerationError(
                "checkpoint_not_found",
                f"no checkpoint database at {checkpoint_path(self.workspace)}",
            )
        episode = self.episodes.load_latest_episode(episode_id)
        if episode.task_id != request.task_id or episode.seed != request.seed:
            raise GenerationError(
                "episode_mismatch",
                "the requested task and seed do not match the episode being resumed",
            )
        return self.run(
            request,
            _episode_id=episode_id,
            _resume_from_checkpoint=True,
        )

    # ---- helpers ------------------------------------------------------

    def _resolve_literature_provider(self, request: EntityGenerationRequest):
        """Build the formal literature port only when the spec can use it."""
        spec = request.generation_spec
        if (
            spec is None
            or not self._is_agentic_mode()
            or "literature"
            not in {item.value for item in spec.allowed_sources}
            or spec.evidence_constraints.literature_policy.value == "forbidden"
        ):
            return None
        if self.literature_provider is not None:
            capability = self.literature_provider.preflight()
            snapshot_id = (
                capability.snapshot.snapshot_id
                if capability.snapshot
                else self.literature_snapshot_id
            )
            knowledge_bases = list(
                self.literature_knowledge_base_ids
                or (
                    capability.snapshot.knowledge_base_ids
                    if capability.snapshot
                    else []
                )
            )
            return (
                self.literature_provider,
                self.literature_provider_id or capability.provider,
                knowledge_bases,
                snapshot_id,
            )
        from ..literature.registry import (
            LiteratureRegistry,
            load_literature_config,
        )

        try:
            registry = LiteratureRegistry(
                config=load_literature_config()
            )
            provider = registry.build()
            provider_config = registry.config.provider()
            capability = provider.preflight()
        except Exception as exc:
            required = (
                spec.evidence_constraints.literature_policy.value == "required"
            )
            if required:
                code = getattr(exc, "code", "literature_config_error")
                raise GenerationError(
                    str(code),
                    f"the generation spec requires literature, but no usable "
                    f"provider was assembled: {exc}",
                ) from exc
            return None
        return (
            provider,
            provider_config.provider_id,
            list(provider_config.knowledge_base_ids),
            (
                capability.snapshot.snapshot_id
                if capability.snapshot
                else provider_config.snapshot_id
            ),
        )

    def _is_agentic_mode(self) -> bool:
        return self.model_mode in ("fake", "structured")

    def _resolve_evidence_enricher(self, request, model):
        spec = request.generation_spec
        if spec is None or not spec.evidence_enrichment:
            return None
        if self._injected_evidence_enricher is not None:
            return self._injected_evidence_enricher
        if model is None:
            raise GenerationError(
                "evidence_enrichment_unavailable",
                "evidence enrichment requires a structured model",
            )
        from ..evidence_enrichment.ctd import CtdEvidenceIndex
        from ..evidence_enrichment.pubmed import PubMedClient
        from ..evidence_enrichment.service import EvidenceEnrichmentService

        config_path = Path(
            os.environ.get(
                EVIDENCE_CONFIG_ENV, "config/evidence_enrichment.json"
            )
        )
        if not config_path.is_file():
            raise GenerationError(
                "evidence_enrichment_unavailable",
                f"evidence enrichment config not found: {config_path}",
            )
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GenerationError(
                "evidence_enrichment_config_error",
                f"invalid evidence enrichment config: {exc}",
            ) from exc
        if not config.get("enabled", False):
            raise GenerationError(
                "evidence_enrichment_unavailable",
                "evidence enrichment is disabled",
            )
        pubmed_config = dict(config.get("pubmed") or {})
        if not pubmed_config.get("enabled", True):
            raise GenerationError(
                "evidence_enrichment_unavailable",
                "PubMed evidence enrichment is disabled",
            )
        ctd_path = Path(
            str(
                config.get(
                    "ctd_index_path", "data/cache/ctd-evidence.sqlite"
                )
            )
        )
        ctd_index = CtdEvidenceIndex(ctd_path) if ctd_path.is_file() else None
        return EvidenceEnrichmentService(
            ctd_index=ctd_index,
            model=model,
            pubmed=PubMedClient(),
            max_abstracts=int(pubmed_config.get("max_abstracts", 5)),
        )

    def _write_run_metadata(
        self,
        request: EntityGenerationRequest,
        episode_id: str,
        provider: Any,
        literature: Any,
    ) -> None:
        from .training import (
            GenerationRunMetadata,
            stable_training_id,
        )
        from .training_repository import TrainingRepository
        from ..llm.prompts import prompt_hashes
        from .question_skills import load_question_skills

        spec = request.resolved_generation_spec()
        snapshot = provider.snapshot()
        hashes = prompt_hashes()
        if request.generation_spec is not None:
            for skill in load_question_skills(spec.question_style_ids):
                hashes[f"style:{skill.skill_id}"] = skill.prompt_hash()
        TrainingRepository(str(self.workspace)).save_metadata(
            GenerationRunMetadata(
                generation_spec_id=stable_training_id(
                    "generation-spec",
                    episode_id,
                    spec.model_dump(mode="json"),
                    prefix="generation-spec",
                ),
                generation_spec=spec,
                safety_budget=request.resolved_safety_budget(),
                model_config_id=(
                    str(getattr(self.model, "model_config_id", self.model_mode))
                    if self.model is not None
                    else self.model_mode
                ),
                prompt_hashes=hashes,
                kg_provider_id=provider.provider_id,
                kg_snapshot_id=snapshot.data_snapshot_id,
                literature_provider_id=(
                    literature[1] if literature is not None else None
                ),
                literature_snapshot_id=(
                    literature[3] if literature is not None else None
                ),
            ),
            episode_id,
        )

    def _write_initial_episode(
        self, request: EntityGenerationRequest, episode_id: str
    ) -> None:
        from .contracts import GenerationEpisode

        self.episodes.save_episode(
            GenerationEpisode(
                episode_id=episode_id,
                episode_version=f"{episode_id}.v1",
                task_id=request.task_id,
                seed=request.seed,
                state=EpisodeState.RUNNING,
                policy_version=request.policy_version,
                question_target=request.question_count,
                started_at=utc_now(),
            )
        )

    def _result(
        self,
        request: EntityGenerationRequest,
        task: GenerationTask,
        final: dict[str, Any],
    ) -> GenerationRunResult:
        episode_id = final["episode_id"]
        episode = self.episodes.load_latest_episode(episode_id)
        stop_reason = final.get("stop_reason")
        if not stop_reason:
            # A partial run without an explicit reason means the evidence ran
            # out before the target; only a failed episode defaults to fatal.
            stop_reason = (
                StopReason.FATAL_ERROR.value
                if episode.state is EpisodeState.FAILED
                else StopReason.EVIDENCE_EXHAUSTED.value
            )
            self._record_stop_reason(episode_id, stop_reason)
        queued = list(final.get("accepted_question_ids", []))
        status = episode.state.value
        self._update_task_status(task, status)
        return GenerationRunResult(
            task_id=request.task_id,
            episode_id=episode_id,
            status=status,  # type: ignore[arg-type]
            bundle_ids=[final["bundle_id"]] if final.get("bundle_id") else [],
            qa_item_ids=list(final.get("qa_item_ids", [])),
            planner_trajectory_id=final.get("planner_trajectory_id"),
            claim_ids=list(final.get("claim_ids", [])),
            question_ids=queued,
            rejection_record_ids=list(final.get("rejection_record_ids", [])),
            stop_reason=stop_reason,
            target_question_count=request.question_count,
            target_difficulties_satisfied=bool(
                final.get("target_difficulties_satisfied", False)
            ),
        )

    def _record_stop_reason(self, episode_id: str, stop_reason: str) -> None:
        """Persist the derived reason so the audit trail never shows a null."""
        episode = self.episodes.load_latest_episode(episode_id)
        existing = self.episodes.artifacts.list_versions("episodes", episode_id)
        self.episodes.save_episode(
            episode.model_copy(
                update={
                    "episode_version": f"{episode_id}.v{len(existing) + 1}",
                    "stop_reason": StopReason(stop_reason),
                }
            )
        )

    def _update_task_status(self, task: GenerationTask, status: str) -> None:
        from ..workflow import transition_task

        mapping = {
            EpisodeState.COMPLETED.value: TaskStatus.COMPLETED,
            EpisodeState.PARTIAL.value: TaskStatus.PARTIAL,
            EpisodeState.FAILED.value: TaskStatus.FAILED,
        }
        target = mapping.get(status, TaskStatus.PARTIAL)
        latest = self.repository.load_latest_task(task.task_id)
        if latest.status is TaskStatus.PENDING:
            # Match the existing pipeline: a task always enters `running`
            # before it can reach a terminal state.
            latest = transition_task(
                latest,
                TaskStatus.RUNNING,
                summary="start entity generation",
                current_stage="evidence_collection",
            )
            self.repository.save_task(latest)
        if latest.status is target:
            return
        self.repository.save_task(
            transition_task(
                latest,
                target,
                summary=f"generation episode finished with status {status}",
                current_stage="question_generated",
            )
        )
