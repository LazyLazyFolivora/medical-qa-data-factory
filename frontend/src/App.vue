<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import EntityPicker from "./components/EntityPicker.vue";
import GenerationComposer from "./components/GenerationComposer.vue";
import ObjectiveRun from "./components/ObjectiveRun.vue";
import TaskRun from "./components/TaskRun.vue";

const loading = ref(true);
const fatalError = ref("");
const entityError = ref("");
const entities = ref([]);
const selectedEntityId = ref("");
const tasks = ref([]);
const selectedTask = ref(null);
const questions = ref([]);
const model = reactive({ ready: false, detail: "" });
const questionStyles = ref([]);
const starting = ref(false);
const runNotice = ref("");
const taskSection = ref(null);
let progressTimer = null;

const selectedEntity = computed(
  () => entities.value.find((item) => item.value === selectedEntityId.value) ?? null,
);

const taskIsRunning = computed(
  () =>
    starting.value ||
    ["pending", "running"].includes(selectedTask.value?.status),
);

const currentUrl = (entityId, taskId) => {
  const url = new URL(window.location.href);
  if (entityId) url.searchParams.set("entity", entityId);
  else url.searchParams.delete("entity");
  if (taskId) url.searchParams.set("task", taskId);
  else url.searchParams.delete("task");
  return `${url.pathname}${url.search}`;
};

const applyWorkspace = (payload, { updateUrl = true } = {}) => {
  entities.value = payload.entities || [];
  selectedEntityId.value = payload.selected_entity_id || "";
  tasks.value = payload.tasks || [];
  selectedTask.value = payload.selected_task || null;
  questions.value = payload.questions || [];
  questionStyles.value = payload.question_styles || [];
  entityError.value = payload.entity_error || "";
  model.ready = Boolean(payload.model?.ready);
  model.detail = payload.model?.detail || "";
  if (updateUrl && selectedEntityId.value) {
    window.history.replaceState(
      {},
      "",
      currentUrl(selectedEntityId.value, selectedTask.value?.task_id),
    );
  }
};

const loadWorkspace = async (params = {}) => {
  const search = new URLSearchParams(window.location.search);
  Object.entries(params).forEach(([key, value]) => {
    if (value) search.set(key, value);
    else search.delete(key);
  });
  const response = await fetch(`/api/generation-workspace?${search}`, {
    cache: "no-store",
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "无法读取生成工作台");
  applyWorkspace(payload);
  return payload;
};

const selectEntity = async (entity) => {
  if (taskIsRunning.value) return;
  selectedEntityId.value = entity.value;
  selectedTask.value = null;
  questions.value = [];
  runNotice.value = "";
  try {
    await loadWorkspace({ entity: entity.value, task: "" });
  } catch (error) {
    fatalError.value = error.message || String(error);
  }
};

const selectTask = async (taskId) => {
  if (!taskId || taskId === selectedTask.value?.task_id) return;
  try {
    await loadWorkspace({ task: taskId });
  } catch (error) {
    runNotice.value = `无法切换任务：${error.message || error}`;
  }
};

const pollProgress = async (taskId, target) => {
  try {
    const response = await fetch(
      `/api/generation/${encodeURIComponent(taskId)}`,
      { cache: "no-store" },
    );
    const progress = await response.json();
    if (!response.ok) throw new Error(progress.error || "无法读取生成进度");
    selectedTask.value = {
      ...(selectedTask.value || {}),
      ...progress,
      status: progress.status,
      status_label: progress.status_label,
      task_id: taskId,
      target_question_count: target,
      detail_url: `/tasks/${taskId}`,
      operations: progress.operations || [],
      planner_steps: progress.planner_steps || [],
      question_count: progress.question_count || 0,
    };
    runNotice.value =
      progress.current_step?.label ||
      progress.status_label ||
      "正在启动生成任务";
    return progress;
  } catch (error) {
    runNotice.value = `进度读取暂时失败：${error.message || error}`;
    return null;
  }
};

const pollObjectiveProgress = async (taskId, target) => {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(
      `/api/objective/${encodeURIComponent(taskId)}`,
      { cache: "no-store", signal: controller.signal },
    );
    const progress = await response.json();
    if (!response.ok) throw new Error(progress.error || "无法读取主 Agent 进度");
    selectedTask.value = {
      ...(selectedTask.value || {}),
      ...progress,
      engine: "objective",
      task_id: taskId,
      target_question_count: target,
      detail_url: `/?task=${taskId}`,
    };
    questions.value = progress.questions || [];
    runNotice.value = progress.stalled
      ? `主 Agent 已经 ${Math.floor(
          (progress.seconds_since_progress || 0) / 60,
        )} 分钟没有新进展，任务可能已经卡住。`
      : progress.status === "running"
        ? `主 Agent 正在执行：${
            progress.main_agent_steps?.at(-1)?.action_label || "规划下一步"
          }`
        : progress.status_label;
    return progress;
  } catch (error) {
    runNotice.value =
      error?.name === "AbortError"
        ? "读取主 Agent 进度超时，后台暂时没有响应。"
        : `进度读取失败：${error.message || error}`;
    return null;
  } finally {
    window.clearTimeout(timeout);
  }
};

const startGeneration = async (configuration) => {
  if (!configuration.entity || taskIsRunning.value) return;
  starting.value = true;
  runNotice.value = "正在启动生成任务";
  const taskId = window.crypto?.randomUUID
    ? `web-${window.crypto.randomUUID()}`
    : `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  selectedTask.value = {
    task_id: taskId,
    engine: "objective",
    status: "pending",
    status_label: "准备生成",
    target_question_count: configuration.questionCount,
    question_count: 0,
    operations: [],
    planner_steps: [],
    detail_url: `/tasks/${taskId}`,
  };
  questions.value = [];
  window.clearInterval(progressTimer);
  progressTimer = window.setInterval(
    () => pollObjectiveProgress(taskId, configuration.questionCount),
    800,
  );
  await pollObjectiveProgress(taskId, configuration.questionCount);
  taskSection.value?.scrollIntoView({ behavior: "smooth", block: "start" });

  try {
    const response = await fetch("/api/objective/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        run_id: taskId,
        entity_canonical_id: configuration.entity.canonical_id,
        entity_name:
          configuration.entity.canonical_name || configuration.entity.name,
        entity_type: configuration.entity.entity_type,
        question_count: configuration.questionCount,
        max_actions: 100,
        providers: [
          "hgnc",
          "ncbi_gene",
          "ensembl",
          "primekg",
          "pubmed",
        ],
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "生成失败");
    selectedTask.value = { ...payload, engine: "objective" };
    questions.value = payload.questions || [];
    await loadWorkspace({ task: taskId });
    runNotice.value =
      payload.published_count > 0
        ? `主 Agent 发布了 ${payload.published_count} 道题`
        : "主 Agent 已完成，但没有题目通过发布门禁";
  } catch (error) {
    runNotice.value = `生成失败：${error.message || error}`;
    try {
      await loadWorkspace({ task: taskId });
    } catch {
      selectedTask.value = {
        ...selectedTask.value,
        status: "failed",
        status_label: "失败",
      };
    }
  } finally {
    window.clearInterval(progressTimer);
    starting.value = false;
  }
};

onMounted(async () => {
  try {
    await loadWorkspace();
  } catch (error) {
    fatalError.value = error.message || String(error);
  } finally {
    loading.value = false;
  }
});

onBeforeUnmount(() => window.clearInterval(progressTimer));
</script>

<template>
  <div class="generation-vue" data-vue-generation-app>
    <header class="generation-masthead">
      <div>
        <p class="masthead-kicker">QUESTION GENERATION</p>
        <h1>从医学实体生成可核验的问题</h1>
        <p class="masthead-copy">
          选择一个起点，定义问题范围。系统会收集证据、验证候选，并把过程和结果固定在同一次任务中。
        </p>
      </div>
      <div class="masthead-steps" aria-label="生成流程">
        <span class="active"><b>1</b> 选择实体</span>
        <span><b>2</b> 运行生成</span>
        <span><b>3</b> 审查结果</span>
      </div>
    </header>

    <div v-if="loading" class="app-loading">
      <span class="working-pulse"></span>
      正在加载生成工作台…
    </div>

    <div v-else-if="fatalError" class="fatal-state">
      <p class="field-kicker">无法加载</p>
      <h2>生成工作台暂时不可用</h2>
      <p>{{ fatalError }}</p>
      <button type="button" @click="location.reload()">重新加载</button>
    </div>

    <template v-else>
      <section class="composer-panel" data-generation-workspace>
        <p v-if="entityError" class="entity-global-error">
          PrimeKG 实体读取失败：{{ entityError }}
        </p>
        <EntityPicker
          :entities="entities"
          :selected-id="selectedEntityId"
          :disabled="taskIsRunning"
          @select="selectEntity"
        />
        <GenerationComposer
          :selected-entity="selectedEntity"
          :model="model"
          :styles="questionStyles"
          :running="taskIsRunning"
          @submit="startGeneration"
        />
      </section>

      <div ref="taskSection">
        <ObjectiveRun
          v-if="selectedTask?.engine === 'objective'"
          :task="selectedTask"
          :tasks="tasks"
          :questions="questions"
          :notice="runNotice"
          @select-task="selectTask"
        />
        <TaskRun
          v-else
          :task="selectedTask"
          :tasks="tasks"
          :questions="questions"
          :running="taskIsRunning"
          :notice="runNotice"
          @select-task="selectTask"
        />
      </div>
    </template>
  </div>
</template>
