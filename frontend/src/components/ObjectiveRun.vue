<script setup>
import { computed } from "vue";

const props = defineProps({
  task: { type: Object, default: null },
  tasks: { type: Array, default: () => [] },
  questions: { type: Array, default: () => [] },
  notice: { type: String, default: "" },
});

const emit = defineEmits(["select-task"]);

const artifactCounts = computed(() =>
  Object.entries(props.task?.artifact_counts || {}).sort(([left], [right]) =>
    left.localeCompare(right),
  ),
);

const reports = computed(() => props.task?.validation_reports || []);
const zeroQuestions = computed(
  () => props.task?.run_state === "COMPLETED_WITH_ZERO_QUESTIONS",
);
const isRunning = computed(() =>
  ["pending", "running"].includes(props.task?.status),
);

const artifactLabel = (key) =>
  ({
    evidence_material: "原始材料",
    evidence_anchor: "原文定位",
    candidate_claim: "候选事实",
    question_hypothesis: "候选题",
    answer_resolution: "答案判定",
    frozen_question_contract: "冻结契约",
    objective_question_draft: "最终题面",
    objective_validation_report: "复核报告",
    provider_result: "检索结果",
  })[key] || key;

const purposeLabel = (value) =>
  ({ discovery: "发现材料", verification: "独立验证" })[value] || value;

const terminalLabel = (value) =>
  ({ SUCCEEDED: "成功", EMPTY: "无结果", FAILED: "失败", UNKNOWN: "未知" })[
    value
  ] || value;

const verdictLabel = (value) =>
  ({ PASS: "通过", REJECT: "拒绝", INDETERMINATE: "无法确定" })[value] ||
  value;
</script>

<template>
  <section
    v-if="task"
    class="objective-run"
    data-objective-run
    :data-run-state="task.status"
  >
    <header class="section-heading run-heading">
      <div>
        <span class="step-index">02</span>
        <div>
          <p class="field-kicker">主 Agent 循环</p>
          <h2>自主决策与证据门禁</h2>
        </div>
      </div>
      <div class="objective-status">
        <label v-if="tasks.length > 1" class="history-select">
          <span>运行历史</span>
          <select
            :value="task.task_id"
            @change="emit('select-task', $event.target.value)"
          >
            <option
              v-for="item in tasks"
              :key="item.task_id"
              :value="item.task_id"
            >
              {{ item.status_label }} · {{ item.task_id.slice(-12) }} ·
              {{ item.question_count || 0 }} 题
            </option>
          </select>
        </label>
        <span class="status-pill">{{ task.status_label }}</span>
        <code>{{ task.task_id }}</code>
      </div>
    </header>

    <div class="objective-overview">
      <div>
        <span>运行状态</span>
        <strong>{{ task.run_state }}</strong>
      </div>
      <div>
        <span>主 Agent 决策</span>
        <strong>{{ task.main_agent_steps?.length || 0 }} 次</strong>
      </div>
      <div>
        <span>发布题数</span>
        <strong>{{ task.published_count || questions.length }}</strong>
      </div>
      <div>
        <span>Commit</span>
        <strong>{{ task.commit_sequence || "—" }}</strong>
      </div>
    </div>

    <p v-if="notice" class="run-notice">{{ notice }}</p>
    <p v-if="task.stop_reason" class="objective-stop">
      <span>停止原因</span>
      {{ task.stop_reason }}
    </p>

    <div class="artifact-strip" aria-label="运行 artifact 数量">
      <div v-for="[key, count] in artifactCounts" :key="key">
        <span>{{ artifactLabel(key) }}</span>
        <strong>{{ count }}</strong>
      </div>
      <p v-if="!artifactCounts.length">主 Agent 尚未产生 artifact。</p>
    </div>

    <details
      class="objective-materials"
      :open="Boolean(task.materials?.length)"
    >
      <summary>
        <span>材料收集</span>
        <span>
          {{ task.retrieval_sources?.length || 0 }} 次检索 ·
          {{ task.materials?.length || 0 }} 份材料
        </span>
      </summary>

      <div v-if="task.retrieval_sources?.length" class="retrieval-source-list">
        <article
          v-for="source in task.retrieval_sources"
          :key="`${source.provider_id}-${source.purpose}-${source.query_label}`"
          data-retrieval-source
        >
          <div>
            <strong>{{ source.provider_label }}</strong>
            <span>{{ purposeLabel(source.purpose) }}</span>
          </div>
          <p>{{ source.query_label }}</p>
          <footer>
            <span>{{ terminalLabel(source.terminal_status) }}</span>
            <span>{{ source.material_count }} 份</span>
            <code v-if="source.snapshot_id">{{ source.snapshot_id }}</code>
          </footer>
        </article>
      </div>

      <div v-if="task.materials?.length" class="collected-material-list">
        <article
          v-for="material in task.materials"
          :key="material.material_id"
          data-collected-material
        >
          <header>
            <div>
              <strong>{{ material.provider_label }}</strong>
              <span>{{ material.material_label }}</span>
            </div>
            <span>{{ purposeLabel(material.purpose) }}</span>
          </header>
          <p class="material-preview">{{ material.preview }}</p>
          <footer>
            <code>{{ material.source_record_id }}</code>
            <span>{{ material.anchor_count }} 个原文定位</span>
          </footer>
        </article>
      </div>

      <p
        v-if="isRunning && !task.materials?.length"
        class="empty-detail material-waiting"
      >
        主 Agent 正在收集材料。检索开始后，这里会显示来源、查询和原文预览。
      </p>
    </details>

    <div v-if="zeroQuestions" class="objective-zero">
      <span>0</span>
      <div>
        <h3>0 题是正常结果</h3>
        <p>
          没有候选同时满足题面自包含、独立答案验证和发布复核。系统不会为了数量降低门禁。
        </p>
      </div>
    </div>

    <div v-else-if="isRunning && !questions.length" class="question-placeholder">
      <span class="working-pulse"></span>
      <div>
        <h3>主 Agent 正在选择下一步</h3>
        <p>查看下方决策轨迹，了解当前在检索、验证还是复核。</p>
      </div>
    </div>

    <section v-if="questions.length" class="objective-results">
      <header class="objective-subheading">
        <p class="field-kicker">Public package</p>
        <h3>已通过发布门禁</h3>
      </header>
      <article
        v-for="(question, index) in questions"
        :key="question.question_id"
        class="objective-question"
      >
        <span class="result-index">{{ String(index + 1).padStart(2, "0") }}</span>
        <div>
          <h3>{{ question.text }}</h3>
          <p class="answer">
            <span>答案</span>
            {{ Array.isArray(question.answer) ? question.answer.join("；") : question.answer }}
          </p>
          <p v-if="question.explanation" class="explanation">
            {{ question.explanation }}
          </p>
        </div>
      </article>
    </section>

    <details class="objective-trace" open>
      <summary>
        <span>主 Agent 决策轨迹</span>
        <span>{{ task.main_agent_steps?.length || 0 }} 次决策</span>
      </summary>
      <ol v-if="task.main_agent_steps?.length" class="objective-agent-list">
        <li
          v-for="step in task.main_agent_steps"
          :key="step.decision_id || step.sequence"
          data-agent-step
        >
          <span class="trace-dot">{{ step.sequence }}</span>
          <div>
            <div class="trace-item-title">
              <strong>{{ step.action_label }}</strong>
              <span>{{ step.information_gain_label }}</span>
            </div>
            <div class="trace-meta">
              <code>{{ step.action }}</code>
              <span>{{ step.target_subagent_role || "停止" }}</span>
              <span>{{ step.state_label }}</span>
            </div>
            <p v-if="step.stop_reason">{{ step.stop_reason }}</p>
          </div>
        </li>
      </ol>
      <p v-else class="empty-detail">主 Agent 尚未提交决策。</p>
    </details>

    <details v-if="reports.length" class="objective-trace">
      <summary>
        <span>发布复核</span>
        <span>{{ reports.length }} 份报告</span>
      </summary>
      <ul class="objective-report-list">
        <li v-for="report in reports" :key="report.report_id">
          <strong>{{ verdictLabel(report.verdict) }}</strong>
          <code>{{ report.primary_reason }}</code>
          <span>{{ report.release_allowed ? "允许发布" : "禁止发布" }}</span>
          <p v-for="finding in report.blocking_findings || []" :key="finding">
            {{ finding }}
          </p>
        </li>
      </ul>
    </details>
  </section>
</template>
