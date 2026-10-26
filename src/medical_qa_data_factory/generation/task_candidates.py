"""High-value task candidate sources built on the original generation stack."""
from __future__ import annotations

import hashlib

from ..knowledge_graph.contracts import GraphEdge, GraphEntity
from .logical_query import LogicalDirection, QueryConstraint, QueryStep
from .task_contracts import TaskCandidate


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
