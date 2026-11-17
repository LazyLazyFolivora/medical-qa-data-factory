<script setup>
import { runStopMessage } from "../api.js";
import { computed } from "vue";

const props = defineProps({
  task: { type: Object, default: null },
  workbench: { type: Object, default: null },
  activeTab: { type: String, default: "overview" },
  traceLoading: { type: Boolean, default: false },
  traceError: { type: String, default: "" },
  resumeBusy: { type: Boolean, default: false },
});

const emit = defineEmits([
  "select-tab",
  "resume",
  "open-question",
  "open-evidence",
  "back",
]);

const tabs = [
  ["overview", "概览"],
  ["execution", "执行轨迹"],
  ["candidates", "候选假设"],
  ["evidence", "证据材料"],
  ["questions", "已发布题目"],
  ["diagnostics", "诊断与导出"],
];

const data = computed(() => ({
  ...(props.workbench || {}),
  ...(props.task || {}),
}));
const steps = computed(() => data.value.main_agent_steps || []);
const interruptions = computed(() => {
  const events = data.value.recovery_events || [];
  return events.map((event, index) => ({
    event,
    current: data.value.run_state === "RECOVERY_REQUIRED" && index === events.length - 1,
  })).reverse();
});
const eventTime = (at) => at ? new Date(at).toLocaleString("zh-CN", { hour12: false }) : "";
const candidates = computed(() => [
  ...(data.value.candidate_questions || []),
  ...(data.value.rejected_questions || []),
  ...(data.value.indeterminate_questions || []),
]);
const questions = computed(
  () => data.value.published_questions || data.value.questions || [],
);
const artifactCounts = computed(() =>
  Object.entries(data.value.artifact_counts || {}),
);
const resumeContext = computed(() => {
  if (!data.value.can_resume) return null;
  return {
    lastStep: data.value.last_successful_step ?? steps.value.length,
    nextStep: data.value.next_step,
  };
});
const formatAnswer = (answer) =>
  Array.isArray(answer) ? answer.join("；") : answer || "—";
const statusText = (item) =>
  item.status_label ||
  ({
    candidate: "候选",
    rejected: "已拒绝",
    indeterminate: "无法确定",
  })[item.status] ||
  item.status;
</script>

<template>
  <main v-if="task" class="page-shell run-detail-page" data-run-detail>
    <nav class="breadcrumbs" aria-label="面包屑">
      <button type="button" class="link-button" @click="emit('back')">
        生成记录
      </button>
      <span>/</span>
      <span>{{ data.goal?.target_entity || "生成任务" }}</span>
      <span>/</span>
      <code>{{ data.task_id }}</code>
    </nav>

    <header class="run-page-heading">
      <div>
        <p class="masthead-kicker">GENERATION RUN</p>
        <h1>{{ data.goal?.target_entity || "生成任务" }}</h1>
        <code>{{ data.task_id }}</code>
      </div>
      <div class="run-heading-actions">
        <span class="status-pill">{{ data.status_label || data.status }}</span>
        <button
          v-if="data.can_resume"
          type="button"
          class="primary-action"
          data-resume-run
          :disabled="resumeBusy"
          @click="emit('resume')"
        >
          {{ resumeBusy ? "正在继续…" : "从断点继续" }}
        </button>
      </div>
    </header>

    <section class="run-summary-grid" aria-label="运行摘要">
      <div>
        <span>目标上限</span>
        <strong>{{ data.target_question_count ?? "—" }}</strong>
      </div>
      <div>
        <span>候选假设</span>
        <strong>{{ data.candidate_count ?? candidates.length }}</strong>
      </div>
      <div>
        <span>已发布</span>
        <strong>{{ data.published_count ?? questions.length }}</strong>
        <small v-if="data.goal?.difficulty_targets?.length === 1 && ['medium', 'hard'].includes(data.goal.difficulty_targets[0])">
          计入目标难度 {{ data.counted_toward_target ?? '—' }} 道
        </small>
      </div>
      <div>
        <span>主 Agent</span>
        <strong>第 {{ data.main_agent_step_count ?? steps.length }} 步</strong>
      </div>
      <div>
        <span>证据材料</span>
        <strong>{{ data.artifact_counts?.evidence_material ?? 0 }}</strong>
      </div>
    </section>

    <section v-if="resumeContext" class="recovery-banner">
      <div>
        <strong>等待从断点继续</strong>
        <p>
          最后完成：第 {{ resumeContext.lastStep }} 步；
          下一步：第 {{ resumeContext.nextStep }} 步。
        </p>
        <p v-if="data.stop_reason">{{ runStopMessage(data) }}</p>
      </div>
    </section>

    <nav class="detail-tabs" aria-label="运行详情页签">
      <button
        v-for="[key, label] in tabs"
        :key="key"
        type="button"
        :class="{ active: activeTab === key }"
        :data-run-tab="key"
        @click="emit('select-tab', key)"
      >
        {{ label }}
      </button>
    </nav>

    <section v-if="activeTab === 'overview'" class="detail-section">
      <h2>运行概览</h2>
      <dl class="run-config">
        <div>
          <dt>实体</dt>
          <dd>{{ data.goal?.target_entity || "—" }}</dd>
        </div>
        <div>
          <dt>实体 ID</dt>
          <dd>{{ data.goal?.target_entity_id || "—" }}</dd>
        </div>
        <div>
          <dt>难度</dt>
          <dd>{{ (data.goal?.difficulty_targets || []).join(" / ") || "—" }}</dd>
        </div>
        <div>
          <dt>提问风格</dt>
          <dd>{{ (data.goal?.style_targets || []).join(" / ") || "—" }}</dd>
        </div>
        <div>
          <dt>停止原因</dt>
          <dd>{{ runStopMessage(data) || "—" }}</dd>
        </div>
      </dl>
      <div v-if="questions.length" class="overview-questions">
        <h3>已发布题目</h3>
        <button
          v-for="question in questions"
          :key="question.question_id"
          type="button"
          class="overview-question"
          @click="emit('open-question', question)"
        >
          <span>{{ question.text }}</span>
          <small>{{ formatAnswer(question.answer) }}</small>
        </button>
      </div>
    </section>

    <section v-else-if="activeTab === 'execution'" class="detail-section">
      <h2>执行轨迹</h2>
      <p v-if="traceLoading" class="page-state">正在读取主 Agent 决策…</p>
      <p v-else-if="traceError" class="page-state error">{{ traceError }}</p>
      <div v-else-if="steps.length" class="trace-list">
        <article v-for="step in steps" :key="step.decision_id" class="trace-row">
          <span>{{ String(step.sequence).padStart(2, "0") }}</span>
          <div>
            <strong>{{ step.action_label || step.action }}</strong>
            <small>{{ step.target_subagent_role }}</small>
            <p v-if="step.stop_reason">{{ step.stop_reason }}</p>
          </div>
        </article>
      </div>
      <p v-else class="page-state">暂无执行轨迹。</p>
    </section>

    <section v-else-if="activeTab === 'candidates'" class="detail-section">
      <h2>候选假设</h2>
      <p class="section-copy">
        候选假设不是正式题目。这里同时展示核验状态和拒绝原因。
      </p>
      <div v-if="candidates.length" class="detail-card-list">
        <article
          v-for="item in candidates"
          :key="item.candidate_id || item.hypothesis_id || item.question_id"
          class="detail-card"
        >
          <div>
            <span class="badge">{{ statusText(item) }}</span>
            <h3>{{ item.text || item.question_intent || "候选题尚未形成题面" }}</h3>
            <p v-if="item.reason">{{ item.reason }}</p>
          </div>
          <code>{{ item.hypothesis_id || item.question_id }}</code>
        </article>
      </div>
      <p v-else class="page-state">没有候选假设。</p>
    </section>

    <section v-else-if="activeTab === 'evidence'" class="detail-section">
      <h2>证据材料</h2>
      <div v-if="data.retrieval_sources?.length" class="detail-card-list">
        <article
          v-for="source in data.retrieval_sources"
          :key="`${source.provider_id}-${source.query_label}-${source.search_round}`"
          class="detail-card"
        >
          <div>
            <strong>{{ source.provider_label || source.provider_id }}</strong>
            <p>{{ source.query_label || "未记录查询" }}</p>
            <small>
              {{ source.terminal_status }} · 材料 {{ source.material_count }}
            </small>
            <p v-if="source.error_category || source.error_message" role="status">
              {{ source.error_category }}
              {{ source.error_message || "该记录未保存具体错误详情。" }}
            </p>
          </div>
        </article>
      </div>
      <p v-else class="page-state">暂无检索来源。</p>


    </section>

    <section v-else-if="activeTab === 'questions'" class="detail-section">
      <h2>已发布题目</h2>
      <div v-if="questions.length" class="detail-card-list">
        <article
          v-for="question in questions"
          :key="question.question_id"
          class="detail-card published-card"
        >
          <div>
            <span class="badge">{{ question.status_label || "已发布" }}</span>
            <span v-if="data.goal?.difficulty_targets?.length === 1 && ['medium', 'hard'].includes(data.goal.difficulty_targets[0]) && question.assessed_difficulty && !data.goal.difficulty_targets.includes(question.assessed_difficulty)" class="badge">附加题 · 不计入目标难度</span>
            <h3>{{ question.text }}</h3>
            <p><strong>答案：</strong>{{ formatAnswer(question.answer) }}</p>
            <p v-if="question.explanation">{{ question.explanation }}</p>
          </div>
          <button
            type="button"
            class="quiet-action"
            @click="emit('open-question', question)"
          >
            查看题目详情
          </button>
        </article>
      </div>
      <p v-else class="page-state">本次运行没有已发布题目。</p>
    </section>

    <section v-else class="detail-section">
      <h2>诊断与导出</h2>
      <div v-if="interruptions.length" class="detail-card-list">
        <details
          v-for="{ event, current } in interruptions"
          :key="`${event.at}-${event.resume_after_step}`"
          :open="current"
        >
          <summary>
            <strong>{{ current ? "当前中断" : "历史中断" }}</strong>
            <time v-if="event.at" :datetime="event.at">{{ eventTime(event.at) }}</time>
          </summary>
          <div>
            <strong>{{ event.code || "运行中断" }}</strong>
            <p>{{ event.message || event.stop_reason }}</p>
            <small v-if="event.provider_response">
              {{ event.provider_response }}
            </small>
          </div>
          <small v-if="event.attempts">请求尝试 {{ event.attempts }} 次</small>
        </details>
      </div>
      <dl class="artifact-count-list">
        <div v-for="[key, count] in artifactCounts" :key="key">
          <dt>{{ key }}</dt>
          <dd>{{ count }}</dd>
        </div>
      </dl>
      <p v-if="data.stop_reason" class="diagnostic-reason">
        {{ runStopMessage(data) }}
      </p>
      <button
        v-if="data.can_resume"
        type="button"
        class="primary-action"
        :disabled="resumeBusy"
        @click="emit('resume')"
      >
        {{ resumeBusy ? "正在继续…" : "从断点继续" }}
      </button>
    </section>
  </main>
  <main v-else class="page-shell run-detail-page">
    <nav class="breadcrumbs" aria-label="面包屑">
      <button type="button" class="link-button" @click="emit('back')">
        生成记录
      </button>
      <span>/</span>
      <span>生成任务</span>
    </nav>
    <p v-if="traceLoading" class="page-state">正在读取生成详情…</p>
    <p v-else class="page-state error">
      {{ traceError || "生成任务不存在或暂时无法读取。" }}
    </p>
  </main>
</template>
