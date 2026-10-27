"""Compile discovery-time candidates into strongly typed task specs."""
from __future__ import annotations

import hashlib
from typing import Any

from ..domain.common import utc_now
from ..evidence_enrichment.contracts import predicate_family_for_intent
from ..evidence_enrichment.models import QuestionIntent
from .logical_query import LogicalQuery
from .logical_query import LogicalDirection
from .task_contracts import (
    AcceptableEvidenceSet,
    AnswerContract,
    ClaimPattern,
    InformationRequirement,
    TaskCandidate,
    TaskSpec,
    ValidationEnvironment,
    StudyContext,
)


class TaskCompilationError(ValueError):
    """A candidate cannot be represented by the supported task semantics."""


class TaskCompiler:
    """Compile candidates without certifying their proposed answers."""

    version = "task-compiler-v2"

    def compile(
        self,
        candidate: TaskCandidate,
        environment: ValidationEnvironment,
    ) -> TaskSpec:
        if candidate.question_type == "study_context_qualified_disease":
            return self._compile_study_context(candidate, environment)
        if candidate.question_type != "constrained_entity":
            raise TaskCompilationError(
                "the first production slice only supports constrained entities"
            )
        if len(candidate.steps) != 1 or not candidate.constraints:
            raise TaskCompilationError(
                "a constrained entity needs one primary step and a constraint"
            )
        if any(len(item.steps) != 1 for item in candidate.constraints):
            raise TaskCompilationError(
                "multi-step constraints are not supported in the first slice"
            )
        if len(candidate.proposed_answer_ids) != 1:
            raise TaskCompilationError(
                "a single-answer candidate must propose exactly one entity"
            )
        if candidate.proposed_answer_ids[0] in candidate.public_entity_names:
            raise TaskCompilationError(
                "the answer entity cannot be exposed through public names"
            )
        if candidate.snapshot_id not in {
            item.snapshot_id for item in environment.providers
        }:
            raise TaskCompilationError(
                "candidate snapshot is outside the validation environment"
            )

        candidate_digest = candidate.semantic_digest()
        query = LogicalQuery(
            query_id="query-pending",
            path_id=f"candidate-path-{candidate.candidate_id}",
            path_version=candidate.generator_version,
            probe_id=f"candidate-probe-{candidate.candidate_id}",
            probe_digest=candidate_digest,
            start_entity_id=candidate.seed_entity_id,
            steps=list(candidate.steps),
            constraints=list(candidate.constraints),
            answer_entity_type=candidate.answer_entity_type,
            expected_cardinality="one",
            snapshot_id=candidate.snapshot_id,
            dependency_digest="pending",
            created_at=utc_now(),
        )
        dependency_digest = query.digest()
        query = query.model_copy(
            update={
                "query_id": f"query-{dependency_digest[:24]}",
                "dependency_digest": dependency_digest,
            }
        )

        answer_id = candidate.proposed_answer_ids[0]
        primary_step = candidate.steps[0]
        primary_subject, primary_object = (
            (candidate.seed_entity_id, answer_id)
            if primary_step.direction is LogicalDirection.FORWARD
            else (answer_id, candidate.seed_entity_id)
        )
        requirements = [
            InformationRequirement(
                requirement_id="req-primary",
                description="confirm the seed-to-answer relation",
                claim_pattern=ClaimPattern(
                    subject_id=primary_subject,
                    predicate_family=primary_step.relation,
                    object_id=primary_object,
                ),
            )
        ]
        for index, constraint in enumerate(candidate.constraints, start=1):
            constraint_step = constraint.steps[0]
            constraint_subject, constraint_object = (
                (answer_id, constraint.terminal_entity_id)
                if constraint_step.direction is LogicalDirection.FORWARD
                else (constraint.terminal_entity_id, answer_id)
            )
            requirements.append(
                InformationRequirement(
                    requirement_id=f"req-constraint-{index}",
                    description="confirm an answer-disambiguating constraint",
                    claim_pattern=ClaimPattern(
                        subject_id=constraint_subject,
                        predicate_family=constraint_step.relation,
                        object_id=constraint_object,
                    ),
                )
            )

        task_digest = hashlib.sha256(
            f"{candidate.candidate_id}|{query.dependency_digest}|{environment.environment_id}".encode(
                "utf-8"
            )
        ).hexdigest()[:24]
        return TaskSpec(
            task_id=f"task-{task_digest}",
            candidate_id=candidate.candidate_id,
            seed_entity_id=candidate.seed_entity_id,
            question_type=candidate.question_type,
            query=query,
            proposed_answer_contract=AnswerContract(
                answer_type="entity",
                cardinality="exactly_one",
                canonical_answer_ids=[answer_id],
                accepted_aliases=list(candidate.private_answer_names),
                scope_statement=environment.completeness_scope,
            ),
            information_requirements=requirements,
            acceptable_evidence_sets=[
                AcceptableEvidenceSet(
                    all_of=[item.requirement_id for item in requirements]
                )
            ],
            validation_environment=environment,
            candidate_evidence_ids=list(candidate.evidence_ids),
            public_entity_names=dict(candidate.public_entity_names),
            generator_version=candidate.generator_version,
        )

    def _compile_study_context(
        self,
        candidate: TaskCandidate,
        environment: ValidationEnvironment,
    ) -> TaskSpec:
        if candidate.public_study_context is None:
            raise TaskCompilationError(
                "a study-context candidate needs a public study context"
            )
        if len(candidate.steps) != 1 or candidate.constraints:
            raise TaskCompilationError(
                "a study-context disease task uses one semantic step"
            )
        if len(candidate.proposed_answer_ids) != 1:
            raise TaskCompilationError(
                "a study-context candidate must propose exactly one disease"
            )
        if candidate.answer_entity_type != "disease":
            raise TaskCompilationError(
                "a study-context candidate must ask for a disease"
            )
        if candidate.snapshot_id not in {
            item.snapshot_id for item in environment.providers
        }:
            raise TaskCompilationError(
                "candidate snapshot is outside the validation environment"
            )
        if candidate.proposed_answer_ids[0] in candidate.public_entity_names:
            raise TaskCompilationError(
                "the answer disease cannot be exposed through public names"
            )

        context = candidate.public_study_context
        semantic_digest = candidate.semantic_digest()
        query = LogicalQuery(
            query_id="query-pending",
            path_id=f"candidate-path-{candidate.candidate_id}",
            path_version=candidate.generator_version,
            probe_id=f"candidate-probe-{candidate.candidate_id}",
            probe_digest=semantic_digest,
            start_entity_id=candidate.seed_entity_id,
            steps=list(candidate.steps),
            answer_entity_type="disease",
            expected_cardinality="one",
            filters=context.model_dump(mode="json"),
            temporal_scope=context.temporal_scope,
            snapshot_id=candidate.snapshot_id,
            dependency_digest="pending",
            created_at=utc_now(),
        )
        dependency_digest = query.digest()
        query = query.model_copy(
            update={
                "query_id": f"query-{dependency_digest[:24]}",
                "dependency_digest": dependency_digest,
            }
        )
        answer_id = candidate.proposed_answer_ids[0]
        requirement = InformationRequirement(
            requirement_id=(
                "requirement-study-context-"
                + dependency_digest[:24]
            ),
            description=(
                "establish the public study-context relation to the "
                "proposed disease"
            ),
            claim_pattern=ClaimPattern(
                subject_id=candidate.seed_entity_id,
                predicate_family=predicate_family_for_intent(
                    QuestionIntent(context.relation_intent),
                    fallback_relation="chemical_disease_association",
                ),
                object_id=answer_id,
                qualifier_constraints=_study_qualifier_constraints(context),
            ),
            evidence_quality_rule=(
                "literature_required"
            ),
        )
        snapshot_scope = ", ".join(
            f"{item.provider_id}:{item.snapshot_id}"
            for item in sorted(
                environment.providers,
                key=lambda item: item.provider_id,
            )
        )
        return TaskSpec(
            task_id=(
                "task-study-context-"
                + hashlib.sha256(
                    f"{candidate.candidate_id}|{dependency_digest}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:24]
            ),
            candidate_id=candidate.candidate_id,
            seed_entity_id=candidate.seed_entity_id,
            question_type="study_context_qualified_disease",
            query=query,
            proposed_answer_contract=AnswerContract(
                answer_type="entity",
                cardinality="exactly_one",
                canonical_answer_ids=[answer_id],
                accepted_aliases=_candidate_answer_aliases(candidate),
                scope_statement=(
                    "within the declared public study context and frozen "
                    f"snapshots ({snapshot_scope})"
                ),
            ),
            information_requirements=[requirement],
            acceptable_evidence_sets=[
                AcceptableEvidenceSet(all_of=[requirement.requirement_id])
            ],
            validation_environment=environment,
            candidate_claim_ids=list(candidate.candidate_claim_ids),
            candidate_evidence_ids=list(candidate.evidence_ids),
            public_study_context=context,
            public_entity_names=dict(candidate.public_entity_names),
            generator_version=candidate.generator_version,
        )


def _study_qualifier_constraints(
    context: StudyContext,
) -> dict[str, Any]:
    constraints: dict[str, Any] = {
        "polarity": context.polarity,
        "disease_category": context.disease_category,
    }
    for key in (
        "population",
        "study_type",
        "sample_type",
    ):
        value = getattr(context, key)
        if value is not None:
            constraints[key] = value
    if context.exposure_context is not None:
        constraints["exposure_context"] = context.exposure_context
    if context.dose_or_regimen is not None:
        constraints["dose_exposure"] = context.dose_or_regimen
    duration = context.exposure_duration or context.temporal_scope
    if duration is not None:
        constraints["temporal_scope"] = duration
    constraints.update(context.additional_qualifiers)
    return constraints


def _candidate_answer_aliases(candidate: TaskCandidate) -> list[str]:
    return sorted(set(candidate.private_answer_names))
