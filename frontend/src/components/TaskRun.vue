<script setup>
import { computed } from "vue";
import ExecutionTrace from "./ExecutionTrace.vue";

const props = defineProps({
  task: { type: Object, default: null },
  tasks: { type: Array, default: () => [] },
  questions: { type: Array, default: () => [] },
  running: { type: Boolean, default: false },
  notice: { type: String, default: "" },
});

const emit = defineEmits(["select-task"]);

const failedOperation = computed(
  () => props.task?.operations?.find((item) => item.status === "failed") ?? null,
);
const failedPlanningStep = computed(
  () =>
    [...(props.task?.planner_steps || [])]
      .reverse()
      .find((item) => item.error_code) ?? null,
);

const taskState = computed(() => {
  if (!props.task) return "idle";
  if (props.task.status === "failed") return "failed";
  if (props.task.status === "partial") return "partial";
  if (props.task.status === "running") return "running";
  if (props.task.status === "completed") return "completed";
  return "pending";
});

const target = computed(() => props.task?.target_question_count || 1);
const completion = computed(() =>
  Math.min(100, Math.round((props.questions.length / target.value) * 100)),
);

const failureMessage = computed(() => {
  if (failedOperation.value?.error_message) {
    return failedOperation.value.error_message;
  }
  if (failedPlanningStep.value?.error_message) {
    return failedPlanningStep.value.error_message;
  }
  if (props.task?.stop_reason) {
    return `任务停止原因：${props.task.stop_reason}`;
  }
  return "任务没有完成，请展开执行过程查看失败节点。";
});

const taskOptionLabel = (task) =>
  `${task.status_label} · ${task.task_id.slice(-12)} · ${task.question_count || 0} 题`;
</script>

<template>
  <section
    class="task-run"
    data-generation-run
    :data-run-task-id="task?.task_id || ''"
    :data-run-state="taskState"
  >
    <header class="section-heading run-heading">
      <div>
        <span class="step-index">02</span>
        <div>
          <p class="field-kicker">当前任务</p>
          <h2>
            {{ task ? "本次生成结果" : "等待生成" }}
          </h2>
        </div>
      </div>

      <div v-if="task" class="run-heading-actions">
        <label v-if="tasks.length > 1" class="history-select">
          <span>历史任务</span>
          <select
            :value="task.task_id"
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
        <a class="quiet-action" :href="task.detail_url">完整任务记录 ↗</a>
      </div>
    </header>

    <div v-if="!task" class="run-idle">
      <span class="idle-glyph" aria-hidden="true">＋</span>
      <div>
        <h3>还没有选择任务</h3>
        <p>选择实体并设置生成范围后，结果和运行过程会固定显示在这里。</p>
      </div>
    </div>

    <template v-else>
      <div class="run-overview" :class="taskState">
        <div class="run-status">
          <span class="status-pill">{{ task.status_label }}</span>
          <code>{{ task.task_id }}</code>
          <span v-if="task.stop_reason">{{ task.stop_reason }}</span>
        </div>
        <div class="run-progress">
          <span>{{ questions.length }} / {{ target }} 题</span>
          <div class="progress-rail">
            <i :style="{ width: `${running ? Math.max(6, completion) : completion}%` }"></i>
          </div>
        </div>
        <p v-if="notice" class="run-notice">{{ notice }}</p>
      </div>

      <div v-if="taskState === 'failed' || taskState === 'partial'" class="run-alert">
        <div>
          <p class="field-kicker">
            {{ taskState === "failed" ? "生成失败" : "未达到目标" }}
          </p>
          <h3>
            {{
              failedOperation
                ? `失败于 ${failedOperation.node}`
                : failedPlanningStep
                  ? `未通过：${failedPlanningStep.action_label}`
                  : "本次任务没有产出完整结果"
            }}
          </h3>
          <p>{{ failureMessage }}</p>
        </div>
        <a :href="task.detail_url">查看诊断详情 →</a>
      </div>

      <div v-if="questions.length" class="result-list">
        <article
          v-for="(question, index) in questions"
          :key="question.question_id"
          class="result-card"
        >
          <div class="result-index">{{ String(index + 1).padStart(2, "0") }}</div>
          <div class="result-content">
            <h3>{{ question.text }}</h3>
            <p class="answer">
              <span>答案</span>
              {{ question.answer }}
            </p>
            <p v-if="question.explanation" class="explanation">
              {{ question.explanation }}
            </p>
            <div class="result-meta">
              <span>{{ question.difficulty_label }}</span>
              <span>{{ question.source_label }}</span>
              <span>{{ question.style_label }}</span>
              <span>{{ question.validation_label }}</span>
            </div>
          </div>
          <a class="result-link" :href="question.review_url">
            审查 <span aria-hidden="true">→</span>
          </a>
        </article>
      </div>

      <div
        v-else-if="taskState === 'running' || taskState === 'pending'"
        class="question-placeholder"
      >
        <span class="working-pulse"></span>
        <div>
          <h3>{{ running ? "正在探索与生成" : "任务已创建" }}</h3>
          <p>完成证据收集、候选验证和题面独立校验后，问题会出现在这里。</p>
        </div>
      </div>

      <ExecutionTrace :task="task" />
    </template>
  </section>
</template>
