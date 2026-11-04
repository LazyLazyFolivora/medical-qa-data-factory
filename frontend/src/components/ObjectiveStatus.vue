<script setup>
import { computed } from "vue";

const props = defineProps({
  task: { type: Object, default: null },
  tasks: { type: Array, default: () => [] },
  workbench: { type: Object, default: null },
  notice: { type: String, default: "" },
  running: { type: Boolean, default: false },
});

const emit = defineEmits(["select-task", "load-details"]);

const activeWorkbench = computed(() =>
  props.workbench?.task_id === props.task?.task_id ? props.workbench : null,
);
const target = computed(() => props.task?.target_question_count || 0);
const published = computed(
  () => props.task?.published_count ?? activeWorkbench.value?.published_count ?? 0,
);
const progress = computed(() => {
  if (props.task?.is_terminal || props.task?.status === "completed") return 100;
  if (!target.value) return props.running ? 8 : 0;
  if (props.running) {
    return Math.min(92, Math.max(8, published.value / target.value * 100));
  }
  return Math.min(100, Math.round(published.value / target.value * 100));
});
const taskInHistory = computed(() =>
  props.tasks.some((item) => item.task_id === props.task?.task_id),
);
const stateClass = computed(() => {
  if (!props.task) return "idle";
  if (props.task.status === "completed") return "done";
  if (props.task.status === "failed" || props.task.status === "stalled") return "failed";
  return "running";
});
const gateLabel = computed(() => {
  if (!props.task) return "等待执行";
  if (props.running) return props.task.stalled ? "等待恢复" : "尚未完成";
  if (props.task.status === "failed") return "未完成";
  return published.value > 0 ? `${published.value} 题通过` : "0 题通过";
});
const materialCount = computed(
  () =>
    activeWorkbench.value?.artifact_counts?.evidence_material ??
    props.task?.artifact_counts?.evidence_material ??
    null,
);
const currentAction = computed(
  () => props.task?.current_action || activeWorkbench.value?.current_action || null,
);
const currentQuery = computed(() => {
  const query = currentAction.value?.parameters?.query;
  if (!query) return "";
  if (typeof query === "string") return query;
  return Object.entries(query)
    .map(([key, value]) => `${key}=${value}`)
    .join(" · ");
});
const taskOptionLabel = (task) =>
  `${task.status_label} · ${task.task_id.slice(-10)} · ${task.question_count ?? task.published_count ?? 0} 题`;
</script>

<template>
  <section
    class="workbench-panel status-panel"
    aria-labelledby="run-title"
    data-objective-status
  >
    <header class="panel-heading">
      <div>
        <p class="panel-step">02 / 实时状态</p>
        <h2 id="run-title">本次运行</h2>
      </div>
      <label
        v-if="tasks.length > 1 && task && taskInHistory"
        class="history-select"
      >
        <span class="sr-only">切换历史任务</span>
        <select
          :value="task.task_id"
          aria-label="切换历史任务"

          @change="emit('select-task', $event.target.value)"
        >
          <option
            v-for="item in tasks"
            :key="item.task_id"
            :value="item.task_id"
          >
            {{ taskOptionLabel(item) }}
          </option>
        </select>
      </label>
    </header>

    <div class="run-state">
      <span class="state-dot" :class="stateClass"></span>
      <span role="status" aria-live="polite">
        {{ task?.status_label || "等待开始" }}
      </span>
    </div>

    <div class="run-number">
      <span>{{ published }}</span>
      <small>/ {{ target || "—" }}</small>
    </div>
    <p class="metric-label">通过发布复核的问题</p>
    <div
      class="progress"
      role="progressbar"
      :aria-valuenow="progress"
      aria-valuemin="0"
      aria-valuemax="100"
      :aria-label="`运行进度 ${progress}%`"
    >
      <span :style="{ width: `${progress}%` }"></span>
    </div>
    <p class="run-note">
      {{ notice || "开始后，这里会显示检索、验证与发布复核状态。" }}
    </p>

    <div class="run-facts">
      <div>
        <span>证据材料</span>
        <strong>{{ materialCount == null ? "—" : `${materialCount} 份` }}</strong>
      </div>
      <div>
        <span>当前动作</span>
        <strong>{{ currentAction?.action_label || gateLabel }}</strong>
      </div>
      <div>
        <span>发布门禁</span>
        <strong>{{ gateLabel }}</strong>
      </div>
    </div>
    <p v-if="currentQuery" class="run-query">
      本轮输入：{{ currentQuery }}
    </p>

    <div v-if="task" class="status-actions">
      <code :title="task.task_id">{{ task.task_id }}</code>
      <button type="button" class="quiet-action" @click="emit('load-details')">
        运行详情
      </button>
    </div>
  </section>
</template>
