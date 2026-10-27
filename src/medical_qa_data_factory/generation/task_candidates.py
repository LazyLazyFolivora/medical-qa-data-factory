"""High-value task candidate sources built on the original generation stack."""
from __future__ import annotations

import hashlib

from pydantic import Field

from ..domain.common import ContractModel
from ..knowledge_graph.contracts import GraphEdge, GraphEntity

from .ctd_world_evaluation import (
    CtdWorldEvaluation,
    CtdWorldEvaluator,
)
from .logical_query import LogicalDirection, QueryConstraint, QueryStep
from .task_contracts import StudyContext, TaskCandidate


class ConstrainedIntersectionCandidateSource:
    """Propose an answer entity selected by two independently checkable facts.

    Given ``seed --r1--> candidate --r2--> constraint_entity``, the candidate
    asks for the entity satisfying both conditions.  The path is only a
    discovery hint; validation later enumerates every r1 neighbor and applies
    r2 to all of them in the complete snapshot.
    """

    version = "constrained-intersection-source-v1"

    def from_edges(
        self,
        *,
        seed_edge: GraphEdge,
        constraint_edge: GraphEdge,
        snapshot_id: str,
        primary_direction: LogicalDirection = LogicalDirection.FORWARD,
        constraint_direction: LogicalDirection = LogicalDirection.FORWARD,
    ) -> TaskCandidate:
        if seed_edge.provider_id != constraint_edge.provider_id:
            raise ValueError("candidate edges must come from the same provider")
        seed_entity, answer_entity = self._walk(
            seed_edge, primary_direction
        )
        constraint_start, constraint_terminal = self._walk(
            constraint_edge, constraint_direction
        )
        if answer_entity.canonical_id != constraint_start.canonical_id:
            raise ValueError("constraint edge must start at the proposed answer")
        identity = "|".join(
            (
                snapshot_id,
                seed_edge.edge_id,
                constraint_edge.edge_id,
                self.version,
            )
        )
        candidate_id = "candidate-constrained-" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:24]
        return TaskCandidate(
            candidate_id=candidate_id,
            seed_entity_id=seed_entity.canonical_id,
            question_type="constrained_entity",
            steps=[
                QueryStep(
                    position=0,
                    relation=seed_edge.relation,
                    direction=primary_direction,
                    target_entity_type=answer_entity.entity_type,
                )
            ],
            constraints=[
                QueryConstraint(
                    constraint_id=f"constraint-{constraint_edge.edge_id}",
                    steps=[
                        QueryStep(
                            position=0,
                            relation=constraint_edge.relation,
                            direction=constraint_direction,
                            target_entity_type=constraint_terminal.entity_type,
                        )
                    ],
                    terminal_entity_id=constraint_terminal.canonical_id,
                )
            ],
            proposed_answer_ids=[answer_entity.canonical_id],
            private_answer_names=[
                answer_entity.name,
                *answer_entity.aliases,
            ],
            answer_entity_type=answer_entity.entity_type,
            snapshot_id=snapshot_id,
            evidence_ids=[seed_edge.edge_id, constraint_edge.edge_id],
            public_entity_names={
                seed_entity.canonical_id: seed_entity.name,
                constraint_terminal.canonical_id: constraint_terminal.name,
            },
            discovery_source="structured_graph_intersection",
            generator_version=self.version,
        )

    @staticmethod
    def _walk(
        edge: GraphEdge, direction: LogicalDirection
    ) -> tuple[GraphEntity, GraphEntity]:
        if direction is LogicalDirection.FORWARD:
            return edge.head, edge.tail
        return edge.tail, edge.head


class CtdQualifierCounterfactual(ContractModel):
    qualifier: str = Field(min_length=1)
    world_answer_ids: list[str] = Field(default_factory=list)
    result_complete: bool
    discriminates: bool
    reason_codes: list[str] = Field(default_factory=list)


class CtdCandidateProposal(ContractModel):
    evaluation: CtdWorldEvaluation
    counterfactuals: list[CtdQualifierCounterfactual] = Field(
        default_factory=list
    )
    candidate: TaskCandidate | None = None
    reason_codes: list[str] = Field(default_factory=list)


class CtdStudyContextCandidateSource:
    """Discover a CTD task only after its complete competition report is known."""

    version = "ctd-study-context-source-v1"
    _RELAXABLE_FIELDS = (
        "population",
        "exposure_context",
        "dose_or_regimen",
        "exposure_duration",
        "study_type",
        "sample_type",
        "temporal_scope",
        "disease_category",
    )

    def __init__(
        self,
        evaluator: CtdWorldEvaluator,
        *,
        enforce_qualifier_value: bool = True,
    ) -> None:
        self.evaluator = evaluator
        self.enforce_qualifier_value = enforce_qualifier_value

    def propose(
        self,
        *,
        seed_entity_id: str,
        chemical_id: str,
        snapshot_id: str,
        context: StudyContext,
    ) -> CtdCandidateProposal:
        evaluation = self.evaluator.evaluate(
            chemical_id=chemical_id,
            snapshot_id=snapshot_id,
            context=context,
        )
        if not evaluation.result_complete:
            return CtdCandidateProposal(
                evaluation=evaluation,
                reason_codes=(
                    list(evaluation.reason_codes)
                    or ["unknown_competitor"]
                ),
            )
        answers = evaluation.world_answer_ids
        if not answers:
            return CtdCandidateProposal(
                evaluation=evaluation,
                reason_codes=["world_answer_missing"],
            )
        if len(answers) != 1:
            return CtdCandidateProposal(
                evaluation=evaluation,
                reason_codes=["cardinality_mismatch"],
            )

        counterfactuals = self._qualifier_counterfactuals(
            chemical_id=chemical_id,
            snapshot_id=snapshot_id,
            context=context,
            baseline_answers=set(answers),
        )
        if self.enforce_qualifier_value:
            if any(not item.result_complete for item in counterfactuals):
                return CtdCandidateProposal(
                    evaluation=evaluation,
                    counterfactuals=counterfactuals,
                    reason_codes=["incomplete_search"],
                )
            if not any(
                item.discriminates
                and item.qualifier != "disease_category"
                for item in counterfactuals
            ):
                return CtdCandidateProposal(
                    evaluation=evaluation,
                    counterfactuals=counterfactuals,
                    reason_codes=["shortcut_detected"],
                )

        answer_id = answers[0]
        matched = next(
            item
            for item in evaluation.competitor_outcomes
            if item.answer_id == answer_id
        )
        records = evaluation.answer_records[answer_id]
        private_names = sorted(
            {
                name
                for record in records
                for name in [record.disease_name, *record.disease_aliases]
                if name
            }
        )
        chemical_name = records[0].chemical_name
        candidate_id = "candidate-ctd-context-" + hashlib.sha256(
            "|".join(
                (
                    snapshot_id,
                    seed_entity_id,
                    chemical_id,
                    answer_id,
                    context.model_dump_json(),
                    self.version,
                )
            ).encode("utf-8")
        ).hexdigest()[:24]
        candidate = TaskCandidate(
            candidate_id=candidate_id,
            seed_entity_id=seed_entity_id,
            question_type="study_context_qualified_disease",
            steps=[
                QueryStep(
                    position=0,
                    relation="chemical_exposure_disease",
                    direction=LogicalDirection.FORWARD,
                    target_entity_type="disease",
                )
            ],
            proposed_answer_ids=[answer_id],
            private_answer_names=private_names,
            answer_entity_type="disease",
            snapshot_id=snapshot_id,
            evidence_ids=list(matched.evidence_ids),
            candidate_claim_ids=list(matched.claim_ids),
            public_study_context=context,
            public_entity_names={seed_entity_id: chemical_name},
            discovery_source="ctd_complete_world",
            generator_version=self.version,
        )
        return CtdCandidateProposal(
            evaluation=evaluation,
            counterfactuals=counterfactuals,
            candidate=candidate,
        )

    def _qualifier_counterfactuals(
        self,
        *,
        chemical_id: str,
        snapshot_id: str,
        context: StudyContext,
        baseline_answers: set[str],
    ) -> list[CtdQualifierCounterfactual]:
        counterfactuals: list[CtdQualifierCounterfactual] = []
        for field in self._RELAXABLE_FIELDS:
            value = getattr(context, field)
            if value in {None, ""}:
                continue
            relaxed = context.model_copy(
                update={
                    field: "" if field == "disease_category" else None
                }
            )
            counterfactuals.append(
                self._counterfactual(
                    qualifier=field,
                    chemical_id=chemical_id,
                    snapshot_id=snapshot_id,
                    context=relaxed,
                    baseline_answers=baseline_answers,
                )
            )
        for key, value in sorted(context.additional_qualifiers.items()):
            if not value:
                continue
            remaining = dict(context.additional_qualifiers)
            remaining.pop(key, None)
            relaxed = context.model_copy(
                update={"additional_qualifiers": remaining}
            )
            counterfactuals.append(
                self._counterfactual(
                    qualifier=f"additional_qualifiers.{key}",
                    chemical_id=chemical_id,
                    snapshot_id=snapshot_id,
                    context=relaxed,
                    baseline_answers=baseline_answers,
                )
            )
        return counterfactuals

    def _counterfactual(
        self,
        *,
        qualifier: str,
        chemical_id: str,
        snapshot_id: str,
        context: StudyContext,
        baseline_answers: set[str],
    ) -> CtdQualifierCounterfactual:
        evaluation = self.evaluator.evaluate(
            chemical_id=chemical_id,
            snapshot_id=snapshot_id,
            context=context,
        )
        answers = set(evaluation.world_answer_ids)
        return CtdQualifierCounterfactual(
            qualifier=qualifier,
            world_answer_ids=sorted(answers),
            result_complete=evaluation.result_complete,
            discriminates=(
                evaluation.result_complete and answers != baseline_answers
            ),
            reason_codes=list(evaluation.reason_codes),
        )
