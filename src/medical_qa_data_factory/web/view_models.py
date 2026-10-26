"""Read-only projections used by the local evidence workbench."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from ..domain import EntityRef, EvidenceBundle, GenerationTask, QuestionSpec
from ..generation.repository import GenerationRepository
from ..repository import LocalArtifactRepository


@dataclass
class EntityWorkspace:
    """Everything generated from one seed entity, grouped for human browsing."""

    entity: EntityRef
    tasks: list[GenerationTask] = field(default_factory=list)
    bundles: list[EvidenceBundle] = field(default_factory=list)
    questions: list[QuestionSpec] = field(default_factory=list)

    @property
    def kg_count(self) -> int:
        return sum(len(bundle.kg_evidence) for bundle in self.bundles)

    @property
    def literature_count(self) -> int:
        return sum(len(bundle.literature_evidence) for bundle in self.bundles)

    @property
    def claim_count(self) -> int:
        return sum(len(bundle.claims) for bundle in self.bundles)

    @property
    def issue_count(self) -> int:
        task_errors = sum(len(task.errors) for task in self.tasks)
        blocking_gaps = sum(
            gap.blocking for bundle in self.bundles for gap in bundle.evidence_gaps
        )
        return task_errors + blocking_gaps

    @property
    def route_counts(self) -> Counter[str]:
        return Counter(question.expected_route.value for question in self.questions)

    @property
    def last_updated(self) -> datetime | None:
        dates = [task.updated_at for task in self.tasks]
        dates.extend(bundle.created_at for bundle in self.bundles)
        dates.extend(question.created_at for question in self.questions)
        return max(dates, default=None)


@dataclass(frozen=True)
class GeneratedQuestionRow:
    """One generated question with the evidence counts needed by the home page."""

    question: QuestionSpec
    kg_count: int = 0
    literature_count: int = 0

    @property
    def difficulty_label(self) -> str:
        hop_count = self.question.taxonomy.hop_complexity or 0
        tags = {tag.lower() for tag in self.question.taxonomy.difficulty_tags}
        if hop_count >= 3 or "hard" in tags:
            return "困难"
        if hop_count >= 2 or "medium" in tags:
            return "适中"
        return "简单"

    @property
    def source_label(self) -> str:
        if self.kg_count and self.literature_count:
            return "KG + 文献"
        if self.literature_count:
            return "文献"
        return "KG"

    @property
    def validation_label(self) -> str:
        labels = {
            "draft": "候选",
            "machine_validated": "机器校验通过",
            "human_confirmed": "人工确认",
        }
        return labels.get(
            self.question.validation_level.value,
            self.question.validation_level.value,
        )

    @property
    def style_label(self) -> str:
        intent = self.question.query_intent
        skill_id = str(intent.get("style_skill_id") or "").strip()
        profile = str(intent.get("style_profile") or "").strip()
        if not skill_id:
            return "规则题面"
        display = {
            "layperson-search-v1": "普通用户搜索",
            "patient-caregiver-v1": "患者/家属",
            "medical-learner-v1": "医学学习者",
        }.get(skill_id, skill_id)
        return f"{display} · {profile}" if profile else display

    @property
    def style_score_label(self) -> str | None:
        value = self.question.query_intent.get("style_score")
        try:
            return f"风格 {float(value):.2f}"
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class QuestionLibraryRow(GeneratedQuestionRow):
    """A generated question and the task that owns it."""

    task_id: str | None = None


@dataclass(frozen=True)
class GenerationOperationRow:
    """Human-readable projection of one persisted agent step."""

    sequence: int
    node: str
    label: str
    description: str
    status: str
    status_label: str
    elapsed_label: str
    tool_call_count: int
    records_returned: int
    output_count: int


@dataclass(frozen=True)
class GenerationActivity:
    """The persisted execution trace for one generation task."""

    episode_id: str
    status: str
    status_label: str
    stop_reason: str | None
    started_at: datetime
    completed_at: datetime | None
    operations: list[GenerationOperationRow]
    total_elapsed_label: str
    total_tool_calls: int
    total_records_returned: int


_OPERATION_COPY = {
    "verify_seed": ("确认种子实体", "在知识图谱中核对实体身份"),
    "collect_neighbors": ("探索图谱关系", "查询入边与出边，收集候选证据"),
    "agent_explore": ("规划探索路径", "根据当前证据决定下一步查询"),
    "curate_evidence": ("筛选证据", "去重并保留可用于出题的关系"),
    "build_claims": (
        "增强证据并构建主张",
        "读取 CTD/PubMed 证据，判断关系语义并构建可验证主张",
    ),
    "plan_questions": ("规划问题", "选择答案实体、关系语义和问题结构"),
    "write_questions": ("撰写问题", "将问题规划转换成自然语言"),
    "persist_draft_questions": ("保存候选问题", "写入草稿问题与答案"),
    "verify_questions": ("独立验证", "检查答案是否能由证据推出"),
    "machine_validate": ("机器校验", "运行结构、引用和一致性检查"),
    "finalize_artifact_links": ("关联运行产物", "连接问题、证据与任务记录"),
    "finalize": ("完成生成", "保存训练投影与最终运行状态"),
    "finalize_partial": ("保存部分结果", "记录已完成的产物和停止原因"),
    "fail": ("记录失败", "保存失败位置与可恢复信息"),
}


def _elapsed_label(elapsed_ms: int | None) -> str:
    if elapsed_ms is None:
        return "—"
    if elapsed_ms < 1000:
        return f"{elapsed_ms} ms"
    return f"{elapsed_ms / 1000:.1f} s"


def build_generation_activity(
    workspace: str,
    task: GenerationTask | None,
) -> GenerationActivity | None:
    """Load the latest episode and its ordered audit steps for a task."""
    if task is None:
        return None
    episodes = GenerationRepository(workspace)
    matches = [
        item for item in episodes.list_episodes() if item.task_id == task.task_id
    ]
    if not matches:
        return None
    episode = max(matches, key=lambda item: item.started_at)
    observations = {
        item.observation_id: item
        for item in episodes.list_observations_by_episode(episode.episode_id)
    }
    operations: list[GenerationOperationRow] = []
    total_elapsed_ms = 0
    total_tool_calls = 0
    total_records_returned = 0
    for step in episodes.list_steps_by_episode(episode.episode_id):
        step_observations = [
            observations[item]
            for item in step.tool_observation_ids
            if item in observations
        ]
        label, description = _OPERATION_COPY.get(
            step.node,
            (step.node.replace("_", " ").title(), "执行生成流水线步骤"),
        )
        records_returned = sum(item.row_count for item in step_observations)
        tool_call_count = len(step_observations)
        elapsed_ms = step.elapsed_ms or 0
        total_elapsed_ms += elapsed_ms
        total_tool_calls += tool_call_count
        total_records_returned += records_returned
        operations.append(
            GenerationOperationRow(
                sequence=step.sequence,
                node=step.node,
                label=label,
                description=description,
                status=step.status,
                status_label={
                    "started": "进行中",
                    "completed": "已完成",
                    "failed": "失败",
                    "skipped": "已跳过",
                }.get(step.status, step.status),
                elapsed_label=_elapsed_label(step.elapsed_ms),
                tool_call_count=tool_call_count,
                records_returned=records_returned,
                output_count=len(step.output_artifact_ids),
            )
        )
    return GenerationActivity(
        episode_id=episode.episode_id,
        status=episode.state.value,
        status_label={
            "running": "运行中",
            "completed": "已完成",
            "partial": "部分完成",
            "failed": "失败",
        }.get(episode.state.value, episode.state.value),
        stop_reason=episode.stop_reason.value if episode.stop_reason else None,
        started_at=episode.started_at,
        completed_at=episode.completed_at,
        operations=operations,
        total_elapsed_label=_elapsed_label(total_elapsed_ms),
        total_tool_calls=total_tool_calls,
        total_records_returned=total_records_returned,
    )


def build_entity_workspaces(
    repository: LocalArtifactRepository,
) -> list[EntityWorkspace]:
    """Build one deduplicated projection per seed entity."""
    records: dict[str, EntityWorkspace] = {}

    def ensure(entity: EntityRef) -> EntityWorkspace:
        return records.setdefault(
            entity.entity_ref_id,
            EntityWorkspace(entity=entity),
        )

    for task in repository.list_latest_tasks():
        for entity in task.seed_entities:
            ensure(entity).tasks.append(task)

    bundles = repository.list_latest_bundles()
    for bundle in bundles:
        for entity in bundle.seed_entities:
            ensure(entity).bundles.append(bundle)

    bundles_by_id = {bundle.bundle_id: bundle for bundle in bundles}
    for question in repository.list_latest_questions():
        entity_ids = {
            entity.entity_ref_id
            for bundle_id in question.bundle_ids
            if (bundle := bundles_by_id.get(bundle_id)) is not None
            for entity in bundle.seed_entities
        }
        for entity_id in entity_ids:
            records[entity_id].questions.append(question)

    for record in records.values():
        record.tasks.sort(key=lambda item: item.updated_at, reverse=True)
        record.bundles.sort(key=lambda item: item.created_at, reverse=True)
        record.questions.sort(key=lambda item: item.created_at, reverse=True)

    return sorted(
        records.values(),
        key=lambda item: (
            item.last_updated is not None,
            item.last_updated,
        ),
        reverse=True,
    )


def build_generated_question_rows(
    repository: LocalArtifactRepository,
    record: EntityWorkspace | None,
) -> list[GeneratedQuestionRow]:
    """Project the selected entity's questions into generation-result rows."""
    if record is None:
        return []

    bundles = {bundle.bundle_id: bundle for bundle in record.bundles}
    rows: list[GeneratedQuestionRow] = []
    for question in record.questions:
        related = [
            bundles[bundle_id]
            for bundle_id in question.bundle_ids
            if bundle_id in bundles
        ]
        rows.append(
            GeneratedQuestionRow(
                question=question,
                kg_count=sum(len(bundle.kg_evidence) for bundle in related),
                literature_count=sum(
                    len(bundle.literature_evidence) for bundle in related
                ),
            )
        )
    return rows


def build_question_library_rows(
    repository: LocalArtifactRepository,
) -> list[QuestionLibraryRow]:
    """Project every question with its owning task and evidence counts."""
    tasks = repository.list_latest_tasks()
    bundles = repository.list_latest_bundles()
    bundles_by_id = {bundle.bundle_id: bundle for bundle in bundles}

    artifacts_to_task: dict[str, str] = {}
    for task in tasks:
        for artifact_id in task.artifact_ids:
            artifacts_to_task.setdefault(artifact_id, task.task_id)

    episodes_to_task = {
        episode.episode_id: episode.task_id
        for episode in GenerationRepository(str(repository.root)).list_episodes()
    }

    rows: list[QuestionLibraryRow] = []
    for question in repository.list_latest_questions():
        task_id = artifacts_to_task.get(question.question_id)
        if task_id is None and question.generation_episode_id:
            task_id = episodes_to_task.get(question.generation_episode_id)
        if task_id is None:
            task_id = next(
                (
                    artifacts_to_task[bundle_id]
                    for bundle_id in question.bundle_ids
                    if bundle_id in artifacts_to_task
                ),
                None,
            )
        related = [
            bundles_by_id[bundle_id]
            for bundle_id in question.bundle_ids
            if bundle_id in bundles_by_id
        ]
        rows.append(
            QuestionLibraryRow(
                question=question,
                task_id=task_id,
                kg_count=sum(len(bundle.kg_evidence) for bundle in related),
                literature_count=sum(
                    len(bundle.literature_evidence) for bundle in related
                ),
            )
        )
    return sorted(
        rows,
        key=lambda item: item.question.created_at,
        reverse=True,
    )
