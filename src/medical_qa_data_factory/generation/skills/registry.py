"""Deterministic registry and retrieval for agent skills."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..contracts import (
    CtdAction,
    ExpandAction,
    ExplorationAction,
    ExplorationActionType,
    NeighborAction,
    LiteratureAction,
)
from .contracts import (
    SkillAvailability,
    SkillCostEstimate,
    SkillDefinition,
    SkillInvocation,
    SkillRuntimeContext,
    SkillSelection,
)


SkillActionBuilder = Callable[[dict[str, Any]], ExplorationAction]


def _skill_argument_schema(action_model: type[Any]) -> dict[str, Any]:
    """Expose only model-selectable fields, not server-fixed action identity."""

    schema = action_model.model_json_schema()
    properties = dict(schema.get("properties", {}))
    properties.pop("action_type", None)
    schema["properties"] = properties
    schema["required"] = [
        item for item in schema.get("required", []) if item != "action_type"
    ]
    return schema


class SkillUnavailableError(ValueError):
    """Raised when a model selects a skill that is not usable now."""


class SkillRegistry:
    """Registry of skill metadata and server-owned action compilers."""

    def __init__(self) -> None:
        self._definitions: dict[str, SkillDefinition] = {}
        self._builders: dict[str, SkillActionBuilder] = {}

    def register(
        self,
        definition: SkillDefinition,
        builder: SkillActionBuilder,
    ) -> None:
        if definition.skill_id in self._definitions:
            raise ValueError(f"duplicate skill ID: {definition.skill_id}")
        self._definitions[definition.skill_id] = definition
        self._builders[definition.skill_id] = builder

    def definitions(self) -> list[SkillDefinition]:
        return [self._definitions[key] for key in sorted(self._definitions)]

    def availability_reasons(
        self,
        definition: SkillDefinition,
        runtime: SkillRuntimeContext,
    ) -> list[str]:
        reasons: list[str] = []
        if runtime.remaining_tool_calls < definition.cost.tool_calls:
            reasons.append("tool_budget_exhausted")
        if (
            definition.action_type
            in {
                ExplorationActionType.QUERY_NEIGHBORS,
                ExplorationActionType.EXPAND_ENTITY,
                ExplorationActionType.QUERY_PATHS,
            }
            and runtime.remaining_edge_budget == 0
        ):
            reasons.append("edge_budget_exhausted")
        missing_tools = sorted(
            set(definition.required_tool_names)
            - set(runtime.available_tool_names)
        )
        if missing_tools:
            reasons.append("missing_tools:" + ",".join(missing_tools))
        if (
            runtime.allowed_action_types
            and definition.action_type.value not in runtime.allowed_action_types
        ):
            reasons.append("action_not_allowed")
        missing_artifacts = sorted(
            set(definition.required_artifact_types)
            - set(runtime.artifact_types)
        )
        if missing_artifacts:
            reasons.append(
                "missing_artifacts:" + ",".join(missing_artifacts)
            )
        if (
            definition.seed_entity_types
            and runtime.seed_entity_type not in definition.seed_entity_types
        ):
            reasons.append("seed_entity_type_not_supported")
        if (
            runtime.gap_types
            and definition.addressed_gap_types
            and not set(runtime.gap_types).intersection(
                definition.addressed_gap_types
            )
        ):
            reasons.append("does_not_address_current_gap")
        return reasons

    def available_skills(
        self, runtime: SkillRuntimeContext
    ) -> list[SkillAvailability]:
        available: list[SkillAvailability] = []
        for definition in self.definitions():
            if self.availability_reasons(definition, runtime):
                continue
            available.append(
                SkillAvailability(
                    skill_id=definition.skill_id,
                    version=definition.version,
                    summary=definition.summary,
                    action_type=definition.action_type,
                    input_schema=definition.input_schema,
                    cost=definition.cost,
                    addressed_gap_types=definition.addressed_gap_types,
                )
            )
        return available

    def prepare_invocation(
        self,
        selection: SkillSelection,
        *,
        runtime: SkillRuntimeContext,
        episode_id: str,
        turn: int,
        state_digest: str,
    ) -> SkillInvocation:
        try:
            definition = self._definitions[selection.skill_id]
            builder = self._builders[selection.skill_id]
        except KeyError as exc:
            raise SkillUnavailableError(
                f"unknown_skill:{selection.skill_id}"
            ) from exc
        reasons = self.availability_reasons(definition, runtime)
        if reasons:
            raise SkillUnavailableError(";".join(reasons))
        action = builder(dict(selection.arguments))
        if action.action_type is not definition.action_type:
            raise ValueError(
                "skill action compiler returned a different action type"
            )
        return SkillInvocation.create(
            episode_id=episode_id,
            turn=turn,
            state_digest=state_digest,
            definition=definition,
            arguments=dict(selection.arguments),
            action=action,
        )


def _neighbor(arguments: dict[str, Any]) -> ExplorationAction:
    return NeighborAction.model_validate(
        {
            **arguments,
            "action_type": ExplorationActionType.QUERY_NEIGHBORS,
        }
    )


def _expand(arguments: dict[str, Any]) -> ExplorationAction:
    return ExpandAction.model_validate(
        {
            **arguments,
            "action_type": ExplorationActionType.EXPAND_ENTITY,
        }
    )


def _ctd(arguments: dict[str, Any]) -> ExplorationAction:
    return CtdAction.model_validate(
        {**arguments, "action_type": ExplorationActionType.QUERY_CTD}
    )


def _literature(arguments: dict[str, Any]) -> ExplorationAction:
    return LiteratureAction.model_validate(
        {
            **arguments,
            "action_type": ExplorationActionType.RETRIEVE_LITERATURE,
        }
    )


def build_default_skill_registry() -> SkillRegistry:
    """Build the first skill catalog over existing PrimeKG actions."""

    registry = SkillRegistry()
    registry.register(
        SkillDefinition(
            skill_id="ctd-direct-evidence-lookup",
            version="ctd-direct-evidence-lookup-v1",
            summary=(
                "Look up bounded chemical-disease records and direct-evidence "
                "labels in the pinned CTD snapshot."
            ),
            action_type=ExplorationActionType.QUERY_CTD,
            required_tool_names=["ctd_lookup"],
            produced_artifact_types=["ctd_evidence"],
            addressed_gap_types=[
                "answer_completeness",
                "claim_support",
                "ctd_direct_evidence_missing",
                "chemical_disease_relation_unknown",
                "entity_resolution",
            ],
            seed_entity_types=["exposure"],
            input_schema=_skill_argument_schema(CtdAction),
            cost=SkillCostEstimate(tool_calls=1, relative_cost="low"),
        ),
        _ctd,
    )
    registry.register(
        SkillDefinition(
            skill_id="pubmed-evidence-search",
            version="pubmed-evidence-search-v1",
            summary=(
                "Search and read back PubMed evidence for an open evidence "
                "need derived from a known graph relation."
            ),
            action_type=ExplorationActionType.RETRIEVE_LITERATURE,
            required_tool_names=["literature_search"],
            required_artifact_types=["kg_evidence"],
            produced_artifact_types=["literature_evidence"],
            addressed_gap_types=[
                "claim_support",
                "contradiction",
                "literature_support_missing",
                "pubmed_evidence_missing",
                "qualifier",
            ],
            input_schema=_skill_argument_schema(LiteratureAction),
            cost=SkillCostEstimate(
                tool_calls=1,
                max_records=20,
                relative_cost="medium",
            ),
        ),
        _literature,
    )
    registry.register(
        SkillDefinition(
            skill_id="primekg-entity-expansion",
            version="primekg-entity-expansion-v1",
            summary="Expand one known entity through bounded PrimeKG edges.",
            action_type=ExplorationActionType.EXPAND_ENTITY,
            required_tool_names=["kg_neighbors"],
            produced_artifact_types=["kg_evidence"],
            input_schema=_skill_argument_schema(ExpandAction),
            cost=SkillCostEstimate(tool_calls=1, relative_cost="low"),
        ),
        _expand,
    )
    registry.register(
        SkillDefinition(
            skill_id="primekg-neighbor-exploration",
            version="primekg-neighbor-exploration-v1",
            summary=(
                "Query a bounded, filtered PrimeKG neighborhood for one "
                "known entity."
            ),
            action_type=ExplorationActionType.QUERY_NEIGHBORS,
            required_tool_names=["kg_neighbors"],
            produced_artifact_types=["kg_evidence"],
            input_schema=_skill_argument_schema(NeighborAction),
            cost=SkillCostEstimate(tool_calls=1, relative_cost="low"),
        ),
        _neighbor,
    )
    return registry
