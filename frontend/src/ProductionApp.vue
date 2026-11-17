<script setup>
import { computed, onMounted, onBeforeUnmount, reactive, ref } from 'vue';
import { apiFetch, readApiPayload } from './api';

const view = window.location.pathname === '/published' ? 'all' : window.location.pathname === '/publication-runs' ? 'groups' : 'production';
const config = reactive({ target_count: 30, per_entity_count: 1, difficulty: 'any', generation_strategy: 'free', workers: 2, entity_type: 'gene/protein', max_entities: 100, max_actions: 50, revisit_processed: false });
const triggers = ref([]);
const error = ref('');
const busy = ref(false);
const loading = ref(true);
const selected = ref('');
const query = reactive({ difficulty: '', search: '', page: 1 });
const results = ref({ total: 0, questions: [] });
const history = reactive({ search: '', page: 1, total: 0, entities: [] });
const historyOpen = ref(false);
const historyError = ref('');
const isProducing = computed(() => triggers.value.some(t => ['running', 'pausing'].includes(t.state)));
function batchDate(value) {
  if (!value) return '未知时间';
  const date = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
}
const active = computed(() => triggers.value.find(t => t.id === selected.value) || triggers.value.find(t => ['running', 'pausing', 'paused', 'failed'].includes(t.state)) || triggers.value[0]);
const states = { running: '运行中', pausing: '正在保存断点', paused: '已暂停', completed: '已结束', stopped: '批次已达标', failed: '需要处理', pending: '待处理' };
const difficulties = { any: '不限', easy: '简单', medium: '中等', hard: '困难', mixed: '混合难度' };
const reasons = { target_reached: '已达到目标题数', entity_budget_exhausted: '已达到本次实体处理上限', source_exhausted: '当前范围的实体已遍历完毕', paused: '已暂停', interrupted: '正在从服务中断前的断点恢复' };
let timer;
let disposed = false;
let questionRequest = 0;
let historyRequest = 0;

async function request(path, options) {
  const response = await apiFetch(path, options);
  const { payload, problem } = await readApiPayload(response);
  if (!response.ok || problem) throw new Error(payload?.error || problem || `请求失败 (${response.status})`);
  return payload;
}
async function refresh() {
  try {
    const data = await request('/api/production');
    if (!disposed) {
      triggers.value = data.triggers;
      if (!selected.value && view === 'production' && active.value) selected.value = active.value.id;
      if (historyOpen.value) loadHistory();
    }
  } catch (e) { if (!disposed) error.value = e.message; }
  finally {
    loading.value = false;
    if (!disposed) timer = setTimeout(refresh, 3000);
  }
}
async function loadQuestions() {
  const version = ++questionRequest;
  try {
    const params = new URLSearchParams({ page: String(query.page), difficulty: query.difficulty, search: query.search });
    if (view === 'groups' && selected.value) params.set('trigger_id', selected.value);
    const data = await request(`/api/production/questions?${params}`);
    if (!disposed && version === questionRequest) results.value = data;
  } catch (e) { error.value = e.message; }
}
async function start() {
  busy.value = true; error.value = '';
  try {
    const data = await request('/api/production', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(config) });
    selected.value = data.trigger_id;
    triggers.value = (await request('/api/production')).triggers;
  } catch (e) { error.value = e.message; }
  finally { busy.value = false; }
}
async function control(action) {
  busy.value = true; error.value = '';
  try {
    await request(`/api/production/${active.value.id}/${action}`, { method: 'POST' });
    triggers.value = (await request('/api/production')).triggers;
  } catch (e) { error.value = e.message; }
  finally { busy.value = false; }
}
async function toggle(id) {
  selected.value = selected.value === id ? '' : id;
  results.value = { total: 0, questions: [] }; query.page = 1;
  if (selected.value) await loadQuestions();
}
async function loadHistory(page = history.page) {
  const version = ++historyRequest;
  try {
    const params = new URLSearchParams({ search: history.search, page: String(page) });
    const data = await request(`/api/production/entities?${params}`);
    if (!disposed && version === historyRequest) {
      Object.assign(history, data);
      historyError.value = '';
    }
  } catch (e) {
    if (!disposed && version === historyRequest) historyError.value = e.message;
  }
}
async function filter() { query.page = 1; await loadQuestions(); }
onMounted(() => { refresh(); if (view === 'all') loadQuestions(); });
onBeforeUnmount(() => { disposed = true; clearTimeout(timer); });
</script>

<template>
  <div class="production-shell">
  <header class="production-topbar">
    <a class="production-brand" href="/production">Medical QA</a>
    <nav aria-label="数据生产导航">
      <a href="/production" :aria-current="view === 'production' ? 'page' : undefined">自动出题</a>
      <a href="/publication-runs" :aria-current="view === 'groups' ? 'page' : undefined">按批次查看</a>
      <a href="/published" :aria-current="view === 'all' ? 'page' : undefined">全部题目</a>
    </nav>
    <a class="production-workbench-link" href="/generate">单实体工作台 <span aria-hidden="true">›</span></a>
  </header>
  <main class="production-page">
    <div class="production-heading">
      <div><p class="production-eyebrow">{{ view === 'production' ? '自动出题' : '题目资料库' }}</p>
      <h1>{{ view === 'production' ? '持续自动出题' : view === 'groups' ? '按批次查看题目' : '全部已发布题目' }}</h1></div>
      <select v-if="view === 'production' && active" v-model="selected" class="production-batch-select" aria-label="当前批次" :title="active.id">
        <option v-for="item in triggers" :key="item.id" :value="item.id">{{ batchDate(item.created_at) }} · {{ item.id }}</option>
      </select>
    </div>
    <p v-if="error" role="alert" class="production-error">{{ error }}</p>
    <p v-if="loading" role="status">正在读取生产记录…</p>
    <template v-if="view === 'production'">
      <section class="production-overview" aria-label="当前生产进度">
        <template v-if="active">
          <div class="production-overview-toolbar">
            <p class="production-state"><span class="production-status-icon" :data-state="active.state" aria-hidden="true">{{ active.state === 'running' ? '↻' : '○' }}</span>{{ states[active.state] || active.state }}</p>
            <div class="production-actions">
              <button v-if="active.state === 'running'" :disabled="busy" @click="control('pause')">暂停</button>
              <button v-if="['paused', 'failed'].includes(active.state)" :disabled="busy" @click="control('resume')">从断点继续</button>
              <a v-if="active.published_count" :href="`/api/production/dataset?trigger_id=${encodeURIComponent(active.id)}`"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5" /></svg>下载 JSONL</a>
            </div>
          </div>
          <div class="production-overview-data">
            <div class="production-target"><div class="production-target-number">{{ active.counted_count }} <span>/ {{ active.config.target_count }}</span></div><span>计入目标</span><progress :value="active.counted_count" :max="active.config.target_count" aria-label="目标题数进度"></progress></div>
            <div class="production-metrics"><div><span class="production-metric-value">{{ active.published_count }}</span><span>已发布题目</span></div><div><span class="production-metric-value">{{ active.entity_counts.running || 0 }}</span><span>运行中实体</span></div><div><span class="production-metric-value">{{ active.entity_counts.failed || 0 }}</span><span>出错实体</span></div></div>
          </div>
          <p v-if="active.reason" class="production-note" role="status">{{ reasons[active.reason] || active.reason }}</p>
          <p v-if="active.state === 'pausing'" class="production-note" role="status">停止领取新实体，正在保存活动任务。</p>
        </template>
        <p v-else-if="!loading" class="production-empty">尚未开始出题。设置目标题数后，启动第一个批次。</p>
      </section>
      <div class="production-workspace">
      <div class="production-grid">
        <section class="production-composer">
          <h2>启动自动出题</h2>
          <form @submit.prevent="start">
            <div class="production-setting-group">
            <label>目标题数 <input v-model.number="config.target_count" type="number" min="1" max="10000" required /></label>
            <label>并发实体数 <input v-model.number="config.workers" type="number" min="1" max="16" required /></label>
            </div>
            <details class="production-advanced">
              <summary>高级设置</summary>
              <label>每轮单实体题数上限 <input v-model.number="config.per_entity_count" type="number" min="1" max="20" required /></label>
              <label>计入目标的难度 <select v-model="config.difficulty"><option value="any">不限</option><option value="easy">简单</option><option value="medium">中等</option><option value="hard">困难</option></select></label>
              <label>出题策略 <select v-model="config.generation_strategy"><option value="free">自由出题</option><option value="auto">跟随目标难度</option><option value="easy">简单策略</option><option value="medium">中等策略</option><option value="hard">困难策略</option></select></label>
              <label>实体范围 <select v-model="config.entity_type"><option value="gene/protein">基因 / 蛋白</option><option value="disease">疾病</option><option value="drug">药物</option><option value="effect/phenotype">表型</option></select></label>
              <label>本次最多处理实体数 <input v-model.number="config.max_entities" type="number" min="1" max="10000" required /></label>
              <label>每实体最大动作数 <input v-model.number="config.max_actions" type="number" min="2" max="100" required /></label>
              <label class="production-checkbox"><input v-model="config.revisit_processed" type="checkbox" /> 优先再次处理历史实体</label>
            </details>
            <button class="production-start" type="submit" :disabled="busy || loading || isProducing">{{ busy ? '处理中…' : '启动自动出题' }}</button>
            <p v-if="isProducing" class="production-note">当前有批次运行中</p>
          </form>
        </section>
        <section class="production-execution" aria-labelledby="execution-title">
          <h2 id="execution-title">实体执行</h2>
          <div class="production-table-wrap" tabindex="0" role="region" aria-label="实体执行列表">
            <table class="production-table">
              <thead><tr><th scope="col">实体</th><th scope="col">状态</th><th scope="col">当前步骤</th><th scope="col" class="production-numeric">已出题</th><th scope="col"><span class="production-sr-only">详情</span></th></tr></thead>
              <tbody>
                <tr v-for="entity in active?.entities || []" :key="entity.run_id">
                  <td><a class="production-entity-link" :href="`/runs/${entity.run_id}`">{{ entity.name }}</a><p v-if="entity.error" class="production-entity-error">{{ entity.error }}</p></td>
                  <td><span class="production-entity-state" :data-state="entity.state"><span aria-hidden="true">{{ entity.state === 'completed' ? '✓' : entity.state === 'failed' ? '!' : '○' }}</span>{{ entity.state === 'completed' ? '已完成' : states[entity.state] || entity.state }}</span></td>
                  <td>{{ entity.progress?.current_action?.action_label || (entity.state === 'completed' ? (entity.published_count ? '已发布' : '已结束') : '—') }}</td>
                  <td class="production-numeric">{{ entity.published_count ?? 0 }}</td>
                  <td><a class="production-row-link" :href="`/runs/${entity.run_id}`" :aria-label="`查看 ${entity.name} 执行详情`">›</a></td>
                </tr>
                <tr v-if="!active?.entities?.length"><td colspan="5" class="production-table-empty">{{ loading ? '正在读取实体…' : active?.state === 'running' ? '正在准备实体目录并寻找尚未处理的实体…首次构建目录可能需要一些时间。' : '启动后，实体执行进度将在此显示。' }}</td></tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>
      <details class="production-history" @toggle="historyOpen = $event.target.open; if (historyOpen) loadHistory(1)">
        <summary>实体处理历史</summary>
        <form class="production-filters" @submit.prevent="loadHistory(1)">
          <label>按实体名称或 ID 搜索 <input v-model="history.search" type="search" /></label>
          <button type="submit">查询</button>
        </form>
        <p v-if="historyError" role="alert">{{ historyError }}</p>
        <p v-else-if="!history.total">暂无匹配的实体记录。</p>
        <ul class="production-entities">
          <li v-for="entity in history.entities" :key="entity.identity">
            <a :href="`/runs/${entity.run_id}`">{{ entity.name }}</a> <small>{{ entity.identity }}</small>
            <span>{{ states[entity.state] || entity.state }} · {{ entity.attempts }} 次 · 累计 {{ entity.published_count }} 题</span>
          </li>
        </ul>
        <div v-if="history.total" class="production-pagination"><button :disabled="history.page <= 1" @click="loadHistory(history.page - 1)">上一页</button><span>第 {{ history.page }} 页 · {{ history.total }} 个实体</span><button :disabled="history.page * 20 >= history.total" @click="loadHistory(history.page + 1)">下一页</button></div>
      </details>
      </div>
    </template>
    <template v-else>
      <template v-if="view === 'groups'">
        <p v-if="!loading && !triggers.length">暂无触发记录。</p>
        <section v-for="item in triggers" :key="item.id" class="production-panel production-group">
          <button class="production-group-toggle" :aria-expanded="selected === item.id" @click="toggle(item.id)"><span>{{ selected === item.id ? '▾' : '▸' }} {{ item.id }}</span><span>{{ states[item.state] }} · 已发布 {{ item.published_count }} 题</span></button>
          <p>{{ item.created_at }} UTC · {{ difficulties[item.config.difficulty] }} · 目标 {{ item.config.target_count }} 题</p>
          <div v-if="selected === item.id">
            <article v-for="q in results.questions" :key="q.question_id" class="production-question"><span class="badge">{{ difficulties[q.assessed_difficulty] }}</span><h3>{{ q.question }}</h3><p><strong>答案</strong> {{ Array.isArray(q.answer) ? q.answer.join('、') : q.answer }}</p><p>{{ q.explanation }}</p><a :href="`/questions/${q.question_id}`">查看证据与详情</a></article>
            <p v-if="!results.questions.length">本次触发尚无已发布题目。</p>
            <div class="production-pagination"><button :disabled="query.page <= 1" @click="query.page--; loadQuestions()">上一页</button><span>第 {{ query.page }} 页 · {{ results.total }} 题</span><button :disabled="query.page * 20 >= results.total" @click="query.page++; loadQuestions()">下一页</button></div>
          </div>
        </section>
      </template>
      <template v-else>
        <form class="production-filters" @submit.prevent="filter"><label>搜索题干 <input v-model="query.search" type="search" /></label><label>难度 <select v-model="query.difficulty"><option value="">全部</option><option value="easy">简单</option><option value="medium">中等</option><option value="hard">困难</option></select></label><button>查询 / 刷新</button></form>
        <p>共 {{ results.total }} 道符合筛选条件的题目 <a href="/api/production/dataset">下载完整数据集（JSONL）</a></p>
        <article v-for="q in results.questions" :key="q.question_id" class="production-panel production-question"><span class="badge">{{ difficulties[q.assessed_difficulty] }}</span><h2>{{ q.question }}</h2><p><strong>答案</strong> {{ Array.isArray(q.answer) ? q.answer.join('、') : q.answer }}</p><p>{{ q.explanation }}</p><small>触发 ID · {{ q.trigger_id }}</small><p><a :href="`/questions/${q.question_id}`">查看证据与详情</a></p></article>
        <p v-if="!results.questions.length">暂无符合条件的已发布题目。</p>
        <div class="production-pagination"><button :disabled="query.page <= 1" @click="query.page--; loadQuestions()">上一页</button><span>第 {{ query.page }} 页</span><button :disabled="query.page * 20 >= results.total" @click="query.page++; loadQuestions()">下一页</button></div>
      </template>
    </template>
  </main>
  </div>
</template>

<style src="./production.css"></style>
