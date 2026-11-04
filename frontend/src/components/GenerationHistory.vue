<script setup>
import { runStopMessage } from "../api.js";
import { computed, ref } from "vue";

const props = defineProps({
  runs: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  error: { type: String, default: "" },
});

const emit = defineEmits(["open-run", "new-generation"]);

const query = ref("");
const statusFilter = ref("all");

const filteredRuns = computed(() => {
  const needle = query.value.trim().toLowerCase();
  return props.runs.filter((run) => {
    const entity = run.goal?.target_entity || "";
    const runId = run.task_id || "";
    const matchesSearch =
      !needle ||
      `${entity} ${runId}`.toLowerCase().includes(needle);
    const matchesStatus =
      statusFilter.value === "all" ||
      (statusFilter.value === "resumable"
        ? run.can_resume
        : run.status === statusFilter.value);
    return matchesSearch && matchesStatus;
  });
});

const statusClass = (run) => {
  if (run.can_resume) return "failed";
  if (run.status === "completed") return "done";
  if (["failed", "stalled"].includes(run.status)) return "failed";
  return "running";
};

const publishedCount = (run) =>
  run.published_count ?? run.question_count ?? 0;

const candidateCount = (run) =>
  run.candidate_count ??
  run.hypothesis_count ??
  (run.main_agent_step_count || 0);
</script>

<template>
  <main class="page-shell history-page" data-generation-history>
    <header class="page-heading">
      <div>
        <p class="masthead-kicker">GENERATION HISTORY</p>
        <h1>生成记录</h1>
        <p>
          每次生成都有固定记录。候选假设、运行步骤和最终发布题目分别统计。
        </p>
      </div>
      <button type="button" class="primary-action" @click="emit('new-generation')">
        新建生成
      </button>
    </header>

    <section class="history-toolbar" aria-label="生成记录筛选">
      <label>
        <span>搜索实体或 Run ID</span>
        <input
          v-model="query"
          type="search"
          data-history-search
          placeholder="例如 PHYHIP 或 web-6275…"
        />
      </label>
      <label>
        <span>状态</span>
        <select v-model="statusFilter">
          <option value="all">全部状态</option>
          <option value="running">运行中</option>
          <option value="completed">已完成</option>
          <option value="failed">失败或暂停</option>
          <option value="resumable">等待从断点继续</option>
        </select>
      </label>
    </section>

    <p v-if="loading" class="page-state">正在读取生成记录…</p>
    <p v-else-if="error" class="page-state error">{{ error }}</p>
    <div v-else-if="!filteredRuns.length" class="empty-state history-empty">
      <h2>没有匹配的生成记录</h2>
      <p>调整筛选条件，或创建一次新的生成。</p>
    </div>

    <section v-else class="history-list" aria-label="生成任务列表">
      <article
        v-for="run in filteredRuns"
        :key="run.task_id"
        class="history-row"
        :data-run-status="run.status"
      >
        <div class="history-primary">
          <span class="state-dot" :class="statusClass(run)"></span>
          <div>
            <h2>{{ run.goal?.target_entity || "未知实体" }}</h2>
            <code>{{ run.task_id }}</code>
          </div>
        </div>

        <div class="history-status">
          <strong>{{ run.status_label || run.status }}</strong>
          <span v-if="run.current_action?.action_label">
            当前：{{ run.current_action.action_label }}
          </span>
          <span v-else-if="run.can_resume">
            下一步：第 {{ run.next_step || "—" }} 步
          </span>
        </div>

        <div class="history-metrics">
          <span>目标上限 {{ run.target_question_count ?? "—" }}</span>
          <span>候选假设 {{ candidateCount(run) }}</span>
          <span>已发布 {{ publishedCount(run) }}</span>
        </div>

        <div class="history-time">
          <span>{{ run.last_commit_at || run.updated_at || "未知时间" }}</span>
          <span v-if="run.stop_reason">{{ runStopMessage(run) }}</span>
        </div>

        <button
          type="button"
          class="quiet-action"
          data-open-run
          @click="emit('open-run', run.task_id)"
        >
          查看详情
        </button>
      </article>
    </section>
  </main>
</template>
