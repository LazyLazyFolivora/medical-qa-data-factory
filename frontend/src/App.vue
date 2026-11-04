<script setup>
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
} from "vue";
import EntityPicker from "./components/EntityPicker.vue";
import GenerationHistory from "./components/GenerationHistory.vue";
import GenerationComposer from "./components/GenerationComposer.vue";
import ObjectiveRun from "./components/ObjectiveRun.vue";
import ObjectiveStatus from "./components/ObjectiveStatus.vue";
import QuestionDetailPage from "./components/QuestionDetailPage.vue";
import RunDetailPage from "./components/RunDetailPage.vue";
import { apiFetch, readApiPayload } from "./api";

const loading = ref(true);
const fatalError = ref("");
const entityError = ref("");
const entities = ref([]);
const selectedEntityId = ref("");
const tasks = ref([]);
const selectedTask = ref(null);
const questions = ref([]);
const objectiveWorkbench = ref(null);
const evidenceDossier = ref(null);
const evidenceLoading = ref(false);
const evidenceError = ref("");
const selectedEvidenceId = ref("");
const taskDetailsOpen = ref(false);
const taskDetailsError = ref("");
const taskDetailsTaskId = ref("");
const routeName = ref("workbench");
const routeRunId = ref("");
const activeRunTab = ref("overview");
const routeQuestionId = ref("");
const historyLoading = ref(false);
const historyError = ref("");
const detailLoading = ref(false);
const detailError = ref("");
const traceLoading = ref(false);
const traceError = ref("");
const resumeBusy = ref(false);
const pauseBusy = ref(false);
const pauseMessage = ref("");
const questionLoading = ref(false);
const questionError = ref("");
const questionDossier = ref(null);
const model = reactive({
  ready: false,
  detail: "",
  default_provider: "",
  providers: [],
});
const questionStyles = ref([]);
const starting = ref(false);
const runNotice = ref("");
const taskSection = ref(null);
const statusSection = ref(null);
let progressTimer = null;
let completedObjectiveTaskId = "";
let selectionRevision = 0;
let evidenceRevision = 0;

const selectedEntity = computed(
  () =>
    entities.value.find((item) => item.value === selectedEntityId.value) ??
    null,
);

const isTerminalTask = (task) => {
  if (!task) return true;
  if (typeof task.is_terminal === "boolean") return task.is_terminal;
  return !["pending", "running", "stalled"].includes(task.status);
};

const taskIsRunning = computed(
  () => starting.value || !isTerminalTask(selectedTask.value),
);

const validRunTabs = new Set([
  "overview",
  "execution",
  "candidates",
  "evidence",
  "questions",
  "diagnostics",
]);

const parseRoute = () => {
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts[0] === "runs" && parts.length === 1) {
    routeName.value = "history";
    routeRunId.value = "";
    routeQuestionId.value = "";
    return;
  }
  if (parts[0] === "runs" && parts[1]) {
    routeName.value = "detail";
    routeRunId.value = decodeURIComponent(parts[1]);
    activeRunTab.value = validRunTabs.has(parts[2])
      ? parts[2]
      : "overview";
    routeQuestionId.value = "";
    return;
  }
  if (parts[0] === "questions" && parts[1]) {
    routeName.value = "question";
    routeQuestionId.value = decodeURIComponent(parts[1]);
    routeRunId.value = "";
    return;
  }
  routeName.value = "workbench";
  routeRunId.value = "";
  routeQuestionId.value = "";
};

const runDetailUrl = (runId, tab = "overview") =>
  `/runs/${encodeURIComponent(runId)}${tab === "overview" ? "" : `/${tab}`}`;

const navigateTo = (path, { replace = false, load = true } = {}) => {
  if (replace) window.history.replaceState({}, "", path);
  else window.history.pushState({}, "", path);
  parseRoute();
  if (load) return loadActiveRoute();
  return Promise.resolve();
};

const clearEvidence = () => {
  evidenceRevision += 1;
  evidenceDossier.value = null;
  evidenceError.value = "";
  evidenceLoading.value = false;
  selectedEvidenceId.value = "";
};

const clearTaskDetails = () => {
  taskDetailsOpen.value = false;
  taskDetailsError.value = "";
  taskDetailsTaskId.value = "";
};

const applyBootstrap = (payload) => {
  entities.value = payload.entities || [];
  selectedEntityId.value = payload.selected_entity_id || "";
  questionStyles.value = payload.question_styles || [];
  entityError.value = payload.entity_error || "";
  model.ready = Boolean(payload.model?.ready);
  model.detail = payload.model?.detail || "";
  model.default_provider = payload.model?.default_provider || "";
  model.providers = payload.model?.providers || [];
};

const loadBootstrap = async (entityId = "", revision = selectionRevision) => {
  const search = new URLSearchParams();
  if (entityId) search.set("entity", entityId);
  const response = await apiFetch(`/api/workbench/bootstrap?${search}`, {
    cache: "no-store",
  });
  const { payload, problem } = await readApiPayload(response);
  if (!response.ok || !payload) {
    throw new Error(
      payload?.error ||
        problem ||
        `无法读取工作台配置（HTTP ${response.status}）`,
    );
  }
  if (revision !== selectionRevision) return null;
  applyBootstrap(payload);
  return payload;
};

const loadObjectiveRuns = async (
  entityId,
  requestedRunId = "",
  revision = selectionRevision,
) => {
  const search = new URLSearchParams();
  if (entityId) search.set("entity_id", entityId);
  const response = await apiFetch(`/api/objective/runs?${search}`, {
    cache: "no-store",
  });
  const { payload, problem } = await readApiPayload(response);
  if (!response.ok || !payload) {
    throw new Error(
      payload?.error ||
        problem ||
        `无法读取 Objective 运行列表（HTTP ${response.status}）`,
    );
  }
  if (revision !== selectionRevision) return null;
  tasks.value = payload.runs || [];
  const requestedRun = requestedRunId
    ? tasks.value.find((item) => item.task_id === requestedRunId)
    : null;
  if (requestedRunId && !requestedRun) {
    throw new Error("指定的 Objective 运行不存在或不属于当前实体");
  }
  selectedTask.value = requestedRun || tasks.value[0] || null;
  questions.value = [];
  objectiveWorkbench.value = null;
  clearEvidence();
  clearTaskDetails();
  return selectedTask.value;
};

const mergeTaskSummary = (summary) => {
  const merged = {
    ...(selectedTask.value || {}),
    ...summary,
    engine: "objective",
    target_question_count:
      summary.target_question_count ??
      selectedTask.value?.target_question_count,
    detail_url: runDetailUrl(summary.task_id),
  };
  selectedTask.value = merged;
  const taskIndex = tasks.value.findIndex(
    (item) => item.task_id === summary.task_id,
  );
  if (taskIndex >= 0) {
    tasks.value = tasks.value.map((item, index) =>
      index === taskIndex ? { ...item, ...summary } : item,
    );
  }
};

const readJson = async (response) => {
  const { payload } = await readApiPayload(response);
  return payload;
};

const formatWait = (seconds = 0) => {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return remainder ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分钟`;
};

const fetchObjectiveStatus = async (taskId, signal) => {
  const summaryResponse = await apiFetch(
    `/api/objective/${encodeURIComponent(taskId)}/summary`,
    { cache: "no-store", signal },
  );
  const summary = await readJson(summaryResponse);
  if (summaryResponse.ok && summary) return summary;
  throw new Error(
    summary?.error ||
      `读取主 Agent 进度失败（HTTP ${summaryResponse.status}）`,
  );
};

const progressMessage = (summary) => {
  if (summary.stalled) {
    return `主 Agent 已经 ${Math.floor(
      (summary.seconds_since_progress || 0) / 60,
    )} 分钟没有新进展，任务可能已经卡住。`;
  }
  if (["pending", "running"].includes(summary.status)) {
    const stepCount =
      summary.main_agent_step_count ??
      summary.main_agent_steps?.length ??
      0;
    if (!stepCount) {
      return `任务已创建，正在等待模型返回；已等待 ${formatWait(
        summary.seconds_since_progress || 0,
      )}。`;
    }
    return `主 Agent 正在执行：${
      summary.current_action?.action_label || "规划下一步"
    }`;
  }
  return summary.status_label;
};

const loadObjectiveWorkbench = async (
  taskId,
  revision = selectionRevision,
) => {
  const response = await apiFetch(
    `/api/objective/${encodeURIComponent(taskId)}/workbench`,
    { cache: "no-store" },
  );
  const { payload, problem } = await readApiPayload(response);
  if (!response.ok || !payload) {
    throw new Error(
      payload?.error ||
        problem ||
        `无法读取任务工作台（HTTP ${response.status}）`,
    );
  }
  if (
    revision !== selectionRevision ||
    selectedTask.value?.task_id !== taskId
  ) {
    return null;
  }
  objectiveWorkbench.value = payload;
  mergeTaskSummary(payload);
  questions.value = payload.published_questions || payload.questions || [];
  runNotice.value =
    !isTerminalTask(payload)
      ? progressMessage(payload)
      : payload.status === "failed"
        ? "本次运行未完成，请查看停止原因。"
        : payload.published_count > 0
      ? `本次发布 ${payload.published_count} 道题；候选与拒绝原因可继续展开。`
          : "本次运行完成，但没有题目通过发布门禁。";
  return payload;
};

const fetchObjectiveTrace = async (taskId) => {
  const response = await apiFetch(
    `/api/objective/${encodeURIComponent(taskId)}/trace`,
    { cache: "no-store" },
  );
  const payload = await readJson(response);
  if (!response.ok || !payload) {
    throw new Error(
      payload?.error ||
        `运行轨迹读取失败或响应为空（HTTP ${response.status}）`,
    );
  }
  return payload;
};

const loadHistory = async () => {
  historyLoading.value = true;
  historyError.value = "";
  try {
    const response = await apiFetch("/api/objective/runs", {
      cache: "no-store",
    });
    const { payload, problem } = await readApiPayload(response);
    if (!response.ok || !payload) {
      throw new Error(
        payload?.error ||
          problem ||
          `无法读取生成记录（HTTP ${response.status}）`,
      );
    }
    tasks.value = payload.runs || [];
  } catch (error) {
    historyError.value = error.message || String(error);
  } finally {
    historyLoading.value = false;
  }
};

const loadRunDetail = async (runId) => {
  detailLoading.value = true;
  detailError.value = "";
  traceLoading.value = true;
  traceError.value = "";
  selectedTask.value = null;
  objectiveWorkbench.value = null;
  questions.value = [];
  window.clearTimeout(progressTimer);
  clearEvidence();
  try {
    const summary = await fetchObjectiveStatus(runId);
    if (routeName.value !== "detail" || routeRunId.value !== runId) return;
    selectedTask.value = {
      ...summary,
      detail_url: runDetailUrl(runId),
    };
    const goal = summary.goal || {};
    const nativeId = String(goal.target_entity_id || "");
    const entityValue =
      selectedEntityId.value ||
      (nativeId && goal.target_entity_type
        ? `primekg:${goal.target_entity_type}:${nativeId}`
        : "");
    if (entityValue) selectedEntityId.value = entityValue;

    const workbench = await loadObjectiveWorkbench(runId);
    questions.value =
      workbench?.published_questions || workbench?.questions || [];
    try {
      const trace = await fetchObjectiveTrace(runId);
      if (routeName.value === "detail" && routeRunId.value === runId) {
        selectedTask.value = {
          ...(selectedTask.value || {}),
          ...trace,
        };
      }
    } catch (error) {
      traceError.value = error.message || String(error);
    }
    if (workbench && !isTerminalTask(workbench)) {
      scheduleObjectivePoll(runId);
    }
  } catch (error) {
    detailError.value = error.message || String(error);
  } finally {
    detailLoading.value = false;
    traceLoading.value = false;
  }
};

const loadQuestionDetail = async (questionId) => {
  questionLoading.value = true;
  questionError.value = "";
  questionDossier.value = null;
  try {
    const response = await apiFetch(
      `/api/questions/${encodeURIComponent(questionId)}/evidence`,
      { cache: "no-store" },
    );
    const { payload, problem } = await readApiPayload(response);
    if (!response.ok || !payload) {
      throw new Error(
        payload?.error ||
          problem ||
          `无法读取题目详情（HTTP ${response.status}）`,
      );
    }
    questionDossier.value = payload;
  } catch (error) {
    questionError.value = error.message || String(error);
  } finally {
    questionLoading.value = false;
  }
};

const loadActiveRoute = async () => {
  if (routeName.value === "history") {
    loading.value = false;
    return loadHistory();
  }
  if (routeName.value === "detail") {
    loading.value = false;
    return loadRunDetail(routeRunId.value);
  }
  if (routeName.value === "question") {
    loading.value = false;
    return loadQuestionDetail(routeQuestionId.value);
  }
  return null;
};

const openRun = (runId) => navigateTo(runDetailUrl(runId));

const selectRunTab = (tab) => {
  if (!routeRunId.value || !validRunTabs.has(tab)) return;
  navigateTo(runDetailUrl(routeRunId.value, tab), { load: false });
};

const openQuestion = (question) => {
  if (!question?.question_id) return;
  navigateTo(`/questions/${encodeURIComponent(question.question_id)}`);
};

const returnToRun = (runId) => navigateTo(runDetailUrl(runId));

const pauseSelectedTask = async () => {
  const taskId = selectedTask.value?.task_id;
  if (!taskId || pauseBusy.value) return;
  pauseBusy.value = true;
  pauseMessage.value = "";
  try {
    const response = await apiFetch(`/api/objective/${encodeURIComponent(taskId)}/pause`, { method: "POST" });
    const payload = await readJson(response);
    if (!response.ok) throw new Error(payload?.error || "暂停失败");
    if (selectedTask.value?.task_id === taskId) {
      selectedTask.value = { ...selectedTask.value, pause_requested: true };
      scheduleObjectivePoll(taskId);
    }
  } catch (error) {
    pauseMessage.value = error.message || String(error);
  } finally {
    pauseBusy.value = false;
  }
};

const resumeSelectedTask = async () => {
  const taskId = selectedTask.value?.task_id || routeRunId.value;
  if (!taskId || resumeBusy.value) return;
  resumeBusy.value = true;
  detailError.value = "";
  const previousTask = selectedTask.value;
  selectedTask.value = { ...previousTask, status: "running", is_terminal: false,
    can_resume: false, pause_requested: false, stop_reason: null };
  completedObjectiveTaskId = "";
  scheduleObjectivePoll(taskId);
  try {
    const response = await apiFetch(
      `/api/objective/${encodeURIComponent(taskId)}/resume`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      },
    );
    const payload = await readJson(response);
    if (!response.ok) {
      throw new Error(
        payload?.error ||
          `从断点继续失败（HTTP ${response.status}）`,
      );
    }
    selectedTask.value = {
      ...(selectedTask.value || {}),
      ...payload,
    };
    await loadRunDetail(taskId);
  } catch (error) {
    detailError.value = error.message || String(error);
    if (selectedTask.value?.task_id === taskId) selectedTask.value = previousTask;
  } finally {
    resumeBusy.value = false;
  }
};

const selectEntity = async (entity) => {
  const revision = ++selectionRevision;
  window.clearTimeout(progressTimer);
  selectedEntityId.value = entity.value;
  selectedTask.value = null;
  questions.value = [];
  objectiveWorkbench.value = null;
  runNotice.value = "";
  clearEvidence();
  clearTaskDetails();
  try {
    await loadBootstrap(entity.value, revision);
    const selected = await loadObjectiveRuns(entity.value, "", revision);
    if (selected) {
      const workbench = await loadObjectiveWorkbench(selected.task_id, revision);
      if (workbench && !isTerminalTask(workbench)) {
        scheduleObjectivePoll(selected.task_id);
      }
    }
  } catch (error) {
    if (revision === selectionRevision) {
      fatalError.value = error.message || String(error);
    }
  }
};

const selectTask = async (taskId) => {
  if (!taskId || taskId === selectedTask.value?.task_id) return;
  const revision = ++selectionRevision;
  window.clearTimeout(progressTimer);
  clearEvidence();
  clearTaskDetails();
  objectiveWorkbench.value = null;
  questions.value = [];
  runNotice.value = "正在加载所选运行…";
  try {
    navigateTo(runDetailUrl(taskId), {
      replace: true,
      load: false,
    });
    await loadRunDetail(taskId);
  } catch (error) {
    if (revision === selectionRevision) {
      runNotice.value = `无法切换任务：${error.message || error}`;
    }
  }
};

const pollObjectiveSummary = async (taskId) => {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 8000);
  try {
    const summary = await fetchObjectiveStatus(taskId, controller.signal);
    if (selectedTask.value?.task_id !== taskId) return null;
    mergeTaskSummary(summary);
    runNotice.value = progressMessage(summary);
    if (isTerminalTask(summary) && completedObjectiveTaskId !== taskId) {
      window.clearTimeout(progressTimer);
      completedObjectiveTaskId = taskId;
      await loadObjectiveWorkbench(taskId);
    }
    return summary;
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

const scheduleObjectivePoll = (taskId) => {
  window.clearTimeout(progressTimer);
  if (
    selectedTask.value?.task_id !== taskId ||
    isTerminalTask(selectedTask.value)
  ) {
    return;
  }
  progressTimer = window.setTimeout(async () => {
    await pollObjectiveSummary(taskId);
    if (
      selectedTask.value?.task_id === taskId &&
      !isTerminalTask(selectedTask.value)
    ) {
      scheduleObjectivePoll(taskId);
    }
  }, 900);
};

const loadObjectiveDetails = async () => {
  const taskId = selectedTask.value?.task_id;
  if (!taskId) return;
  taskDetailsOpen.value = true;
  taskDetailsError.value = "";
  await nextTick();
  document
    .querySelector("[data-run-details]")
    ?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  if (
    taskDetailsTaskId.value === taskId &&
    Array.isArray(selectedTask.value?.main_agent_steps)
  ) {
    return;
  }
  try {
    const response = await apiFetch(
      `/api/objective/${encodeURIComponent(taskId)}/trace`,
      { cache: "no-store" },
    );
    const payload = await readJson(response);
    if (!response.ok || !payload) {
      throw new Error(
        payload?.error ||
          `运行详情读取失败或响应为空（HTTP ${response.status}）`,
      );
    }
    if (selectedTask.value?.task_id !== taskId) return;
    selectedTask.value = {
      ...(selectedTask.value || {}),
      ...payload,
      engine: "objective",
    };
    taskDetailsTaskId.value = taskId;
  } catch (error) {
    if (selectedTask.value?.task_id === taskId) {
      taskDetailsError.value = error.message || String(error);
    }
  }
};

const openEvidence = async (question) => {
  const questionId = question?.question_id;
  if (!questionId) return;
  if (selectedEvidenceId.value === questionId) {
    clearEvidence();
    return;
  }
  const revision = ++evidenceRevision;
  selectedEvidenceId.value = questionId;
  evidenceDossier.value = null;
  evidenceError.value = "";
  evidenceLoading.value = true;
  try {
    const response = await apiFetch(
      `/api/questions/${encodeURIComponent(questionId)}/evidence`,
      { cache: "no-store" },
    );
    const { payload, problem } = await readApiPayload(response);
    if (!response.ok || !payload) {
      throw new Error(
        payload?.error ||
          problem ||
          `无法读取题目证据（HTTP ${response.status}）`,
      );
    }
    if (
      revision !== evidenceRevision ||
      selectedEvidenceId.value !== questionId
    ) {
      return;
    }
    evidenceDossier.value = payload;
  } catch (error) {
    if (revision === evidenceRevision) {
      evidenceError.value = error.message || String(error);
    }
  } finally {
    if (revision === evidenceRevision) evidenceLoading.value = false;
  }
};

const startGeneration = async (configuration) => {
  if (!configuration.entity || taskIsRunning.value) return;
  const revision = ++selectionRevision;
  starting.value = true;
  completedObjectiveTaskId = "";
  runNotice.value = "正在创建 Objective 运行…";
  clearEvidence();
  clearTaskDetails();
  objectiveWorkbench.value = null;
  const taskId = window.crypto?.randomUUID
    ? `web-${window.crypto.randomUUID()}`
    : `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  selectedTask.value = {
    task_id: taskId,
    engine: "objective",
    status: "pending",
    is_terminal: false,
    status_label: "正在创建运行",
    target_question_count: configuration.questionCount,
    published_count: 0,
    question_count: 0,
    operations: [],
    planner_steps: [],
    detail_url: `/?run=${taskId}`,
  };
  questions.value = [];
  window.clearTimeout(progressTimer);
  await nextTick();
  statusSection.value?.$el?.scrollIntoView?.({
    behavior: "smooth",
    block: "start",
  });
  scheduleObjectivePoll(taskId);
  try {
    const response = await apiFetch("/api/objective/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        run_id: taskId,
        entity_canonical_id: configuration.entity.canonical_id,
        entity_name:
          configuration.entity.canonical_name || configuration.entity.name,
        entity_type: configuration.entity.entity_type,
        question_count: configuration.questionCount,
        model_provider_id: configuration.modelProviderId,
        difficulty_targets: configuration.difficulties,
        ...(configuration.styleId
          ? { question_style_ids: [configuration.styleId] }
          : {}),
        max_actions: 100,
        providers: configuration.providers,
      }),
    });
    const payload = await readJson(response);
    if (!response.ok) {
      throw new Error(
        payload?.error || `生成请求失败（HTTP ${response.status}）`,
      );
    }
    if (revision !== selectionRevision || selectedTask.value?.task_id !== taskId) return;
    mergeTaskSummary(payload);
    tasks.value = [
      payload,
      ...tasks.value.filter((item) => item.task_id !== taskId),
    ];
    if (routeName.value !== "workbench") return;
    window.history.replaceState({}, "", runDetailUrl(taskId));
    parseRoute();
    await loadRunDetail(taskId);
  } catch (error) {
    if (revision !== selectionRevision || selectedTask.value?.task_id !== taskId) return;
    runNotice.value = `生成失败：${error.message || error}`;
    selectedTask.value = {
      ...(selectedTask.value || {}),
      status: "failed",
      is_terminal: true,
      status_label: "生成失败",
      stop_reason: error.message || String(error),
    };
  } finally {
    starting.value = false;
  }
};

const handlePopState = () => {
  window.clearTimeout(progressTimer);
  parseRoute();
  loadActiveRoute();
};

onMounted(async () => {
  const revision = ++selectionRevision;
  parseRoute();
  window.addEventListener("popstate", handlePopState);
  try {
    if (routeName.value === "history") {
      await loadHistory();
      return;
    }
    if (routeName.value === "detail") {
      await loadRunDetail(routeRunId.value);
      return;
    }
    if (routeName.value === "question") {
      await loadQuestionDetail(routeQuestionId.value);
      return;
    }

    const search = new URLSearchParams(window.location.search);
    const requestedEntityId = search.get("entity") || "";
    const requestedRunId = search.get("run") || search.get("task") || "";
    if (requestedRunId) {
      navigateTo(runDetailUrl(requestedRunId), {
        replace: true,
        load: false,
      });
      await loadRunDetail(requestedRunId);
      return;
    }
    await loadBootstrap(requestedEntityId, revision);
    const selected = await loadObjectiveRuns(
      selectedEntityId.value,
      "",
      revision,
    );
    if (selected) {
      const workbench = await loadObjectiveWorkbench(selected.task_id, revision);
      if (workbench && !isTerminalTask(workbench)) {
        scheduleObjectivePoll(selected.task_id);
      }
    }
  } catch (error) {
    if (routeName.value === "workbench") {
      fatalError.value = error.message || String(error);
    } else {
      detailError.value = error.message || String(error);
    }
  } finally {
    loading.value = false;
  }
});

onBeforeUnmount(() => {
  window.clearTimeout(progressTimer);
  window.removeEventListener("popstate", handlePopState);
});
</script>

<template>
  <header class="app-topbar">
    <a
      class="app-brand"
      href="/generate"
      @click.prevent="navigateTo('/generate')"
    >
      Medical QA
      <span>Research</span>
    </a>
    <nav aria-label="工作台导航">
      <a
        href="/generate"
        :aria-current="routeName === 'workbench' ? 'page' : undefined"
        @click.prevent="navigateTo('/generate')"
      >
        生成工作台
      </a>
      <a
        href="/runs"
        :aria-current="
          ['history', 'detail'].includes(routeName) ? 'page' : undefined
        "
        @click.prevent="navigateTo('/runs')"
      >
        生成记录
      </a>
    </nav>
    <span class="app-service-label">Vue workspace</span>
  </header>

  <div class="generation-vue" data-vue-generation-app>
    <section v-if="selectedTask" class="run-control-bar" aria-label="任务控制">
      <button v-if="!isTerminalTask(selectedTask)" type="button" data-pause-run
        :disabled="pauseBusy || selectedTask.pause_requested" @click="pauseSelectedTask">
        {{ pauseBusy || selectedTask.pause_requested ? "正在暂停…" : "暂停任务" }}
      </button>
      <span v-if="selectedTask.pause_requested && !isTerminalTask(selectedTask)" role="status">
        当前步骤完成并保存后暂停，不再开始下一步。
      </span>
      <span v-if="selectedTask.stop_reason === 'user_paused'" role="status">任务已暂停</span>
      <button v-if="selectedTask.can_resume && routeName === 'workbench'" type="button"
        :disabled="resumeBusy" @click="resumeSelectedTask">{{ resumeBusy ? "正在继续…" : "继续任务" }}</button>
      <span v-if="pauseMessage" role="alert">{{ pauseMessage }}</span>
    </section>
    <GenerationHistory
      v-if="routeName === 'history'"
      :runs="tasks"
      :loading="historyLoading"
      :error="historyError"
      @open-run="openRun"
      @new-generation="navigateTo('/generate')"
    />

    <RunDetailPage
      v-else-if="routeName === 'detail'"
      :task="selectedTask"
      :workbench="objectiveWorkbench"
      :active-tab="activeRunTab"
      :trace-loading="traceLoading || detailLoading"
      :trace-error="traceError || detailError"
      :resume-busy="resumeBusy"
      @select-tab="selectRunTab"
      @resume="resumeSelectedTask"
      @open-question="openQuestion"
      @back="navigateTo('/runs')"
    />

    <QuestionDetailPage
      v-else-if="routeName === 'question'"
      :dossier="questionDossier"
      :loading="questionLoading"
      :error="questionError"
      @back-run="returnToRun"
    />

    <template v-else>
    <header class="generation-masthead">
      <div>
        <p class="masthead-kicker">QUESTION GENERATION / 01</p>
        <h1>从可靠证据，生成可核验的问题</h1>
        <p class="masthead-copy">
          选择一个医学实体。系统会检索材料、独立验证答案，只呈现通过发布门禁的结果。
        </p>
      </div>
      <div class="masthead-steps" aria-label="生成流程">
        <span :class="{ active: !selectedTask }"><b>1</b> 选择实体</span>
        <span
          :class="{
            active: selectedTask && taskIsRunning,
            completed: selectedTask && !taskIsRunning,
          }"
        >
          <b>2</b> 运行生成
        </span>
        <span :class="{ active: selectedTask && !taskIsRunning }">
          <b>3</b> 审查结果
        </span>
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
      <div class="workbench-command">
        <section
          class="workbench-panel setup-panel"
          data-generation-workspace
          aria-labelledby="setup-title"
        >
          <header class="panel-heading">
            <div>
              <p class="panel-step">01 / 选择与配置</p>
              <h2 id="setup-title">设置本次生成</h2>
            </div>
            <span class="panel-state">
              {{ selectedEntity ? "实体已确认" : "等待选择实体" }}
            </span>
          </header>
          <p v-if="entityError" class="entity-global-error">
            PrimeKG 实体读取失败：{{ entityError }}
          </p>
          <EntityPicker
            :entities="entities"
            :selected-id="selectedEntityId"
            :disabled="false"
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

        <ObjectiveStatus
          ref="statusSection"
          :task="selectedTask"
          :tasks="tasks"
          :workbench="objectiveWorkbench"
          :notice="runNotice"
          :running="taskIsRunning"
          @select-task="selectTask"
          @load-details="loadObjectiveDetails"
        />
      </div>

      <div ref="taskSection" class="task-results-anchor">
        <ObjectiveRun
          v-if="selectedTask"
          :task="selectedTask"
          :workbench="objectiveWorkbench"
          :questions="questions"
          :notice="runNotice"
          :evidence="evidenceDossier"
          :evidence-loading="evidenceLoading"
          :evidence-error="evidenceError"
          :selected-evidence-id="selectedEvidenceId"
          :details-open="taskDetailsOpen"
          :details-error="taskDetailsError"
          @open-evidence="openEvidence"
          @load-details="loadObjectiveDetails"
          @close-details="taskDetailsOpen = false"
        />
      </div>
    </template>
    </template>
  </div>
</template>
