<script setup>
import { runStopMessage } from "../api.js";
import { computed } from "vue";

const props = defineProps({
  task: { type: Object, required: true },
  workbench: { type: Object, default: null },
  questions: { type: Array, default: () => [] },
  notice: { type: String, default: "" },
  evidence: { type: Object, default: null },
  evidenceLoading: { type: Boolean, default: false },
  evidenceError: { type: String, default: "" },
  selectedEvidenceId: { type: String, default: "" },
  detailsOpen: { type: Boolean, default: false },
  detailsError: { type: String, default: "" },
});

const emit = defineEmits(["open-evidence", "load-details", "close-details"]);

const activeWorkbench = computed(() =>
  props.workbench?.task_id === props.task?.task_id ? props.workbench : null,
);
const isRunning = computed(() =>
  typeof props.task?.is_terminal === "boolean"
    ? !props.task.is_terminal
    : ["pending", "running", "stalled"].includes(props.task?.status),
);
const zeroQuestions = computed(
  () =>
    !isRunning.value &&
    props.questions.length === 0 &&
    props.task?.status !== "failed",
);
const candidateQuestions = computed(
  () => activeWorkbench.value?.candidate_questions || [],
);
const rejectedQuestions = computed(
  () => activeWorkbench.value?.rejected_questions || [],
);
const indeterminateQuestions = computed(
  () => activeWorkbench.value?.indeterminate_questions || [],
);
const otherCandidates = computed(() => [
  ...candidateQuestions.value,
  ...rejectedQuestions.value,
  ...indeterminateQuestions.value,
]);
const providerRows = computed(() => {
  return (
    props.task?.retrieval_sources ||
    activeWorkbench.value?.retrieval_sources ||
    []
  );
});
const artifactCounts = computed(
  () =>
    Object.entries(
      props.task?.artifact_counts ||
        activeWorkbench.value?.artifact_counts ||
        {},
    ).sort(([left], [right]) => left.localeCompare(right)),
);
const evidenceItems = computed(() => props.evidence?.evidence || []);
const supportingEvidence = computed(() =>
  evidenceItems.value.filter((item) => item.role === "supporting"),
);
const contradictingEvidence = computed(() =>
  evidenceItems.value.filter((item) => item.role === "contradicting"),
);

const formatAnswer = (value) =>
  Array.isArray(value) ? value.join("；") : value || "—";
const statusLabel = (value) =>
  ({ candidate: "候选", rejected: "已拒绝", indeterminate: "无法确定" })[
    value
  ] || value;
const candidateText = (item) =>
  item.text || item.question_intent || "候选题尚未形成最终题面";
const validationReason = (validation) => {
  const reason = validation?.primary_reason;
  if (reason === "PASS") return "题面与答案均通过发布复核。";
  if (reason === "REJECT") return "题面或答案未通过发布复核。";
  if (reason === "INDETERMINATE") return "现有证据不足以完成发布判断。";
  return reason || "复核结论未提供说明。";
};
const sourceCountLabel = computed(() => {
  const count = (
    props.task?.retrieval_sources ??
    activeWorkbench.value?.retrieval_sources ??
    []
  ).length;
  return count ? `${count} 次检索` : "暂无检索记录";
});
const formatValue = (value) => {
  if (value == null || value === "") return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
};
const parameterRows = (step) =>
  Object.entries(step.parameters || {}).filter(
    ([, value]) => value !== "" && value != null,
  );
const outputSummary = (step) =>
  Object.entries(step.outputs?.count_labels || {})
    .map(([label, count]) => `${label} ${count}`)
    .join(" · ");
const providerStatusLabel = (status) =>
  ({
    SUCCEEDED: "成功",
    EMPTY: "无结果",
    FAILED: "失败",
    UNKNOWN: "结果未知",
  })[status] || status || "—";
</script>

<template>
  <section
    v-if="task"
    class="objective-results"
    data-objective-run
    :data-run-state="task.status"
    aria-labelledby="results-title"
  >
    <header class="results-heading">
      <div>
        <p class="panel-step">03 / 阅读与审查</p>
        <h2 id="results-title">问题与证据</h2>
      </div>
      <div class="results-heading-meta">
        <span>{{ questions.length }} 题已发布</span>
        <button type="button" class="quiet-action" @click="emit('load-details')">
          技术轨迹
        </button>
      </div>
    </header>

    <div class="results-layout">
      <section class="workbench-panel result-panel" aria-labelledby="published-title">
        <header class="result-toolbar">
          <div>
            <h3 id="published-title">达到发布标准的题目</h3>
            <p>题目、答案与解释均来自本次运行的公开发布包。</p>
          </div>
          <span>{{ questions.length }} 题</span>
        </header>

        <div
          v-if="isRunning && !questions.length"
          class="empty-state result-empty"
          role="status"
        >
          <span class="empty-icon" aria-hidden="true">
            <i class="working-pulse"></i>
          </span>
          <h3>{{ task.status_label || "正在生成" }}</h3>
          <p>
            {{ notice || "主 Agent 正在检索证据、验证答案并执行发布复核。" }}
          </p>
        </div>

        <div v-else-if="zeroQuestions" class="empty-state result-empty">
          <span class="zero-mark" aria-hidden="true">0</span>
          <h3>0 题是正常结果</h3>
          <p>
            这是有效结果。系统不会为了达到目标题数而放宽答案验证或发布标准。
          </p>
          <p v-if="task.stop_reason" class="empty-reason">
            停止原因：{{ runStopMessage(task) }}
          </p>
        </div>

        <div v-else-if="task.status === 'failed'" class="empty-state result-empty">
          <span class="empty-icon failed-mark" aria-hidden="true">!</span>
          <h3>本次运行未完成</h3>
          <p>{{ runStopMessage(task) || notice || "请打开技术轨迹查看失败位置。" }}</p>
        </div>

        <div v-else class="question-list">
          <article
            v-for="(question, index) in questions"
            :key="question.question_id"
            class="question"
          >
            <div class="question-top">
              <span class="question-index">
                QUESTION {{ String(index + 1).padStart(2, "0") }}
              </span>
              <span class="badge">{{ question.status_label || "已发布" }}</span>
            </div>
            <h3>{{ question.text }}</h3>
            <p class="answer">
              <span>参考答案</span>
              {{ formatAnswer(question.answer) }}
            </p>
            <p v-if="question.explanation" class="explanation">
              {{ question.explanation }}
            </p>
            <div class="question-footer">
              <div class="question-facts">
                <span v-if="question.evidence_count">
                  {{ question.evidence_count }} 条支持证据
                </span>
                <span v-if="question.release_allowed">已通过发布复核</span>
              </div>
              <button
                type="button"
                class="evidence-link"
                aria-controls="evidence-panel"
                :aria-expanded="selectedEvidenceId === question.question_id"
                @click="emit('open-evidence', question)"
              >
                {{
                  selectedEvidenceId === question.question_id
                    ? "收起证据"
                    : "查看证据与说明"
                }}
                <span aria-hidden="true">↗</span>
              </button>
            </div>
          </article>
        </div>
      </section>

      <aside
        id="evidence-panel"
        class="workbench-panel research-aside"
        aria-labelledby="aside-title"
        :aria-busy="evidenceLoading"
      >
        <template v-if="evidenceLoading">
          <p class="panel-step">EVIDENCE DOSSIER</p>
          <h3 id="aside-title">正在读取证据</h3>
          <div class="aside-loading">
            <span class="working-pulse"></span>
            正在关联发布结论与原文定位…
          </div>
        </template>

        <template v-else-if="evidenceError">
          <p class="panel-step">EVIDENCE DOSSIER</p>
          <h3 id="aside-title">证据暂时不可用</h3>
          <p class="aside-error">{{ evidenceError }}</p>
        </template>

        <template v-else-if="evidence">
          <p class="panel-step">EVIDENCE DOSSIER</p>
          <div class="aside-title-row">
            <h3 id="aside-title">本题证据</h3>
            <span
              class="badge"
              :class="{ rejected: !evidence.validation?.release_allowed }"
            >
              {{ evidence.validation?.verdict || evidence.question.status_label }}
            </span>
          </div>
          <p class="aside-summary">
            {{ validationReason(evidence.validation) }}
          </p>
          <div class="evidence-group">
            <h4>支持证据</h4>
            <blockquote
              v-for="item in supportingEvidence"
              :key="item.anchor_id"
              class="evidence-quote"
            >
              <p>{{ item.quoted_text }}</p>
              <footer>
                <strong>{{ item.provider_label }}</strong>
                <span>{{ item.source_record_id }}</span>
                <span>{{ item.section_or_field }}</span>
              </footer>
            </blockquote>
            <p v-if="!supportingEvidence.length" class="quiet-copy">
              当前发布包没有返回可定位的支持段落。
            </p>
          </div>
          <div v-if="contradictingEvidence.length" class="evidence-group">
            <h4>冲突证据</h4>
            <blockquote
              v-for="item in contradictingEvidence"
              :key="item.anchor_id"
              class="evidence-quote conflict"
            >
              <p>{{ item.quoted_text }}</p>
              <footer>
                <strong>{{ item.provider_label }}</strong>
                <span>{{ item.source_record_id }}</span>
              </footer>
            </blockquote>
          </div>
          <dl class="dossier-meta">
            <div>
              <dt>运行</dt>
              <dd><code>{{ evidence.run_context?.run_id }}</code></dd>
            </div>
            <div>
              <dt>答案审计</dt>
              <dd><code>{{ evidence.validation?.answer_audit_id }}</code></dd>
            </div>
            <div>
              <dt>题面审计</dt>
              <dd><code>{{ evidence.validation?.public_question_audit_id }}</code></dd>
            </div>
          </dl>
        </template>

        <template v-else>
          <p class="panel-step">RUN EVIDENCE</p>
          <h3 id="aside-title">运行依据</h3>
          <p class="aside-summary">
            选择一道题查看答案如何由原文支持，以及它通过了哪些发布门禁。
          </p>
          <dl class="run-dossier-stats">
            <div>
              <dt>候选</dt>
              <dd>{{ workbench?.candidate_count ?? "—" }}</dd>
            </div>
            <div>
              <dt>已拒绝</dt>
              <dd>{{ workbench?.rejected_count ?? "—" }}</dd>
            </div>
            <div>
              <dt>无法确定</dt>
              <dd>{{ workbench?.indeterminate_count ?? "—" }}</dd>
            </div>
          </dl>
          <div class="source-list">
            <div class="source-list-heading">
              <h4>检索来源</h4>
              <span>{{ sourceCountLabel }}</span>
            </div>
            <article
              v-for="source in providerRows"
              :key="`${source.provider_id}-${source.purpose}-${source.search_round}-${source.query_label}-${source.material_count}`"
              class="source-row"
            >
              <div>
                <strong>{{ source.provider_label || source.provider_id }}</strong>
                <span>
                  {{ source.purpose === "verification" ? "独立验证" : "发现材料" }}
                  <template v-if="source.search_round">
                    · 第 {{ source.search_round }} 轮
                  </template>
                </span>
              </div>
              <p>{{ source.query_label || "未记录查询" }}</p>
              <small>
                {{ providerStatusLabel(source.terminal_status) }}
                · 保留 {{ source.material_count }} 份
                <template v-if="source.candidate_count != null">
                  · 候选 {{ source.candidate_count }}
                </template>
              </small>
              <q v-if="source.search_rationale">
                {{ source.search_rationale }}
              </q>
            </article>
            <p v-if="!providerRows.length" class="quiet-copy">
              完成检索后，这里会显示材料来源。
            </p>
          </div>
          <details v-if="artifactCounts.length" class="artifact-details">
            <summary>运行产物</summary>
            <dl>
              <div v-for="[key, count] in artifactCounts" :key="key">
                <dt>{{ key }}</dt>
                <dd>{{ count }}</dd>
              </div>
            </dl>
          </details>
          <details
            v-if="task.materials?.length"
            class="artifact-details material-details"
          >
            <summary>材料原文预览</summary>
            <article
              v-for="material in task.materials"
              :key="material.material_id"
              data-collected-material
            >
              <header>
                <strong>{{ material.provider_label }}</strong>
                <span>
                  {{ material.material_label }}
                  <template v-if="material.purposes?.length > 1">
                    · 发现与验证共用
                  </template>
                </span>
              </header>
              <p>{{ material.preview }}</p>
              <code>{{ material.source_record_id }}</code>
            </article>
          </details>
        </template>
      </aside>
    </div>

    <details v-if="otherCandidates.length" class="candidate-review">
      <summary>
        <span>候选与未发布结果</span>
        <span>{{ otherCandidates.length }} 项</span>
      </summary>
      <div class="candidate-list">
        <article
          v-for="item in otherCandidates"
          :key="item.candidate_id || item.hypothesis_id"
          class="candidate-row"
        >
          <span class="candidate-status" :class="item.status">
            {{ statusLabel(item.status) }}
          </span>
          <div>
            <h4>{{ candidateText(item) }}</h4>
            <p v-if="item.reason">{{ item.reason }}</p>
            <p v-else-if="item.hypothesis_state">
              当前状态：{{ item.hypothesis_state }}
            </p>
          </div>
          <code>{{ item.question_id || item.hypothesis_id }}</code>
        </article>
      </div>
    </details>

    <section
      v-if="detailsOpen"
      class="technical-details"
      data-run-details
      aria-labelledby="technical-details-title"
    >
      <header class="technical-details-heading">
        <div>
          <p class="panel-step">RUN DETAILS</p>
          <h3 id="technical-details-title">运行详情</h3>
        </div>
        <button type="button" class="quiet-action" @click="emit('close-details')">
          收起
        </button>
      </header>
      <p v-if="detailsError" class="aside-error">{{ detailsError }}</p>
      <dl class="run-detail-facts">
        <div>
          <dt>运行状态</dt>
          <dd>{{ task.status_label || task.status }}</dd>
        </div>
        <div>
          <dt>内部状态</dt>
          <dd>{{ task.run_state || "—" }}</dd>
        </div>
        <div>
          <dt>提交序号</dt>
          <dd>{{ task.commit_sequence ?? "—" }}</dd>
        </div>
        <div>
          <dt>停止原因</dt>
          <dd>{{ runStopMessage(task) || "尚无停止原因" }}</dd>
        </div>
      </dl>
      <h4
        v-if="task.main_agent_steps?.length"
        class="technical-step-title"
      >
        主 Agent 决策轨迹
      </h4>
      <ol
        v-if="task.main_agent_steps?.length"
        class="technical-step-list"
      >
        <li
          v-for="step in task.main_agent_steps"
          :key="step.decision_id || step.sequence"
          data-agent-step
        >
          <span>{{ step.sequence }}</span>
          <div>
            <strong>{{ step.action_label }}</strong>
            <p>
              {{ step.target_subagent_role || "停止" }}
              · 预期信息增益 {{ step.information_gain_label || "—" }}
            </p>
            <dl
              v-if="parameterRows(step).length"
              class="technical-parameters"
            >
              <div
                v-for="[key, value] in parameterRows(step)"
                :key="key"
              >
                <dt>{{ key }}</dt>
                <dd>{{ formatValue(value) }}</dd>
              </div>
            </dl>
            <details
              v-if="step.outputs && Object.keys(step.outputs.counts || {}).length"
              class="agent-output"
            >
              <summary>{{ outputSummary(step) || "查看后端产出" }}</summary>
              <div
                v-if="step.outputs.providers?.length"
                class="agent-output-group"
              >
                <h5>数据源调用</h5>
                <article
                  v-for="provider in step.outputs.providers"
                  :key="`${provider.provider_id}-${provider.purpose}`"
                >
                  <strong>{{ provider.provider_label }}</strong>
                  <span>
                    {{ provider.purpose === "verification" ? "独立验证" : "发现材料" }}
                    · {{ providerStatusLabel(provider.terminal_status) }}
                  </span>
                  <p>{{ provider.query_label || "未记录查询" }}</p>
                  <small>
                    保留 {{ provider.material_count }} 份；候选
                    {{ provider.candidate_count }}；搜索
                    {{ provider.search_trace?.length || 0 }} 次
                  </small>
                  <q v-if="provider.search_rationale">
                    {{ provider.search_rationale }}
                  </q>
                </article>
              </div>
              <div
                v-if="step.outputs.claims?.length"
                class="agent-output-group"
              >
                <h5>抽取出的事实</h5>
                <article
                  v-for="claim in step.outputs.claims"
                  :key="claim.claim_id"
                >
                  <strong>{{ claim.subject }}</strong>
                  <span>{{ claim.predicate }}</span>
                  <p>{{ formatValue(claim.object_or_value) }}</p>
                  <small>
                    {{ claim.claim_strength }} · {{ claim.polarity }} ·
                    {{ claim.supporting_anchor_ids?.length || 0 }} 个锚点
                  </small>
                </article>
              </div>
              <div
                v-if="step.outputs.hypotheses?.length"
                class="agent-output-group"
              >
                <h5>设计出的候选题</h5>
                <article
                  v-for="hypothesis in step.outputs.hypotheses"
                  :key="hypothesis.hypothesis_id"
                >
                  <p>{{ hypothesis.question_intent }}</p>
                  <small>
                    答案类型 {{ hypothesis.answer_type }} ·
                    {{ hypothesis.candidate_claim_ids?.length || 0 }} 条事实
                  </small>
                </article>
              </div>
              <div
                v-if="step.outputs.resolutions?.length"
                class="agent-output-group"
              >
                <h5>答案核验</h5>
                <article
                  v-for="resolution in step.outputs.resolutions"
                  :key="resolution.resolution_id"
                >
                  <strong>{{ resolution.status }}</strong>
                  <p>{{ formatAnswer(resolution.answer_values) }}</p>
                  <small>
                    来源 {{ resolution.verification_sources?.join("、") || "—" }}
                    · {{ resolution.completeness_basis || "—" }}
                  </small>
                </article>
              </div>
              <div
                v-if="step.outputs.drafts?.length"
                class="agent-output-group"
              >
                <h5>最终题面</h5>
                <article
                  v-for="draft in step.outputs.drafts"
                  :key="draft.draft_id"
                >
                  <p>{{ draft.question_text }}</p>
                  <small>答案：{{ formatAnswer(draft.answer_values) }}</small>
                </article>
              </div>
              <div
                v-if="step.outputs.validations?.length"
                class="agent-output-group"
              >
                <h5>发布门禁</h5>
                <article
                  v-for="report in step.outputs.validations"
                  :key="report.report_id"
                >
                  <strong>{{ report.verdict }}</strong>
                  <span>
                    {{ report.release_allowed ? "允许发布" : "阻止发布" }}
                  </span>
                  <p>{{ report.primary_reason }}</p>
                  <small v-if="report.blocking_findings?.length">
                    {{ report.blocking_findings.join("；") }}
                  </small>
                </article>
              </div>
              <div
                v-if="step.outputs.failures?.length"
                class="agent-output-group"
              >
                <h5>后端错误</h5>
                <article
                  v-for="failure in step.outputs.failures"
                  :key="failure.failure_id"
                >
                  <strong>{{ failure.error_code }}</strong>
                  <p>{{ failure.error_message }}</p>
                </article>
              </div>
            </details>
          </div>
        </li>
      </ol>
      <div v-else class="empty-detail">
        <strong>尚未产生主 Agent 决策</strong>
        <p>
          运行记录目前只有创建提交。若长时间停留在这里，说明模型调用或后台执行没有继续推进。
        </p>
      </div>
    </section>
  </section>
</template>
