"""Versioned contracts for the evidence-first objective-question pipeline.

These contracts encode authority boundaries rather than workflow convenience:
sources produce materials, models propose candidates, verification resolves
answers, writers realize frozen contracts, and deterministic code alone can
allow release.
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Annotated, Any, Literal, Sequence

from pydantic import Field, model_validator

from ..domain.common import ContractModel, UtcDateTime
from .evidence_provider import ProviderTerminalStatus


OBJECTIVE_QUESTION_SCHEMA_VERSION = "objective-question-v1"
OBJECTIVE_ACCEPTANCE_SCHEMA_VERSION = "objective-question-acceptance-v1"

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ObjectiveContractModel(ContractModel):
    schema_version: str = OBJECTIVE_QUESTION_SCHEMA_VERSION

    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class ExecutionStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class DomainOutcome(str, Enum):
    SUPPORTED = "SUPPORTED"
    CONFLICT = "CONFLICT"
    INSUFFICIENT = "INSUFFICIENT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class VerificationVerdict(str, Enum):
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    INDETERMINATE = "INDETERMINATE"
    NOT_EMITTED = "NOT_EMITTED"


class AnswerStatus(str, Enum):
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    INDETERMINATE = "INDETERMINATE"


class ObjectiveVerdict(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"
    INDETERMINATE = "INDETERMINATE"


class AnswerCardinality(str, Enum):
    EXACTLY_ONE = "EXACTLY_ONE"
    ONE_OR_MORE = "ONE_OR_MORE"
    EXACT_SET = "EXACT_SET"


class CompletenessBasis(str, Enum):
    CANONICAL_IDENTITY = "CANONICAL_IDENTITY"
    AUTHORITATIVE_DEFINITION = "AUTHORITATIVE_DEFINITION"
    AUTHORITATIVE_ENUMERATION = "AUTHORITATIVE_ENUMERATION"
    EXPLICIT_PUBLIC_OPTIONS = "EXPLICIT_PUBLIC_OPTIONS"
    DETERMINISTIC_CALCULATION = "DETERMINISTIC_CALCULATION"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"


class EntityResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"


class CoverageStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


class DeduplicationStatus(str, Enum):
    IN_FLIGHT = "IN_FLIGHT"
    COMMITTED = "COMMITTED"
    RETRYABLE_FAILED = "RETRYABLE_FAILED"
    UNKNOWN = "UNKNOWN"
    TERMINAL_FAILED = "TERMINAL_FAILED"


class HypothesisState(str, Enum):
    PROPOSED = "PROPOSED"
    VERIFICATION_PLANNED = "VERIFICATION_PLANNED"
    VERIFYING = "VERIFYING"
    ANSWER_VERIFIED = "ANSWER_VERIFIED"
    QUESTION_REALIZED = "QUESTION_REALIZED"
    RELEASE_REVIEWED = "RELEASE_REVIEWED"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    INDETERMINATE = "INDETERMINATE"


class RunState(str, Enum):
    CREATED = "CREATED"
    MATERIALS_COLLECTED = "MATERIALS_COLLECTED"
    CLAIMS_PROPOSED = "CLAIMS_PROPOSED"
    HYPOTHESES_PROPOSED = "HYPOTHESES_PROPOSED"
    CANDIDATES_PROCESSING = "CANDIDATES_PROCESSING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ZERO_QUESTIONS = "COMPLETED_WITH_ZERO_QUESTIONS"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    VERSION_INCOMPATIBLE = "VERSION_INCOMPATIBLE"
    CANCELLED = "CANCELLED"


_HYPOTHESIS_TRANSITIONS: dict[HypothesisState, frozenset[HypothesisState]] = {
    HypothesisState.PROPOSED: frozenset(
        {
            HypothesisState.VERIFICATION_PLANNED,
            HypothesisState.REJECTED,
            HypothesisState.INDETERMINATE,
        }
    ),
    HypothesisState.VERIFICATION_PLANNED: frozenset(
        {
            HypothesisState.VERIFYING,
            HypothesisState.REJECTED,
            HypothesisState.INDETERMINATE,
        }
    ),
    HypothesisState.VERIFYING: frozenset(
        {
            HypothesisState.ANSWER_VERIFIED,
            HypothesisState.REJECTED,
            HypothesisState.INDETERMINATE,
        }
    ),
    HypothesisState.ANSWER_VERIFIED: frozenset(
        {
            HypothesisState.QUESTION_REALIZED,
            HypothesisState.REJECTED,
            HypothesisState.INDETERMINATE,
        }
    ),
    HypothesisState.QUESTION_REALIZED: frozenset(
        {
            HypothesisState.RELEASE_REVIEWED,
            HypothesisState.REJECTED,
            HypothesisState.INDETERMINATE,
        }
    ),
    HypothesisState.RELEASE_REVIEWED: frozenset(
        {
            HypothesisState.PUBLISHED,
            HypothesisState.REJECTED,
            HypothesisState.INDETERMINATE,
        }
    ),
    HypothesisState.PUBLISHED: frozenset(),
    HypothesisState.REJECTED: frozenset(),
    HypothesisState.INDETERMINATE: frozenset(),
}


def transition_hypothesis(
    current: HypothesisState, target: HypothesisState
) -> HypothesisState:
    if target not in _HYPOTHESIS_TRANSITIONS[current]:
        raise ValueError(f"illegal hypothesis transition: {current.value} -> {target.value}")
    return target


def aggregate_generation_run(
    hypothesis_states: Sequence[HypothesisState],
    *,
    no_hypotheses: bool = False,
    recovery_required: bool = False,
    version_incompatible: bool = False,
    cancelled: bool = False,
) -> RunState:
    if recovery_required:
        return RunState.RECOVERY_REQUIRED
    if version_incompatible:
        return RunState.VERSION_INCOMPATIBLE
    if cancelled:
        return RunState.CANCELLED
    if HypothesisState.PUBLISHED in hypothesis_states:
        return RunState.COMPLETED
    terminal = {HypothesisState.REJECTED, HypothesisState.INDETERMINATE}
    if no_hypotheses or (
        hypothesis_states and all(item in terminal for item in hypothesis_states)
    ):
        return RunState.COMPLETED_WITH_ZERO_QUESTIONS
    return RunState.CANDIDATES_PROCESSING


class EvidenceMaterial(ObjectiveContractModel):
    material_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    retrieved_at: UtcDateTime
    content_hash: Digest
    raw_content_ref: str = Field(min_length=1)
    material_type: str = Field(min_length=1)
    retrieval_request_id: str = Field(min_length=1)
    terminal_status: ProviderTerminalStatus


class EvidenceAnchor(ObjectiveContractModel):
    anchor_id: str = Field(min_length=1)
    material_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    section_or_field: str = Field(min_length=1)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    quoted_text: str = Field(min_length=1)
    quote_hash: Digest

    @model_validator(mode="after")
    def offsets_and_hash_are_valid(self) -> "EvidenceAnchor":
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must be greater than start_offset")
        if self.end_offset - self.start_offset != len(self.quoted_text):
            raise ValueError("quoted_text length must match anchor offsets")
        if text_digest(self.quoted_text) != self.quote_hash:
            raise ValueError("quote_hash does not match quoted_text")
        return self


class EntityResolution(ObjectiveContractModel):
    resolution_id: str = Field(min_length=1)
    mention: str = Field(min_length=1)
    namespace: str | None = None
    canonical_id: str | None = None
    canonical_name: str | None = None
    species: str | None = None
    aliases: list[str] = Field(default_factory=list)
    authority: str | None = None
    resolution_status: EntityResolutionStatus
    candidate_entities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def resolution_shape_is_valid(self) -> "EntityResolution":
        if self.resolution_status is EntityResolutionStatus.RESOLVED:
            if not self.canonical_id or not self.canonical_name:
                raise ValueError(
                    "RESOLVED entities require canonical_id and canonical_name"
                )
        if self.resolution_status is EntityResolutionStatus.AMBIGUOUS:
            if len(self.candidate_entities) < 2:
                raise ValueError(
                    "AMBIGUOUS entities require at least two candidates"
                )
        return self


class CandidateClaim(ObjectiveContractModel):
    claim_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object_or_value: str = Field(min_length=1)
    qualifiers: dict[str, str] = Field(default_factory=dict)
    claim_strength: str = Field(min_length=1)
    polarity: Literal["POSITIVE", "NEGATIVE", "UNKNOWN"]
    supporting_anchor_ids: list[str] = Field(min_length=1)
    contradicting_anchor_ids: list[str] = Field(default_factory=list)
    extraction_model_version: str = Field(min_length=1)
    status: Literal["PROPOSED"] = "PROPOSED"


class QuestionHypothesis(ObjectiveContractModel):
    hypothesis_id: str = Field(min_length=1)
    candidate_claim_ids: list[str] = Field(min_length=1)
    question_intent: str = Field(min_length=1)
    proposed_public_scope: str = Field(min_length=1)
    proposed_answer_type: str = Field(min_length=1)
    proposed_answer_values: list[str] = Field(default_factory=list)
    proposed_cardinality: AnswerCardinality
    discovery_anchor_ids: list[str] = Field(min_length=1)
    risk_flags: list[str] = Field(default_factory=list)
    status: Literal["PROPOSED"] = "PROPOSED"


class BlindAnswerQuery(ObjectiveContractModel):
    query_id: str = Field(min_length=1)
    question_intent: str = Field(min_length=1)
    public_scope: str = Field(min_length=1)
    answer_type: str = Field(min_length=1)
    answer_cardinality: AnswerCardinality
    required_relation_strength: str = Field(min_length=1)

    def input_digest(self) -> str:
        return self.digest()


class VerificationPlan(ObjectiveContractModel):
    plan_id: str = Field(min_length=1)
    hypothesis_id: str = Field(min_length=1)
    blind_answer_query_digest: Digest
    expected_attempt_ids: list[str] = Field(min_length=1)
    allowed_provider_ids: list[str] = Field(min_length=1)
    competing_answer_strategy: str = Field(min_length=1)
    contradiction_strategy: str = Field(min_length=1)
    budget: dict[str, Any] = Field(min_length=1)
    stop_conditions: list[str] = Field(min_length=1)


class VerificationAttempt(ObjectiveContractModel):
    attempt_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    started_at: UtcDateTime
    finished_at: UtcDateTime | None = None
    terminal_status: ProviderTerminalStatus | None = None
    error_category: str | None = None
    material_ids: list[str] = Field(default_factory=list)
    anchor_ids: list[str] = Field(default_factory=list)


class VerificationCoverageReport(ObjectiveContractModel):
    coverage_report_id: str = Field(min_length=1)
    expected_attempt_ids: list[str] = Field(min_length=1)
    terminal_attempt_ids: list[str] = Field(default_factory=list)
    successful_attempt_ids: list[str] = Field(default_factory=list)
    failed_attempt_ids: list[str] = Field(default_factory=list)
    unknown_attempt_ids: list[str] = Field(default_factory=list)
    budget_exhausted: bool
    competing_answer_queries: int = Field(ge=0)
    contradiction_queries: int = Field(ge=0)
    coverage_status: CoverageStatus

    @model_validator(mode="after")
    def coverage_shape_is_valid(self) -> "VerificationCoverageReport":
        expected = set(self.expected_attempt_ids)
        terminal = set(self.terminal_attempt_ids)
        successful = set(self.successful_attempt_ids)
        failed = set(self.failed_attempt_ids)
        unknown = set(self.unknown_attempt_ids)
        if terminal - expected or successful - expected or failed - expected or unknown - expected:
            raise ValueError("all coverage attempt IDs must belong to expected attempts")
        if successful & failed or successful & unknown or failed & unknown:
            raise ValueError("attempt terminal classes must not overlap")
        complete = (
            terminal == expected
            and successful == expected
            and not failed
            and not unknown
            and not self.budget_exhausted
        )
        if self.coverage_status is CoverageStatus.COMPLETE and not complete:
            raise ValueError(
                "COMPLETE coverage requires every expected attempt to succeed"
            )
        return self


class AnswerResolution(ObjectiveContractModel):
    resolution_id: str = Field(min_length=1)
    hypothesis_id: str = Field(min_length=1)
    status: AnswerStatus
    verified_answer_values: list[str] = Field(default_factory=list)
    answer_cardinality: AnswerCardinality
    public_scope: str = Field(min_length=1)
    supporting_anchor_ids: list[str] = Field(default_factory=list)
    contradicting_anchor_ids: list[str] = Field(default_factory=list)
    competing_answers_checked: bool
    conflict_checked: bool
    completeness_basis: CompletenessBasis
    verification_sources: list[str] = Field(default_factory=list)
    verification_policy_version: str = Field(min_length=1)
    blind_answer_query_digest: Digest
    verification_plan_id: str = Field(min_length=1)
    verification_coverage_report_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def verified_resolution_is_publishable(self) -> "AnswerResolution":
        if self.status is not AnswerStatus.VERIFIED:
            return self
        if not self.verified_answer_values:
            raise ValueError("VERIFIED resolutions require answer values")
        if not self.supporting_anchor_ids:
            raise ValueError(
                "VERIFIED resolutions require supporting_anchor_ids"
            )
        if not self.competing_answers_checked:
            raise ValueError(
                "VERIFIED resolutions require competing_answers_checked=true"
            )
        if not self.conflict_checked:
            raise ValueError("VERIFIED resolutions require conflict_checked=true")
        if not self.verification_sources:
            raise ValueError("VERIFIED resolutions require verification_sources")
        if self.completeness_basis is CompletenessBasis.NOT_ESTABLISHED:
            raise ValueError(
                "VERIFIED resolutions require an established completeness_basis"
            )
        return self


class FrozenQuestionContract(ObjectiveContractModel):
    contract_id: str = Field(min_length=1)
    question_intent: str = Field(min_length=1)
    public_scope: str = Field(min_length=1)
    answer_type: str = Field(min_length=1)
    answer_values: list[str] = Field(min_length=1)
    answer_cardinality: AnswerCardinality
    claim_strength: str = Field(min_length=1)
    required_public_conditions: list[str] = Field(default_factory=list)
    allowed_wording_constraints: list[str] = Field(default_factory=list)
    forbidden_semantic_upgrades: list[str] = Field(default_factory=list)
    resolution_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def cardinality_matches_answers(self) -> "FrozenQuestionContract":
        if (
            self.answer_cardinality is AnswerCardinality.EXACTLY_ONE
            and len(self.answer_values) != 1
        ):
            raise ValueError("EXACTLY_ONE contracts require exactly one answer")
        return self


class DerivationEdge(ObjectiveContractModel):
    source_artifact_id: str = Field(min_length=1)
    target_artifact_id: str = Field(min_length=1)
    relation: str = Field(min_length=1)
    rule_or_prompt_version: str = Field(min_length=1)
    input_digest: Digest
    output_digest: Digest


class StructuredDerivationTrace(ObjectiveContractModel):
    trace_id: str = Field(min_length=1)
    hypothesis_id: str = Field(min_length=1)
    edges: list[DerivationEdge] = Field(min_length=1)


class PublicQuestionAuditResult(ObjectiveContractModel):
    task_id: str = Field(min_length=1)
    public_question_digest: Digest
    self_contained: bool
    source_independent: bool
    asks_underlying_fact: bool
    entity_unambiguous: bool
    cardinality_clear: bool
    verdict: ObjectiveVerdict


class AnswerEvidenceAuditResult(ObjectiveContractModel):
    task_id: str = Field(min_length=1)
    question_answer_contract_digest: Digest
    independently_supported: bool
    unique_or_closed: bool
    contradiction_checked: bool
    relation_strength_preserved: bool
    verdict: ObjectiveVerdict


class ObjectiveValidationReport(ObjectiveContractModel):
    report_id: str = Field(min_length=1)
    question_id: str = Field(min_length=1)
    verdict: ObjectiveVerdict
    release_allowed: bool
    primary_reason: str = Field(min_length=1)
    secondary_reasons: list[str] = Field(default_factory=list)
    public_question_audit_id: str = Field(min_length=1)
    answer_audit_id: str = Field(min_length=1)
    blocking_findings: list[str] = Field(default_factory=list)
    evidence_used: list[str] = Field(default_factory=list)
    validator_version: str = Field(min_length=1)
    input_digest: Digest

    @model_validator(mode="after")
    def release_matches_verdict(self) -> "ObjectiveValidationReport":
        is_pass = self.verdict is ObjectiveVerdict.PASS
        if self.release_allowed != is_pass:
            raise ValueError(
                "release_allowed must be true exactly when verdict is PASS"
            )
        if self.release_allowed and self.blocking_findings:
            raise ValueError("PASS reports cannot contain blocking findings")
        return self


class EvidenceNeed(ObjectiveContractModel):
    evidence_need_id: str = Field(min_length=1)
    origin_stage: str = Field(min_length=1)
    target_claim_or_hypothesis_id: str = Field(min_length=1)
    information_required: str = Field(min_length=1)
    allowed_providers: list[str] = Field(min_length=1)
    budget: dict[str, Any] = Field(min_length=1)
    max_rounds: int = Field(ge=1)
    stop_conditions: list[str] = Field(min_length=1)


class RetrievalAttempt(ObjectiveContractModel):
    retrieval_attempt_id: str = Field(min_length=1)
    evidence_need_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    operation_key: str = Field(min_length=1)
    request_digest: Digest
    terminal_status: ProviderTerminalStatus
    error_category: str | None = None
    material_ids: list[str] = Field(default_factory=list)
    anchor_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def failure_has_error_category(self) -> "RetrievalAttempt":
        failed = self.terminal_status in {
            ProviderTerminalStatus.FAILED,
            ProviderTerminalStatus.UNKNOWN,
        }
        if failed and not self.error_category:
            raise ValueError(
                "error_category is required for FAILED or UNKNOWN attempts"
            )
        return self


class GenerationGoal(ObjectiveContractModel):
    run_id: str = Field(min_length=1)
    target_entity: str = Field(min_length=1)
    question_count_upper_bound: int = Field(ge=0)
    difficulty_targets: list[str] = Field(default_factory=list)
    style_targets: list[str] = Field(default_factory=list)
    allowed_fact_types: list[str] = Field(default_factory=list)
    run_budget: dict[str, Any] = Field(min_length=1)
    run_manifest_id: str = Field(min_length=1)
    runtime_bundle_id: str = Field(min_length=1)


class SubagentTask(ObjectiveContractModel):
    subtask_id: str = Field(min_length=1)
    subtask_revision: int = Field(ge=0)
    child_slot_id: str = Field(min_length=1)
    parent_decision_id: str = Field(min_length=1)
    subagent_role: str = Field(min_length=1)
    role_version: str = Field(min_length=1)
    goal: dict[str, Any] = Field(min_length=1)
    input_artifact_ids: list[str] = Field(default_factory=list)
    input_digest: Digest
    runtime_bundle_id: str = Field(min_length=1)
    expected_state: str = Field(min_length=1)
    expected_state_revision: int = Field(ge=0)
    operation_key: str = Field(min_length=1)
    initial_attempt_id: str = Field(min_length=1)
    visible_field_policy: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    budget: dict[str, Any] = Field(min_length=1)
    stop_conditions: list[str] = Field(min_length=1)
    expected_output_schema: str = Field(min_length=1)


class SubagentResult(ObjectiveContractModel):
    subtask_id: str = Field(min_length=1)
    execution_status: ExecutionStatus
    domain_outcome: DomainOutcome
    verdict: VerificationVerdict
    output_artifact_ids: list[str] = Field(default_factory=list)
    evidence_need_ids: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    error_category: str | None = None
    stop_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def execution_error_shape_is_valid(self) -> "SubagentResult":
        failed = self.execution_status in {
            ExecutionStatus.FAILED,
            ExecutionStatus.UNKNOWN,
        }
        if failed and not self.error_category:
            raise ValueError(
                "error_category is required when execution did not succeed"
            )
        if not failed and self.error_category:
            raise ValueError(
                "error_category is only valid for FAILED or UNKNOWN results"
            )
        return self


class MainAgentDecision(ObjectiveContractModel):
    decision_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    target_subagent_role: str | None = None
    input_artifact_ids: list[str] = Field(default_factory=list)
    state_digest: Digest
    requested_budget: dict[str, Any] = Field(default_factory=dict)
    expected_information_gain: float = Field(ge=0, le=1)
    stop_reason: str | None = None


class RunManifest(ObjectiveContractModel):
    manifest_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    orchestrator_version: str = Field(min_length=1)
    main_agent_version: str = Field(min_length=1)
    subagent_role_versions: dict[str, str] = Field(min_length=1)
    model_versions: dict[str, str] = Field(min_length=1)
    prompt_digests: dict[str, str] = Field(default_factory=dict)
    skill_digests: dict[str, str] = Field(default_factory=dict)
    schema_versions: dict[str, str] = Field(min_length=1)
    validation_policy_version: str = Field(min_length=1)
    tool_contract_versions: dict[str, str] = Field(default_factory=dict)
    provider_adapter_versions: dict[str, str] = Field(default_factory=dict)
    data_snapshot_or_retrieval_time_policy: str = Field(min_length=1)
    runtime_bundle_id: str = Field(min_length=1)
    runtime_bundle_digest: Digest


class Checkpoint(ObjectiveContractModel):
    checkpoint_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    runtime_bundle_id: str = Field(min_length=1)
    confirmed_state: str = Field(min_length=1)
    state_revision: int = Field(ge=0)
    last_committed_sequence: int = Field(ge=0)
    open_operation_ids: list[str] = Field(default_factory=list)
    created_at: UtcDateTime


class OperationRecord(ObjectiveContractModel):
    operation_key: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    retry_ordinal: int = Field(ge=0)
    parent_subtask_id: str = Field(min_length=1)
    operation_type: str = Field(min_length=1)
    input_digest: Digest
    started_at: UtcDateTime
    finished_at: UtcDateTime | None
    deduplication_status: DeduplicationStatus
    commit_sequence: int = Field(ge=0)
    execution_status: ExecutionStatus
    domain_outcome: DomainOutcome
    verdict: VerificationVerdict
    output_artifact_ids: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    cost: float = Field(default=0.0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    error_category: str | None = None

    @model_validator(mode="after")
    def execution_error_shape_is_valid(self) -> "OperationRecord":
        failed = self.execution_status in {
            ExecutionStatus.FAILED,
            ExecutionStatus.UNKNOWN,
        }
        if failed and not self.error_category:
            raise ValueError(
                "error_category is required when execution did not succeed"
            )
        if not failed and self.error_category:
            raise ValueError(
                "error_category is only valid for FAILED or UNKNOWN execution"
            )
        if (
            self.deduplication_status is DeduplicationStatus.COMMITTED
            and self.commit_sequence < 1
        ):
            raise ValueError(
                "COMMITTED operations require a positive commit_sequence"
            )
        return self


class MetricSpec(ObjectiveContractModel):
    metric_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    population: str = Field(min_length=1)
    numerator: str = Field(min_length=1)
    denominator: str = Field(min_length=1)
    exclusions: list[str] = Field(default_factory=list)
    label_source: str = Field(min_length=1)
    adjudication_protocol: str = Field(min_length=1)
    aggregation_method: str = Field(min_length=1)
    confidence_interval_method: str = Field(min_length=1)
    minimum_sample_size: int = Field(ge=1)


class ObjectiveAcceptanceCase(ContractModel):
    case_id: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    expected_verdict: str = Field(min_length=1)
    expected_reason_code: str = Field(
        min_length=1,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )


class ObjectiveAcceptanceSuite(ObjectiveContractModel):
    schema_version: str = OBJECTIVE_ACCEPTANCE_SCHEMA_VERSION
    cases: list[ObjectiveAcceptanceCase] = Field(min_length=1)

    @model_validator(mode="after")
    def case_ids_are_unique(self) -> "ObjectiveAcceptanceSuite":
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("acceptance case_id values must be unique")
        return self

    @property
    def case_ids(self) -> list[str]:
        return [case.case_id for case in self.cases]
