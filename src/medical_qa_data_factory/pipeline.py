"""Application pipeline for seed-to-draft dataset generation."""
from __future__ import annotations

from dataclasses import dataclass

from .domain import EvidenceBundle, GenerationTask, QuestionSpec, TaskStatus
from .domain.common import new_id, utc_now
from .machine_validation import apply_machine_validation
from .ports import DraftAgent, KGTool, LiteratureTool
from .repository import LocalArtifactRepository
from .workflow import revise_task, transition_task


@dataclass(frozen=True)
class PipelineResult:
    task: GenerationTask
    bundles: list[EvidenceBundle]
    questions: list[QuestionSpec]


class DataFactoryPipeline:
    def __init__(
        self,
        *,
        repository: LocalArtifactRepository,
        kg_tool: KGTool,
        literature_tool: LiteratureTool,
        draft_agent: DraftAgent,
    ) -> None:
        self.repository = repository
        self.kg_tool = kg_tool
        self.literature_tool = literature_tool
        self.draft_agent = draft_agent

    @staticmethod
    def _error(source: str, exc: Exception) -> dict[str, object]:
        return {
            "source": source,
            "error_type": type(exc).__name__,
            "message": str(exc),
            # Only an explicit, declared retryable flag may mark a failure as
            # retryable; an unknown exception is not retryable by default.
            "code": getattr(exc, "code", "internal_error"),
            "retryable": bool(getattr(exc, "retryable", False)),
        }

    def run(self, task: GenerationTask) -> PipelineResult:
        self.repository.save_task(task)
        current = transition_task(
            task,
            TaskStatus.RUNNING,
            summary="start seed-to-draft pipeline",
            current_stage="evidence_collection",
        )
        self.repository.save_task(current)

        bundles: list[EvidenceBundle] = []
        questions: list[QuestionSpec] = []
        artifacts: list[str] = []
        invocations = []
        usage_records = []
        errors: list[dict[str, object]] = []

        for seed in current.seed_entities:
            kg_evidence = []
            literature_evidence = []
            snapshots = []
            seed_usage_ids: list[str] = []

            try:
                result = self.kg_tool.collect(seed)
                kg_evidence.extend(result.evidence)
                snapshots.append(result.snapshot)
                invocations.append(result.invocation)
                usage_records.append(result.usage)
                seed_usage_ids.append(result.usage.usage_id)
            except Exception as exc:  # external adapter boundary
                errors.append(self._error("kg", exc))

            try:
                result = self.literature_tool.collect(seed)
                literature_evidence.extend(result.evidence)
                snapshots.append(result.snapshot)
                invocations.append(result.invocation)
                usage_records.append(result.usage)
                seed_usage_ids.append(result.usage.usage_id)
            except Exception as exc:  # external adapter boundary
                errors.append(self._error("literature", exc))

            claims = self.draft_agent.propose_claims(
                seed, kg_evidence, literature_evidence
            )
            bundle = EvidenceBundle(
                bundle_id=new_id("bundle"),
                bundle_version=new_id("version"),
                task_id=current.task_id,
                seed_entities=[seed],
                kg_evidence=kg_evidence,
                literature_evidence=literature_evidence,
                claims=claims,
                tool_snapshots=snapshots,
                generation_usage_ids=seed_usage_ids,
                created_at=utc_now(),
            )
            self.repository.save_bundle(bundle)
            bundles.append(bundle)
            artifacts.append(bundle.bundle_id)

            remaining = None
            if current.target_question_count is not None:
                remaining = max(current.target_question_count - len(questions), 0)
            drafts = self.draft_agent.draft_questions(bundle, remaining)
            # This loop belongs to the per-seed block: indentation here is why
            # only the last seed's questions used to be saved.
            for question in drafts:
                validated = apply_machine_validation(
                    question,
                    [bundle],
                    peers=[
                        item
                        for item in questions
                        if item.split_group_id == question.split_group_id
                    ],
                )
                self.repository.save_question(validated)
                questions.append(validated)
                artifacts.append(question.question_id)

        current = revise_task(
            current,
            operation="record_pipeline_artifacts",
            summary="record evidence bundles, question drafts, usage, and errors",
            artifact_ids=artifacts,
            invocation_configs=invocations,
            generation_usage=usage_records,
            errors=errors,
            current_stage="question_drafted" if questions else "evidence_collected",
        )
        self.repository.save_task(current)

        has_evidence = any(
            bundle.kg_evidence or bundle.literature_evidence for bundle in bundles
        )
        if errors and has_evidence:
            final_status = TaskStatus.PARTIAL
        elif errors:
            final_status = TaskStatus.FAILED
        else:
            final_status = TaskStatus.COMPLETED
        current = transition_task(
            current,
            final_status,
            summary=f"pipeline finished with status {final_status.value}",
        )
        self.repository.save_task(current)
        return PipelineResult(task=current, bundles=bundles, questions=questions)

