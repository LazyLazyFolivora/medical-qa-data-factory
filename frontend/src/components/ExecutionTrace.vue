<script setup>
import { computed } from "vue";

const props = defineProps({
  task: { type: Object, default: null },
});

const operations = computed(() => props.task?.operations || []);
const plannerSteps = computed(() => props.task?.planner_steps || []);
const hasFailure = computed(
  () =>
    ["failed", "partial"].includes(props.task?.status) ||
    operations.value.some((item) => item.status === "failed"),
);

const statusIcon = (status) =>
  status === "completed" ? "✓" : status === "failed" ? "!" : "•";

const failureLabel = (step) => {
  if (step.action_type === "candidate_validation") {
    return "候选校验未通过";
  }
  if (step.policy_result === "rejected") {
    return "动作被策略拒绝";
  }
  return `失败于 ${step.node}`;
};
</script>

<template>
  <details class="execution-drawer" :open="hasFailure">
    <summary>
      <span>查看本次执行过程</span>
      <span>{{ operations.length }} 个步骤 · {{ plannerSteps.length }} 轮决策</span>
    </summary>

    <div class="trace-columns">
      <section class="trace-panel">
        <header>
          <p class="field-kicker">流水线</p>
          <h3>系统执行</h3>
        </header>
        <ol v-if="operations.length" class="trace-list">
          <li
            v-for="operation in operations"
            :key="operation.sequence"
            class="trace-item"
            :class="operation.status"
          >
            <span class="trace-dot">{{ statusIcon(operation.status) }}</span>
            <div>
              <div class="trace-item-title">
                <strong>{{ operation.label }}</strong>
                <span>{{ operation.status_label }}</span>
              </div>
              <p>{{ operation.description }}</p>
              <div class="trace-meta">
                <code>{{ operation.node }}</code>
                <span v-if="operation.tool_call_count">
                  {{ operation.tool_call_count }} 次工具调用
                </span>
                <span v-if="operation.records_returned">
                  {{ operation.records_returned }} 条返回
                </span>
                <span>{{ operation.elapsed_label }}</span>
              </div>
              <div v-if="operation.error_code" class="inline-error">
                <strong>失败于 {{ operation.node }}</strong>
                <code>{{ operation.error_code }}</code>
                <p>{{ operation.error_message || "该步骤未保存详细错误信息。" }}</p>
              </div>
              <details
                v-if="operation.tool_calls?.length"
                class="tool-call-drawer"
              >
                <summary>工具调用 {{ operation.tool_calls.length }}</summary>
                <div
                  v-for="call in operation.tool_calls"
                  :key="call.operation_key"
                  class="tool-call"
                >
                  <strong>{{ call.tool_name }} · {{ call.status_label }}</strong>
                  <code>{{ call.operation_key }}</code>
                  <span>{{ call.records_returned }} 条 · {{ call.elapsed_label }}</span>
                  <p v-if="call.error_code">{{ call.error_code }} {{ call.message }}</p>
                </div>
              </details>
            </div>
          </li>
        </ol>
        <p v-else class="empty-detail">任务尚未产生执行步骤。</p>
      </section>

      <section class="trace-panel agent-panel">
        <header>
          <p class="field-kicker">Agent</p>
          <h3>编排与查询</h3>
        </header>
        <ol v-if="plannerSteps.length" class="trace-list">
          <li
            v-for="step in plannerSteps"
            :key="`${step.turn}-${step.phase}-${step.action_type}`"
            class="trace-item agent-step-row"
            :class="{ failed: step.policy_result === 'rejected' }"
          >
            <span class="trace-dot">{{ step.turn }}</span>
            <div>
              <div class="trace-item-title">
                <strong>{{ step.action_label }}</strong>
                <span>{{ step.status_label }}</span>
              </div>
              <p>{{ step.expected_information || "未说明预期信息" }}</p>
              <div class="trace-meta">
                <code>{{ step.action_type }}</code>
                <span v-if="step.evidence_gain">
                  +{{ step.evidence_gain }} 条证据
                </span>
                <span>{{ step.policy_result }}</span>
              </div>
              <pre v-if="step.parameters_json" class="payload">{{ step.parameters_json }}</pre>
              <div v-if="step.error_code" class="inline-error">
                <strong>{{ failureLabel(step) }}</strong>
                <code>{{ step.error_code }}</code>
                <p>{{ step.error_message || "未保存详细错误信息。" }}</p>
              </div>
            </div>
          </li>
        </ol>
        <p v-else class="empty-detail">
          这次任务没有形成有效的 Agent 动作；上方流水线会标明失败位置。
        </p>
      </section>
    </div>
  </details>
</template>
