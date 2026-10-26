"""The acceptance run over the real graph, driven by the ReAct loop.

Step 11 of the real-generation-agent plan. The ``rule-based`` baseline is a
single-hop pipeline. This runner drives the bounded ReAct loop against the
same real graph, so the acceptance report can say whether multi-hop paths are
actually reachable rather than only whether the contracts exist.

The planner here is scripted: it asks for the neighbours of whatever it was
last told about, one hop at a time. That is enough to exercise the loop, the
policy gate and the executor against real data. It is not a
model, and the report says so.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain.common import utc_now
from ..generation.actions import ModelActionProposal, RunPolicy
from ..generation.executor import Executor
from ..generation.planner import PlannerContext
from ..generation.react_graph import ReactContext, build_react_graph
from ..knowledge_graph import provider_from_config
from ..knowledge_graph.contracts import EntityLookup, GraphEntity, NeighborQuery
from .manifest import EvalManifest, EvalSeed


class ScriptedHopPlanner:
    """A planner that walks outwards one hop at a time.

    It is deterministic and it is not a model. It exists so the acceptance
    report can measure the real graph's multi-hop reachability without a
    model in the loop.
    """

    def __init__(self, *, max_hops: int) -> None:
        self.max_hops = max_hops
        self.turn = 0
        self.depth_by_entity: dict[str, int] = {}
        self.queried: set[str] = set()
        self._last_subject: str | None = None
        self._observed_count = 0

    def propose(
        self, context: PlannerContext, response_model: type[Any]
    ) -> ModelActionProposal:
        self.turn += 1
        self.depth_by_entity.setdefault(context.seed_entity_id, 0)
        new_observations = context.observations[self._observed_count :]
        for observation in new_observations:
            if self._last_subject is None:
                continue
            next_depth = self.depth_by_entity[self._last_subject] + 1
            for entity_id in observation.get("discovered_entity_ids") or []:
                self.depth_by_entity.setdefault(entity_id, next_depth)
        self._observed_count = len(context.observations)

        target_reached = max(
            self.depth_by_entity.values(), default=0
        ) >= self.max_hops
        candidates = (
            []
            if target_reached
            else [
                (entity_id, depth)
                for entity_id, depth in self.depth_by_entity.items()
                if depth < self.max_hops and entity_id not in self.queried
            ]
        )
        # Prefer the deepest available entity so the bounded acceptance run
        # actually exercises the requested hop depth instead of spending its
        # entire budget on a high-degree seed's first layer.
        subject = (
            max(candidates, key=lambda item: item[1])[0]
            if candidates
            else None
        )
        cited = (
            [context.observations[-1]["observation_id"]]
            if context.observations
            else []
        )
        if subject is None:
            return ModelActionProposal(
                action={
                    "action_type": "finish",
                    "stop_reason": "evidence_sufficient",
                },
                expected_information="the reachable set is exhausted",
                evidence_gap="none",
                cited_observation_ids=cited,
            )
        self.queried.add(subject)
        self._last_subject = subject
        return ModelActionProposal(
            action={
                "action_type": "query_neighbors",
                "subject_entity_id": subject,
                "direction": "both",
                "max_results": 50,
            },
            expected_information=f"the neighbours of {subject}",
            evidence_gap="the neighbour set is unknown",
            cited_observation_ids=cited,
        )

    @property
    def reached_hops(self) -> list[int]:
        return sorted({depth for depth in self.depth_by_entity.values() if depth >= 2})


class _AcceptanceKGPort:
    """Translate sealed executor requests into provider-native queries."""

    def __init__(self, provider: Any, seed: GraphEntity) -> None:
        self.provider = provider
        self.entities: dict[str, GraphEntity] = {seed.canonical_id: seed}
        self.calls: list[dict[str, Any]] = []

    def neighbors(self, request: dict[str, Any]) -> tuple[list[Any], str]:
        subject_id = str(request["subject_entity_id"])
        entity = self.entities[subject_id]
        result = self.provider.neighbors(
            NeighborQuery(
                entity=entity,
                direction=request.get("direction", "both"),
                relations=list(request.get("relations") or []),
                target_types=list(request.get("target_types") or []),
                limit=int(request.get("max_results", 50)),
                cursor=request.get("cursor"),
            )
        )
        self.calls.append(dict(request))
        for edge in result.edges:
            self.entities[edge.head.canonical_id] = edge.head
            self.entities[edge.tail.canonical_id] = edge.tail
        return list(result.edges), result.trace.query_id


@dataclass
class ReactAcceptance:
    """What the ReAct run reached."""

    seed_id: str
    canonical_name: str
    status: str = "not_run"
    stop_reason: str | None = None
    #: The hop depths reached through observation-dependent expansion.
    path_hop_counts: list[int] = field(default_factory=list)
    #: The entities the run discovered.
    discovered_entity_count: int = 0
    #: The number of tool calls the run made.
    tool_calls: int = 0
    error: str | None = None


def run_react_acceptance(
    manifest: EvalManifest,
    *,
    provider_id: str = "primekg",
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Drive the ReAct loop over the real graph for every seed."""
    outcomes: list[ReactAcceptance] = []
    provider = provider_from_config(provider_id)
    try:
        for seed in manifest.seeds:
            outcomes.append(_run_seed(provider, seed, provider_id=provider_id))
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    payload = {
        "manifest_id": manifest.manifest_id,
        "manifest_hash": manifest.manifest_hash(),
        "mode": "react-scripted",
        "finished_at": utc_now().isoformat(),
        "planner": "scripted (not a model)",
        "outcomes": [
            {
                "seed_id": item.seed_id,
                "canonical_name": item.canonical_name,
                "status": item.status,
                "stop_reason": item.stop_reason,
                "path_hop_counts": list(item.path_hop_counts),
                "max_hop_count": max(item.path_hop_counts, default=0),
                "discovered_entity_count": item.discovered_entity_count,
                "tool_calls": item.tool_calls,
                "error": item.error,
            }
            for item in outcomes
        ],
    }
    if report_path is not None:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return payload


def _run_seed(
    provider: Any,
    seed: EvalSeed,
    *,
    provider_id: str,
) -> ReactAcceptance:
    outcome = ReactAcceptance(
        seed_id=seed.seed_id, canonical_name=seed.canonical_name
    )
    try:
        resolution = provider.resolve_entity(
            EntityLookup(
                mention=seed.canonical_name,
                native_id=seed.native_id,
                entity_types=[seed.entity_type],
            )
        )
        entity = resolution.selected
        if entity is None:
            outcome.status = "failed"
            outcome.error = "the seed did not resolve"
            return outcome
        planner = ScriptedHopPlanner(max_hops=seed.max_hops)
        kg = _AcceptanceKGPort(provider, entity)
        tool_budget = max(seed.max_hops + 1, 2)
        policy = RunPolicy(
            max_turns=tool_budget + 1,
            max_tool_calls=tool_budget,
            remaining_tool_calls=tool_budget,
            max_hops=seed.max_hops,
            available_tool_names=("kg_neighbors", "none"),
            has_evidence=False,
        )
        graph = build_react_graph(
            ReactContext(
                planner=planner,
                executor=Executor(kg=kg),
                policy=policy,
                run_id=f"react-acceptance.{provider_id}.{seed.seed_id}",
                episode_id=f"react-acceptance.{seed.seed_id}",
                seed_entity_id=entity.canonical_id,
                seed_entity_name=entity.name,
                available_tools=("kg_neighbors",),
                max_turns=tool_budget + 1,
            )
        )
        final = graph.invoke(
            graph.initial_state(
                {
                    "task_id": f"react-acceptance.{seed.seed_id}",
                    "seed_id": seed.seed_id,
                }
            )
        )
        outcome.path_hop_counts = planner.reached_hops
        outcome.discovered_entity_count = len(kg.entities)
        outcome.tool_calls = len(kg.calls)
        outcome.status = "completed"
        outcome.stop_reason = final.get("stop_reason")
    except Exception as exc:
        outcome.status = "failed"
        outcome.error = f"{type(exc).__name__}: {exc}"
    return outcome
