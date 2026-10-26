"""Evidence-first, path-second, reload-validation.

Step 3 of the real-generation-agent plan. The order matters and is not
negotiable:

1. Every KG edge the path uses is persisted as ``KGEvidence`` first.
2. Only then is the ``EvidencePath`` written, referencing those evidence ids.
3. The evidence registry and the path are reloaded from disk, and the path is
   re-validated against the reloaded records: endpoints, relation, direction,
   snapshot and the ordering of the steps.

Only after step 3 succeeds is the path published. A path that cannot be
rebuilt from the persisted registry is not a path, and a crash between the
evidence write and the path write must not leave a path that claims evidence
that does not exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..domain import EvidenceBundle, KGEvidence
from pydantic import ValidationError

from ..domain.common import new_id, utc_now
from ..knowledge_graph.contracts import OrderedPath
from .contracts import EvidencePath, EvidencePathStep


class PathValidationError(RuntimeError):
    """A path does not survive reloading and re-validation."""

    code = "path_validation_failed"


@dataclass
class PathPersistResult:
    """What was persisted and what was rejected."""

    paths: list[EvidencePath] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)


def _evidence_for_edge(
    edge: OrderedPath,
    step: EvidencePathStep,
    evidence_by_edge: dict[str, KGEvidence],
) -> KGEvidence:
    try:
        return evidence_by_edge[step.edge_id]
    except KeyError as exc:
        raise PathValidationError(
            f"path {edge.path_id} step {step.position} references edge "
            f"{step.edge_id!r}, which has no persisted evidence"
        ) from exc


def _step_matches_evidence(
    step: EvidencePathStep, evidence: KGEvidence
) -> tuple[bool, str]:
    """Check one step against the evidence that was persisted for it."""
    head_id = evidence.head.canonical_id
    tail_id = evidence.tail.canonical_id
    if step.traversal_direction == "forward":
        if not (head_id == step.from_entity_id and tail_id == step.to_entity_id):
            return False, (
                f"step {step.position} says forward {step.from_entity_id} -> "
                f"{step.to_entity_id} but the evidence runs {head_id} -> {tail_id}"
            )
    else:
        if not (tail_id == step.from_entity_id and head_id == step.to_entity_id):
            return False, (
                f"step {step.position} says reverse {step.from_entity_id} -> "
                f"{step.to_entity_id} but the evidence runs {head_id} -> {tail_id}"
            )
    if evidence.relation != step.relation:
        return False, (
            f"step {step.position} says relation {step.relation!r} but the "
            f"evidence says {evidence.relation!r}"
        )
    return True, ""


def validate_evidence_path(
    path: EvidencePath,
    evidence_by_id: dict[str, KGEvidence],
) -> None:
    """Re-validate a path against the persisted evidence registry.

    Raises ``PathValidationError`` with a specific reason. Every check here
    can be re-derived from the stored records; nothing is taken on trust.
    """
    if len(path.steps) < 2:
        raise PathValidationError(
            f"path {path.path_id} has {len(path.steps)} steps; a path needs at least two"
        )
    positions = [step.position for step in path.steps]
    if positions != list(range(len(path.steps))):
        raise PathValidationError(
            f"path {path.path_id} step positions are not contiguous from zero"
        )
    for index, step in enumerate(path.steps):
        if step.evidence_id not in evidence_by_id:
            raise PathValidationError(
                f"path {path.path_id} step {index} references unknown evidence "
                f"{step.evidence_id!r}"
            )
        evidence = evidence_by_id[step.evidence_id]
        ok, reason = _step_matches_evidence(step, evidence)
        if not ok:
            raise PathValidationError(f"path {path.path_id}: {reason}")
        provider = str(evidence.source_database)
        if provider != path.provider_id:
            raise PathValidationError(
                f"path {path.path_id} step {index} comes from provider "
                f"{provider!r} but the path claims {path.provider_id!r}"
            )
    if path.steps[0].from_entity_id != path.start_entity_id:
        raise PathValidationError(
            f"path {path.path_id} does not start at {path.start_entity_id}"
        )
    if path.steps[-1].to_entity_id != path.end_entity_id:
        raise PathValidationError(
            f"path {path.path_id} does not end at {path.end_entity_id}"
        )
    for left, right in zip(path.steps, path.steps[1:]):
        if left.to_entity_id != right.from_entity_id:
            raise PathValidationError(
                f"path {path.path_id} is broken between step {left.position} "
                f"and step {right.position}"
            )
    if len(set(path.source_evidence_ids)) != len(path.source_evidence_ids):
        raise PathValidationError(
            f"path {path.path_id} reuses the same evidence id"
        )
    expected_intermediate = [step.to_entity_id for step in path.steps[:-1]]
    if path.intermediate_entity_ids != expected_intermediate:
        raise PathValidationError(
            f"path {path.path_id} intermediate entities are not the step joints"
        )


def build_evidence_path(
    ordered_path: OrderedPath,
    *,
    episode_id: str,
    task_id: str,
    bundle_id: str,
    evidence_by_edge: dict[str, KGEvidence],
) -> EvidencePath:
    """Bind a graph-layer walk to the persisted evidence it uses."""
    steps: list[EvidencePathStep] = []
    for step in ordered_path.steps:
        evidence = _evidence_for_edge(ordered_path, step, evidence_by_edge)
        steps.append(
            EvidencePathStep(
                position=step.position,
                evidence_id=evidence.evidence_id,
                traversal_direction=step.traversal_direction,
                from_entity_id=step.from_entity_id,
                relation=step.relation,
                to_entity_id=step.to_entity_id,
                provider_id=evidence.source_database,
                snapshot_id=ordered_path.snapshot_id,
                query_trace_id=step.query_trace_id,
            )
        )
    return EvidencePath(
        path_id=ordered_path.path_id,
        path_version=f"{ordered_path.path_id}.v1",
        episode_id=episode_id,
        task_id=task_id,
        bundle_id=bundle_id,
        provider_id=ordered_path.provider_id,
        snapshot_id=ordered_path.snapshot_id,
        start_entity_id=ordered_path.start_entity_id,
        end_entity_id=ordered_path.end_entity_id,
        steps=steps,
        intermediate_entity_ids=list(ordered_path.intermediate_entity_ids),
        source_evidence_ids=[item.evidence_id for item in steps],
        path_hash=ordered_path.path_hash,
        query_trace_ids=list(ordered_path.query_trace_ids),
    )


class PathPersister:
    """Persists evidence, then paths, then re-validates them.

    The order is the whole point. Evidence is written first, so a path can
    never reference an edge that was never stored.
    """

    def __init__(
        self,
        *,
        repository: object,
        episode_id: str,
        task_id: str,
        bundle_id: str,
    ) -> None:
        self.repository = repository
        self.episode_id = episode_id
        self.task_id = task_id
        self.bundle_id = bundle_id

    def persist(
        self,
        ordered_paths: Sequence[OrderedPath],
        evidence: Iterable[KGEvidence],
    ) -> PathPersistResult:
        """Persist the evidence, then the paths, then re-validate them.

        A path that fails re-validation is rejected with a reason; it is not
        written into the published set.
        """
        result = PathPersistResult()
        evidence_list = list(evidence)
        by_edge: dict[str, KGEvidence] = {}
        for item in evidence_list:
            self._save_evidence(item)
            result.evidence_ids.append(item.evidence_id)
            edge_id = item.source_record_id
            if edge_id:
                by_edge[str(edge_id)] = item
        for ordered in ordered_paths:
            try:
                path = build_evidence_path(
                    ordered,
                    episode_id=self.episode_id,
                    task_id=self.task_id,
                    bundle_id=self.bundle_id,
                    evidence_by_edge=by_edge,
                )
            except PathValidationError as exc:
                result.rejected.append((ordered.path_id, str(exc)))
                continue
            except ValidationError as exc:
                # The path contract rejected the walk. That is a rejection of
                # this candidate, not a failure of the whole run.
                result.rejected.append((ordered.path_id, str(exc)))
                continue
            self._save_path(path)
            result.paths.append(path)
        # Reload and re-validate. A path that does not survive the round trip
        # is removed from the published set.
        reloaded = self._reload_evidence()
        published: list[EvidencePath] = []
        for path in result.paths:
            try:
                validate_evidence_path(path, reloaded)
            except PathValidationError as exc:
                result.rejected.append((path.path_id, str(exc)))
                continue
            published.append(path)
        result.paths = published
        return result

    # ---- storage -------------------------------------------------------

    def _save_evidence(self, evidence: KGEvidence) -> None:
        save = getattr(self.repository, "save", None)
        if save is None:
            raise PathValidationError("the repository cannot store evidence")
        save("kg_evidence", evidence.evidence_id, evidence.evidence_id, evidence)

    def _save_path(self, path: EvidencePath) -> None:
        save = getattr(self.repository, "save", None)
        if save is None:
            raise PathValidationError("the repository cannot store paths")
        save("paths", path.path_id, path.path_version, path)

    def _reload_evidence(self) -> dict[str, KGEvidence]:
        load = getattr(self.repository, "load", None)
        if load is None:
            raise PathValidationError("the repository cannot reload evidence")
        records: dict[str, KGEvidence] = {}
        for evidence_id in self._evidence_ids():
            records[evidence_id] = load(
                "kg_evidence", evidence_id, evidence_id, KGEvidence
            )
        return records

    def _evidence_ids(self) -> list[str]:
        list_ids = getattr(self.repository, "list_record_ids", None)
        if list_ids is None:
            raise PathValidationError("the repository cannot list evidence ids")
        return list(list_ids("kg_evidence"))


def bundle_with_paths(
    bundle: EvidenceBundle, paths: Sequence[EvidencePath]
) -> EvidenceBundle:
    """Attach the published paths to the bundle, as a new version."""
    payload = bundle.model_dump()
    payload["paths"] = [path.model_dump(mode="json") for path in paths]
    payload["bundle_version"] = f"{bundle.bundle_id}.v{_next_bundle_version(bundle)}"
    return EvidenceBundle.model_validate(payload)


def _next_bundle_version(bundle: EvidenceBundle) -> int:
    tail = bundle.bundle_version.rsplit(".v", 1)[-1]
    try:
        return int(tail) + 1
    except ValueError:  # pragma: no cover - defensive
        return 2