"""Evaluate every CTD competitor behind a public study-context question."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

from pydantic import Field, model_validator

from ..domain.common import ContractModel, UtcDateTime, utc_now
from ..evidence_enrichment.ctd import CtdEvidenceIndex
from ..evidence_enrichment.contracts import (
    evidence_from_ctd,
    predicate_family_for_intent,
)
from ..evidence_enrichment.models import CtdEvidenceRecord
from ..evidence_enrichment.models import QuestionIntent
from .evidence_fusion import (
    CtdEvidenceFusion,
    CtdEvidenceFusionStatus,
)
from .task_contracts import (
    AnswerCandidateOutcome,
    StudyContext,
)


KNOWN_OUTCOME_STATUSES = frozenset({"matched", "not_matched"})
UNKNOWN_OUTCOME_STATUSES = frozenset(
    {"ambiguous", "unreadable", "provider_failed", "not_evaluated"}
)


class CtdDiseaseIdentityResolution(ContractModel):
    status: Literal["resolved", "ambiguous", "unmapped", "provider_failed"]
    answer_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def result_matches_status(self) -> "CtdDiseaseIdentityResolution":
        if self.status == "resolved" and len(self.answer_ids) != 1:
            raise ValueError("a resolved CTD disease needs one canonical answer")
        if self.status != "resolved" and len(self.answer_ids) < 1:
            raise ValueError("an unresolved CTD disease needs audit answer IDs")
        return self


class StaticCtdDiseaseIdentityResolver:
    """Deterministic identity map used until a CTD crosswalk is wired in."""

    def __init__(
        self,
        mapping: Mapping[str, str | Sequence[str]],
    ) -> None:
        self.mapping = {
            key: ([value] if isinstance(value, str) else list(value))
            for key, value in mapping.items()
        }

    def resolve(
        self,
        record: CtdEvidenceRecord,
    ) -> CtdDiseaseIdentityResolution:
        key = ctd_record_identity_key(record)
        answer_ids = sorted(set(self.mapping.get(key, [])))
        if not answer_ids:
            audit_id = "ctd:unmapped:" + hashlib.sha256(
                f"{record.source_snapshot_id}|{key}".encode("utf-8")
            ).hexdigest()[:24]
            return CtdDiseaseIdentityResolution(
                status="unmapped",
                answer_ids=[audit_id],
                reason_codes=["ctd_record_unmapped"],
            )
        if len(answer_ids) > 1:
            return CtdDiseaseIdentityResolution(
                status="ambiguous",
                answer_ids=answer_ids,
                reason_codes=["ctd_mapping_ambiguous"],
            )
        return CtdDiseaseIdentityResolution(
            status="resolved",
            answer_ids=answer_ids,
        )


class CtdDiseaseCategoryResolution(ContractModel):
    status: Literal["matched", "not_matched", "unknown", "provider_failed"]
    categories: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)


class StaticCtdDiseaseCategoryResolver:
    """Exact category map keyed by canonical answer identity."""

    def __init__(
        self,
        mapping: Mapping[str, Sequence[str]],
    ) -> None:
        self.mapping = {
            key: sorted(set(value)) for key, value in mapping.items()
        }

    def resolve(
        self,
        *,
        record: CtdEvidenceRecord,
        answer_id: str,
        context: StudyContext,
    ) -> CtdDiseaseCategoryResolution:
        categories = self.mapping.get(answer_id)
        if categories is None:
            return CtdDiseaseCategoryResolution(
                status="unknown",
                reason_codes=["ctd_disease_category_unknown"],
            )
        expected = context.disease_category.casefold()
        if any(item.casefold() == expected for item in categories):
            return CtdDiseaseCategoryResolution(
                status="matched",
                categories=list(categories),
            )
        return CtdDiseaseCategoryResolution(
            status="not_matched",
            categories=list(categories),
        )


class CtdCandidateAssessor(Protocol):
    def assess(
        self,
        *,
        record: CtdEvidenceRecord,
        context: StudyContext,
        answer_id: str,
    ) -> AnswerCandidateOutcome: ...


class CtdWorldEvaluation(ContractModel):
    schema_version: str = "ctd-world-evaluation-v1"
    chemical_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    context: StudyContext
    competitor_outcomes: list[AnswerCandidateOutcome] = Field(
        default_factory=list
    )
    answer_records: dict[str, list[CtdEvidenceRecord]] = Field(
        default_factory=dict
    )
    world_answer_ids: list[str] = Field(default_factory=list)
    result_complete: bool
    reason_codes: list[str] = Field(default_factory=list)
    rows_examined: int = Field(default=0, ge=0)
    evaluated_at: UtcDateTime = Field(default_factory=utc_now)
    evaluator_version: str = "ctd-world-evaluator-v1"

    @model_validator(mode="after")
    def complete_world_has_only_known_outcomes(self) -> "CtdWorldEvaluation":
        if self.result_complete and any(
            item.status not in KNOWN_OUTCOME_STATUSES
            for item in self.competitor_outcomes
        ):
            raise ValueError(
                "a complete CTD world cannot contain an unknown competitor"
            )
        matched = sorted(
            item.answer_id
            for item in self.competitor_outcomes
            if item.status == "matched"
        )
        if matched != self.world_answer_ids:
            raise ValueError(
                "world answers must equal matched competitor outcomes"
            )
        return self


def ctd_record_identity_key(record: CtdEvidenceRecord) -> str:
    return record.disease_id or " ".join(record.disease_name.casefold().split())


def _unknown_answer_id(
    record: CtdEvidenceRecord,
    resolution: CtdDiseaseIdentityResolution,
) -> str:
    if resolution.answer_ids:
        return resolution.answer_ids[0]
    return "ctd:unknown:" + hashlib.sha256(
        f"{record.source_snapshot_id}|{ctd_record_identity_key(record)}".encode(
            "utf-8"
        )
    ).hexdigest()[:24]


def _merge_outcomes(
    outcomes: Sequence[AnswerCandidateOutcome],
    records: Sequence[CtdEvidenceRecord],
) -> AnswerCandidateOutcome:
    statuses = {item.status for item in outcomes}
    if "provider_failed" in statuses:
        status = "provider_failed"
    elif "not_evaluated" in statuses:
        status = "not_evaluated"
    elif "ambiguous" in statuses:
        status = "ambiguous"
    elif "unreadable" in statuses:
        status = "unreadable"
    elif statuses == {"matched", "not_matched"}:
        status = "ambiguous"
    elif "matched" in statuses:
        status = "matched"
    else:
        status = "not_matched"
    answer_id = outcomes[0].answer_id
    claim_ids = sorted(
        {
            claim_id
            for item in outcomes
            if item.status == "matched"
            for claim_id in item.claim_ids
        }
    )
    evidence_ids = sorted(
        {
            evidence_id
            for item in outcomes
            if item.status == "matched"
            for evidence_id in item.evidence_ids
        }
    )
    reasons = sorted(
        {reason for item in outcomes for reason in item.reason_codes}
    )
    if status == "ambiguous" and statuses == {"matched", "not_matched"}:
        reasons.append("ctd_alias_outcomes_conflict")
    if status == "matched" and (not claim_ids or not evidence_ids):
        status = "ambiguous"
        reasons.append("matched_outcome_missing_support")
    return AnswerCandidateOutcome(
        answer_id=answer_id,
        status=status,
        claim_ids=claim_ids,
        evidence_ids=evidence_ids,
        reason_codes=sorted(set(reasons)),
    )


class CtdWorldEvaluator:
    """Enumerate all CTD diseases for a chemical and assess each competitor."""

    version = "ctd-world-evaluator-v1"

    def __init__(
        self,
        *,
        index: CtdEvidenceIndex,
        identity_resolver: StaticCtdDiseaseIdentityResolver,
        category_resolver: StaticCtdDiseaseCategoryResolver,
        assessor: CtdCandidateAssessor,
    ) -> None:
        self.index = index
        self.identity_resolver = identity_resolver
        self.category_resolver = category_resolver
        self.assessor = assessor

    def evaluate(
        self,
        *,
        chemical_id: str,
        snapshot_id: str,
        context: StudyContext,
    ) -> CtdWorldEvaluation:
        index_snapshot = self.index.metadata().get("snapshot_id")
        if snapshot_id != index_snapshot:
            return CtdWorldEvaluation(
                chemical_id=chemical_id,
                snapshot_id=snapshot_id,
                context=context,
                result_complete=False,
                reason_codes=["ctd_snapshot_mismatch"],
            )
        try:
            records = self.index.enumerate_diseases(chemical_id=chemical_id)
        except Exception:
            return CtdWorldEvaluation(
                chemical_id=chemical_id,
                snapshot_id=snapshot_id,
                context=context,
                result_complete=False,
                reason_codes=["ctd_provider_unavailable"],
            )

        per_answer: dict[str, list[AnswerCandidateOutcome]] = {}
        answer_records: dict[str, list[CtdEvidenceRecord]] = {}
        for record in records:
            identity = self.identity_resolver.resolve(record)
            if identity.status == "provider_failed":
                answer_ids = identity.answer_ids or [
                    _unknown_answer_id(record, identity)
                ]
                for answer_id in answer_ids:
                    per_answer.setdefault(answer_id, []).append(
                        AnswerCandidateOutcome(
                            answer_id=answer_id,
                            status="provider_failed",
                            reason_codes=list(identity.reason_codes),
                        )
                    )
                    answer_records.setdefault(answer_id, []).append(record)
                continue
            if identity.status == "unmapped":
                answer_id = _unknown_answer_id(record, identity)
                per_answer.setdefault(answer_id, []).append(
                    AnswerCandidateOutcome(
                        answer_id=answer_id,
                        status="not_evaluated",
                        reason_codes=list(identity.reason_codes),
                    )
                )
                answer_records.setdefault(answer_id, []).append(record)
                continue
            if identity.status == "ambiguous":
                for answer_id in identity.answer_ids:
                    per_answer.setdefault(answer_id, []).append(
                        AnswerCandidateOutcome(
                            answer_id=answer_id,
                            status="ambiguous",
                            reason_codes=list(identity.reason_codes),
                        )
                    )
                    answer_records.setdefault(answer_id, []).append(record)
                continue

            answer_id = identity.answer_ids[0]
            category = self.category_resolver.resolve(
                record=record,
                answer_id=answer_id,
                context=context,
            )
            if category.status == "unknown":
                outcome = AnswerCandidateOutcome(
                    answer_id=answer_id,
                    status="not_evaluated",
                    reason_codes=list(category.reason_codes),
                )
            elif category.status == "provider_failed":
                outcome = AnswerCandidateOutcome(
                    answer_id=answer_id,
                    status="provider_failed",
                    reason_codes=list(category.reason_codes),
                )
            elif category.status == "not_matched":
                outcome = AnswerCandidateOutcome(
                    answer_id=answer_id,
                    status="not_matched",
                    reason_codes=["disease_category_not_matched"],
                )
            else:
                try:
                    outcome = self.assessor.assess(
                        record=record,
                        context=context,
                        answer_id=answer_id,
                    )
                except Exception:
                    outcome = AnswerCandidateOutcome(
                        answer_id=answer_id,
                        status="provider_failed",
                        reason_codes=["ctd_candidate_assessment_failed"],
                    )
                if outcome.answer_id != answer_id:
                    outcome = AnswerCandidateOutcome(
                        answer_id=answer_id,
                        status="ambiguous",
                        reason_codes=["ctd_candidate_answer_identity_mismatch"],
                    )
            per_answer.setdefault(answer_id, []).append(outcome)
            answer_records.setdefault(answer_id, []).append(record)

        outcomes = [
            _merge_outcomes(per_answer[answer_id], answer_records[answer_id])
            for answer_id in sorted(per_answer)
        ]
        result_complete = all(
            item.status in KNOWN_OUTCOME_STATUSES for item in outcomes
        )
        reasons: set[str] = set()
        if not result_complete:
            reasons.add("unknown_competitor")
        return CtdWorldEvaluation(
            chemical_id=chemical_id,
            snapshot_id=snapshot_id,
            context=context,
            competitor_outcomes=outcomes,
            answer_records={
                key: sorted(value, key=ctd_record_identity_key)
                for key, value in answer_records.items()
            },
            world_answer_ids=sorted(
                item.answer_id
                for item in outcomes
                if item.status == "matched"
            ),
            result_complete=result_complete,
            reason_codes=sorted(reasons),
            rows_examined=len(records),
        )


class FusionCtdCandidateAssessor:
    """Turn an already fused CTD/PubMed claim into one competitor outcome."""

    def __init__(
        self,
        *,
        fusions: Mapping[str, CtdEvidenceFusion],
        provider_failures: Mapping[str, str] | None = None,
    ) -> None:
        self.fusions = dict(fusions)
        self.provider_failures = dict(provider_failures or {})

    def assess(
        self,
        *,
        record: CtdEvidenceRecord,
        context: StudyContext,
        answer_id: str,
    ) -> AnswerCandidateOutcome:
        locator = record.source_record_locator or record.content_hash
        if locator in self.provider_failures:
            return AnswerCandidateOutcome(
                answer_id=answer_id,
                status="provider_failed",
                reason_codes=[self.provider_failures[locator]],
            )
        fusion = self.fusions.get(locator)
        if fusion is None:
            return AnswerCandidateOutcome(
                answer_id=answer_id,
                status="not_evaluated",
                reason_codes=["claim_not_supported"],
            )
        if fusion.status is CtdEvidenceFusionStatus.CONFLICTING:
            return AnswerCandidateOutcome(
                answer_id=answer_id,
                status="ambiguous",
                reason_codes=["conflicting_evidence_unresolved"],
            )
        claim = fusion.claim
        if fusion.status is not CtdEvidenceFusionStatus.ELIGIBLE or claim is None:
            unreadable = any(
                reason.startswith("pubmed_")
                or reason
                in {
                    "unknown_pmid_reference",
                    "support_span_coverage_mismatch",
                }
                for reason in fusion.reason_codes
            )
            return AnswerCandidateOutcome(
                answer_id=answer_id,
                status="unreadable" if unreadable else "not_matched",
                reason_codes=list(fusion.reason_codes),
            )
        if claim.object_id != answer_id:
            return AnswerCandidateOutcome(
                answer_id=answer_id,
                status="ambiguous",
                reason_codes=["ctd_candidate_answer_identity_mismatch"],
            )
        expected_predicate = predicate_family_for_intent(
            QuestionIntent(context.relation_intent),
            fallback_relation="chemical_disease_association",
        )
        if (
            claim.predicate_family != expected_predicate
            or claim.polarity != context.polarity
        ):
            return AnswerCandidateOutcome(
                answer_id=answer_id,
                status="not_matched",
                reason_codes=["claim_semantics_mismatch"],
            )
        if not _qualifiers_match(claim.qualifiers, context):
            return AnswerCandidateOutcome(
                answer_id=answer_id,
                status="not_matched",
                reason_codes=["qualifier_not_supported"],
            )
        ctd_evidence = evidence_from_ctd(
            record,
            subject_entity_id=f"ctd:chemical:{record.chemical_id}",
            object_entity_id=answer_id,
            retrieved_at=utc_now(),
        )
        return AnswerCandidateOutcome(
            answer_id=answer_id,
            status="matched",
            claim_ids=[claim.claim_id],
            evidence_ids=[
                ctd_evidence.evidence_id,
                *claim.supporting_evidence_ids,
            ],
        )


def _qualifiers_match(
    actual: Mapping[str, object],
    context: StudyContext,
) -> bool:
    required: dict[str, str] = {
        "disease_category": context.disease_category,
    }
    for key in ("population", "study_type", "sample_type"):
        value = getattr(context, key)
        if value is not None:
            required[key] = value
    if context.exposure_context is not None:
        required["exposure_context"] = context.exposure_context
    if context.dose_or_regimen is not None:
        required["dose_exposure"] = context.dose_or_regimen
    duration = context.exposure_duration or context.temporal_scope
    if duration is not None:
        actual_duration = (
            actual.get("exposure_duration")
            or actual.get("temporal_scope")
        )
        if (
            not isinstance(actual_duration, str)
            or actual_duration.casefold() != duration.casefold()
        ):
            return False
    for key, expected in required.items():
        value = actual.get(key)
        if not isinstance(value, str) or (
            value.casefold() != expected.casefold()
        ):
            return False
    for key, expected in context.additional_qualifiers.items():
        value = actual.get(key)
        if not isinstance(value, str) or (
            value.casefold() != expected.casefold()
        ):
            return False
    return True
