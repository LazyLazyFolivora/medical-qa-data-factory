"""JSON Schema export for externally consumed data contracts."""
from __future__ import annotations

import json
from pathlib import Path

from .domain import EvidenceBundle, GenerationTask, QuestionSpec
from .evidence_enrichment.contracts import (
    EvidenceRecord,
    EvidenceStore,
    NormalizedClaim,
)
from .evidence_enrichment.pubmed_evidence import (
    PubMedCorpusManifest,
    PubMedEvidenceSpan,
)
from .evidence_enrichment.primekg_ctd_identity import (
    PrimeKgCtdChemicalCrosswalk,
    PrimeKgCtdIdentitySnapshot,
)
from .generation.evidence_fusion import (
    CtdEvidenceExtraction,
    CtdEvidenceFusion,
)
from .generation.evidence_provider import (
    EvidenceProviderAttempt,
    EvidenceProviderCapability,
    EvidenceProviderRequest,
    EvidenceProviderResult,
)
from .generation.fact_exploration import CandidateClaimBatch
from .generation.ctd_world_evaluation import CtdWorldEvaluation
from .generation.question_design import QuestionHypothesisBatch
from .generation.objective_question_contracts import (
    AnswerEvidenceAuditResult,
    AnswerResolution,
    BlindAnswerQuery,
    CandidateClaim,
    Checkpoint,
    EntityResolution,
    EvidenceAnchor,
    EvidenceMaterial,
    FrozenQuestionContract,
    GenerationGoal,
    MainAgentDecision,
    MetricSpec,
    ObjectiveAcceptanceSuite,
    ObjectiveValidationReport,
    OperationRecord,
    PublicQuestionAuditResult,
    QuestionHypothesis,
    RunManifest,
    StructuredDerivationTrace,
    SubagentResult,
    SubagentTask,
    VerificationAttempt,
    VerificationCoverageReport,
    VerificationPlan,
)
from .generation.run_identity import (
    AttemptRecord,
    GenerationPreparationRun,
    GenerationRunManifest,
)
from .generation.contracts import GenerationSpec, SafetyBudget
from .generation.task_contracts import (
    EvaluationTarget,
    PlannerQuestionInput,
    TaskSpec,
    ValidatedTask,
    ValidationEnvironment,
    ValidationReport,
)
from .generation.task_candidates import (
    CtdCandidateProposal,
    CtdQualifierCounterfactual,
)
from .generation.task_service import (
    CtdEvidenceCatalogEntry,
    CtdGenerationEpisode,
    CtdTaskRunSpec,
)
from .generation.primekg_ctd_pubmed import (
    PrimeKgCtdChemicalMapping,
    PrimeKgCtdDiseaseMapping,
    PrimeKgCtdPubMedResult,
)
from .generation.primekg_ctd_pubmed_task import (
    PrimeKgCtdPubmedTaskOutcome,
)
from .generation.prompt_writing import (
    GeneratedQuestionText,
    QuestionSemanticView,
)
from .generation.training import (
    GenerationRunMetadata,
    PlannerStep,
    PlannerTrajectory,
    PlannerTrainingSample,
    QAItem,
)


SCHEMA_MODELS = {
    "generation-task.schema.json": GenerationTask,
    "evidence-bundle.schema.json": EvidenceBundle,
    "source-neutral-evidence-record.schema.json": EvidenceRecord,
    "source-neutral-evidence-store.schema.json": EvidenceStore,
    "normalized-claim.schema.json": NormalizedClaim,
    "pubmed-corpus-manifest.schema.json": PubMedCorpusManifest,
    "pubmed-evidence-span.schema.json": PubMedEvidenceSpan,
    "ctd-evidence-extraction.schema.json": CtdEvidenceExtraction,
    "ctd-evidence-fusion.schema.json": CtdEvidenceFusion,
    "ctd-world-evaluation.schema.json": CtdWorldEvaluation,
    "ctd-candidate-proposal.schema.json": CtdCandidateProposal,
    "ctd-qualifier-counterfactual.schema.json": (
        CtdQualifierCounterfactual
    ),
    "ctd-evidence-catalog-entry.schema.json": CtdEvidenceCatalogEntry,
    "ctd-generation-episode.schema.json": CtdGenerationEpisode,
    "ctd-task-run-spec.schema.json": CtdTaskRunSpec,
    "primekg-ctd-disease-mapping.schema.json": PrimeKgCtdDiseaseMapping,
    "primekg-ctd-chemical-mapping.schema.json": PrimeKgCtdChemicalMapping,
    "primekg-ctd-chemical-crosswalk.schema.json": (
        PrimeKgCtdChemicalCrosswalk
    ),
    "primekg-ctd-identity-snapshot.schema.json": (
        PrimeKgCtdIdentitySnapshot
    ),
    "primekg-ctd-pubmed-result.schema.json": PrimeKgCtdPubMedResult,
    "primekg-ctd-pubmed-task-outcome.schema.json": (
        PrimeKgCtdPubmedTaskOutcome
    ),
    "generated-question-text.schema.json": GeneratedQuestionText,
    "question-semantic-view.schema.json": QuestionSemanticView,
    "question-spec.schema.json": QuestionSpec,
    "generation-preparation-run.schema.json": GenerationPreparationRun,
    "generation-run-manifest.schema.json": GenerationRunManifest,
    "attempt-record.schema.json": AttemptRecord,
    "generation-spec.schema.json": GenerationSpec,
    "safety-budget.schema.json": SafetyBudget,
    "qa-item.schema.json": QAItem,
    "planner-step.schema.json": PlannerStep,
    "planner-trajectory.schema.json": PlannerTrajectory,
    "planner-training-sample.schema.json": PlannerTrainingSample,
    "generation-run-metadata.schema.json": GenerationRunMetadata,
    "task-spec.schema.json": TaskSpec,
    "validated-task.schema.json": ValidatedTask,
    "validation-environment.schema.json": ValidationEnvironment,
    "validation-report.schema.json": ValidationReport,
    "planner-question-input.schema.json": PlannerQuestionInput,
    "evaluation-target.schema.json": EvaluationTarget,
    "evidence-provider-capability.schema.json": EvidenceProviderCapability,
    "evidence-provider-attempt.schema.json": EvidenceProviderAttempt,
    "evidence-provider-request.schema.json": EvidenceProviderRequest,
    "evidence-provider-result.schema.json": EvidenceProviderResult,
    "evidence-material.schema.json": EvidenceMaterial,
    "evidence-anchor.schema.json": EvidenceAnchor,
    "entity-resolution.schema.json": EntityResolution,
    "candidate-claim.schema.json": CandidateClaim,
    "candidate-claim-batch.schema.json": CandidateClaimBatch,
    "question-hypothesis.schema.json": QuestionHypothesis,
    "question-hypothesis-batch.schema.json": QuestionHypothesisBatch,
    "blind-answer-query.schema.json": BlindAnswerQuery,
    "verification-plan.schema.json": VerificationPlan,
    "verification-attempt.schema.json": VerificationAttempt,
    "verification-coverage-report.schema.json": VerificationCoverageReport,
    "answer-resolution.schema.json": AnswerResolution,
    "frozen-question-contract.schema.json": FrozenQuestionContract,
    "structured-derivation-trace.schema.json": StructuredDerivationTrace,
    "public-question-audit-result.schema.json": PublicQuestionAuditResult,
    "answer-evidence-audit-result.schema.json": (
        AnswerEvidenceAuditResult
    ),
    "objective-validation-report.schema.json": ObjectiveValidationReport,
    "generation-goal.schema.json": GenerationGoal,
    "subagent-task.schema.json": SubagentTask,
    "subagent-result.schema.json": SubagentResult,
    "main-agent-decision.schema.json": MainAgentDecision,
    "run-manifest.schema.json": RunManifest,
    "checkpoint.schema.json": Checkpoint,
    "operation-record.schema.json": OperationRecord,
    "metric-spec.schema.json": MetricSpec,
    "objective-acceptance-suite.schema.json": ObjectiveAcceptanceSuite,
}


def export_json_schemas(output_dir: str | Path) -> list[Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for filename, model in SCHEMA_MODELS.items():
        path = destination / filename
        payload = json.dumps(
            model.model_json_schema(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        path.write_text(payload + "\n", encoding="utf-8")
        paths.append(path)
    return paths
