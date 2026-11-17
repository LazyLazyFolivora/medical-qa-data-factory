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
const active = computed(() => triggers.value.find(t => t.id === selected.value) || triggers.value.find(t => ['running', 'pausing', 'paused', 'failed'].includes(t.state)) || triggers.value[0]);
const states = { running: '运行中', pausing: '正在保存断点', paused: '已暂停', completed: '已结束', failed: '需要处理', pending: '待处理' };
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
  <header class="app-topbar">
    <a class="app-brand" href="/production">Medical QA <span>Dataset production</span></a>
    <nav aria-label="数据生产导航">
      <a href="/production" :aria-current="view === 'production' ? 'page' : undefined">自动出题</a>
      <a href="/publication-runs" :aria-current="view === 'groups' ? 'page' : undefined">按触发查看</a>
      <a href="/published" :aria-current="view === 'all' ? 'page' : undefined">全部题目</a>
      <a href="/generate">单实体工作台</a>
    </nav>
  </header>
  <main class="production-page">
    <p class="production-eyebrow">QUERY PLANNER DATASET</p>
    <h1>{{ view === 'production' ? '持续自动出题' : view === 'groups' ? '按触发记录查看题目' : '全部已发布题目' }}</h1>
    <p v-if="view === 'production'" class="production-copy">系统从 PrimeKG 自动选实体，并行自由出题。每个实体单独记账，合格题随产出入库。</p>
    <p v-if="error" role="alert" class="production-error">{{ error }}</p>
    <p v-if="loading" role="status">正在读取生产记录…</p>
    <template v-if="view === 'production'">
      <div class="production-grid">
        <section class="production-panel">
          <h2>启动自动出题</h2>
          <form @submit.prevent="start">
            <label>总目标题数 <input v-model.number="config.target_count" type="number" min="1" max="10000" required /></label>
            <label>并发实体数 <input v-model.number="config.workers" type="number" min="1" max="4" required /></label>
            <details>
              <summary>出题设置</summary>
              <label>每轮单实体题数上限 <input v-model.number="config.per_entity_count" type="number" min="1" max="20" required /></label>
              <label>计入目标的难度 <select v-model="config.difficulty"><option value="any">不限</option><option value="easy">简单</option><option value="medium">中等</option><option value="hard">困难</option></select></label>
              <label>出题策略 <select v-model="config.generation_strategy"><option value="free">自由出题</option><option value="auto">跟随目标难度</option><option value="easy">简单策略</option><option value="medium">中等策略</option><option value="hard">困难策略</option></select></label>
              <label>实体范围 <select v-model="config.entity_type"><option value="gene/protein">基因 / 蛋白</option><option value="disease">疾病</option><option value="drug">药物</option><option value="effect/phenotype">表型</option></select></label>
              <label>本次最多处理实体数 <input v-model.number="config.max_entities" type="number" min="1" max="10000" required /></label>
              <label>每实体最大动作数 <input v-model.number="config.max_actions" type="number" min="2" max="100" required /></label>
              <label class="production-checkbox"><input v-model="config.revisit_processed" type="checkbox" /> 优先再次处理历史实体</label>
            </details>
            <button type="submit" :disabled="busy || triggers.some(t => ['running', 'pausing'].includes(t.state))">{{ busy ? '处理中…' : '启动自动出题' }}</button>
          </form>
        </section>
        <section class="production-panel" aria-live="polite">
          <h2>当前生产进度</h2>
          <template v-if="active">
            <label>触发记录 <select v-model="selected"><option v-for="item in triggers" :key="item.id" :value="item.id">{{ item.created_at }} · {{ item.id }}</option></select></label>
            <p><span class="badge">{{ states[active.state] }}</span> <code>{{ active.id }}</code></p>
            <div class="production-metrics"><div><strong>{{ active.published_count }}</strong><span>已发布题目</span></div><div><strong>{{ active.counted_count }} / {{ active.config.target_count }}</strong><span>计入目标</span></div><div><strong>{{ active.entity_counts.running || 0 }}</strong><span>运行中实体</span></div><div><strong>{{ active.entity_counts.completed || 0 }}</strong><span>已完成实体</span></div><div v-if="active.entity_counts.failed"><strong>{{ active.entity_counts.failed }}</strong><span>出错实体</span></div></div>
            <progress :value="active.counted_count" :max="active.config.target_count" aria-label="目标题数进度"></progress>
            <p v-if="active.reason">{{ reasons[active.reason] || active.reason }}</p>
            <button v-if="active.state === 'running'" :disabled="busy" @click="control('pause')">暂停</button>
            <button v-if="['paused', 'failed'].includes(active.state)" :disabled="busy" @click="control('resume')">从断点继续</button>
            <p v-if="active.state === 'pausing'">停止领取新实体，正在保存活动任务。</p>
            <h3>最近处理的实体</h3>
            <p v-if="!active.entities.length && active.state === 'running'">正在准备实体目录并寻找尚未处理的实体…首次构建目录可能需要一些时间。</p>
            <ul class="production-entities"><li v-for="entity in active.entities" :key="entity.run_id"><a :href="`/runs/${entity.run_id}`">{{ entity.name }}</a><span>{{ states[entity.state] || entity.state }} · {{ entity.published_count ?? 0 }} 题<template v-if="entity.state === 'running' && entity.progress?.current_action?.action_label"> · {{ entity.progress.current_action.action_label }}</template></span><p v-if="entity.error" class="production-error">{{ entity.error }}</p></li></ul>
            <a v-if="active.published_count" :href="`/api/production/dataset?trigger_id=${encodeURIComponent(active.id)}`">下载本次数据集（JSONL）</a>
          </template>
          <p v-else>启动后会自动领取实体，此处实时显示出题进度。</p>
        </section>
      </div>
      <details class="production-panel" @toggle="historyOpen = $event.target.open; if (historyOpen) loadHistory(1)">
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
</template>

<style>
.production-page { max-width:1200px; margin:40px auto; padding:0 24px 64px; color:#253b38; }
.production-page h1 { font-size:32px; letter-spacing:-.8px; margin:10px 0; }
.production-eyebrow { font-size:12px; letter-spacing:2px; color:#637a72; }
.production-copy { color:#687a74; line-height:1.7; }
.production-grid { display:grid; grid-template-columns:350px minmax(0,1fr); gap:24px; margin-top:28px; }
.production-panel { padding:24px; border:1px solid #d9e1db; background:#fffefa; border-radius:12px; margin-bottom:16px; }
.production-panel h2 { margin-top:0; font-size:21px; }
.production-page label { display:flex; flex-direction:column; gap:7px; margin-bottom:16px; font-size:14px; }
.production-page input,.production-page select { width:100%; min-height:42px; padding:8px 10px; border:1px solid #bdcdc4; border-radius:6px; background:white; color:inherit; box-sizing:border-box; }
.production-page button { cursor:pointer; padding:10px 16px; border:1px solid #365e50; border-radius:6px; color:#fff; background:#365e50; }
.production-page button:disabled { cursor:default; opacity:.5; }
.production-page details summary { cursor:pointer; margin-bottom:12px; }
.production-page .production-checkbox { flex-direction:row; align-items:center; }
.production-page .production-checkbox input { width:auto; min-height:0; }
.production-page :focus-visible { outline:3px solid #bf873e; outline-offset:3px; }
.production-metrics { display:flex; flex-wrap:wrap; gap:28px; margin:28px 0; }
.production-metrics strong { display:block; font-size:30px; }.production-metrics span { font-size:13px; color:#687a74; }
.production-page progress { width:100%; accent-color:#365e50; }.production-page code { font-size:12px; overflow-wrap:anywhere; }
.production-entities { padding:0; list-style:none; }.production-entities li { border-bottom:1px solid #e2e7e0; padding:12px 0; }.production-entities span { float:right; font-size:13px; }
.production-error { color:#9f352a; background:#fff0eb; padding:12px; overflow-wrap:anywhere; }
.production-group-toggle { display:flex; justify-content:space-between; gap:16px; width:100%; text-align:left; overflow-wrap:anywhere; }
.production-question { border-top:1px solid #dde3da; padding-top:20px; margin-top:20px; }.production-question h3,.production-question h2 { font-size:19px; line-height:1.6; }.production-question p { line-height:1.7; }
.production-pagination,.production-filters { display:flex; gap:16px; align-items:center; flex-wrap:wrap; }.production-filters label { flex:1; min-width:160px; }.production-filters button { margin-top:8px; }
@media(max-width:760px) { .production-grid { grid-template-columns:1fr; }.production-page { padding:0 14px 40px; }.production-entities span { float:none; display:block; }.production-group-toggle { flex-direction:column; } }
</style>
