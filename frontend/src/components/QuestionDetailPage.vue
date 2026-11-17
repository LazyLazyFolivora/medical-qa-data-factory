<script setup>
import { computed } from "vue";

const props = defineProps({
  dossier: { type: Object, default: null },
  loading: { type: Boolean, default: false },
  error: { type: String, default: "" },
});

const emit = defineEmits(["back-run"]);

const question = computed(() => props.dossier?.question || null);
const validation = computed(() => props.dossier?.validation || {});
const evidence = computed(() => props.dossier?.evidence || []);
const suggestedRetrieval = computed(() => question.value?.suggested_retrieval);
const answer = computed(() => {
  const value = question.value?.answer;
  return Array.isArray(value) ? value.join("；") : value || "—";
});
</script>

<template>
  <main class="page-shell question-detail-page" data-question-detail>
    <nav class="breadcrumbs" aria-label="面包屑">
      <button
        v-if="dossier?.run_context?.run_id"
        type="button"
        class="link-button"
        @click="emit('back-run', dossier.run_context.run_id)"
      >
        返回本次生成
      </button>
      <span>/</span>
      <span>题目详情</span>
    </nav>

    <p v-if="loading" class="page-state">正在读取题目与证据…</p>
    <p v-else-if="error" class="page-state error">{{ error }}</p>

    <template v-else-if="question">
      <header class="question-detail-heading">
        <div>
          <p class="masthead-kicker">PUBLISHED QUESTION</p>
          <h1>{{ question.text }}</h1>
          <code>{{ question.question_id }}</code>
        </div>
        <span class="status-pill">
          {{ validation.verdict || question.status_label }}
        </span>
      </header>

      <section class="question-answer-block">
        <span>参考答案</span>
        <strong>{{ answer }}</strong>
        <p v-if="question.explanation">{{ question.explanation }}</p>
      </section>

      <section v-if="dossier.difficulty_assessment" class="question-evidence-block">
        <h2>独立难度评估</h2>
        <p>生成策略 · {{ dossier.difficulty_assessment.generation_strategy }} / 实际难度 · {{ dossier.difficulty_assessment.assessment.assessed_difficulty }}</p>
        <p>{{ dossier.difficulty_assessment.assessment.rationale }}</p>
        <ol><li v-for="step in dossier.difficulty_assessment.assessment.shortest_solution_steps" :key="step">{{ step }}</li></ol>
      </section>

      <section class="detail-section">
        <h2>来源与运行上下文</h2>
        <dl class="run-config">
          <div>
            <dt>运行</dt>
            <dd><code>{{ dossier.run_context?.run_id }}</code></dd>
          </div>
          <div>
            <dt>答案审计</dt>
            <dd>{{ validation.answer_audit_id || "—" }}</dd>
          </div>
          <div>
            <dt>题面审计</dt>
            <dd>{{ validation.public_question_audit_id || "—" }}</dd>
          </div>
          <div>
            <dt>发布允许</dt>
            <dd>{{ validation.release_allowed ? "是" : "否" }}</dd>
          </div>
        </dl>
      </section>

      <section v-if="suggestedRetrieval" class="detail-section">
        <h2>随题生成的检索建议</h2>
        <dl class="run-config">
          <div><dt>查询</dt><dd>{{ suggestedRetrieval.query }}</dd></div>
          <div><dt>来源</dt><dd>{{ suggestedRetrieval.sources.join("、") }}</dd></div>
          <div><dt>需要核对的证据</dt><dd>{{ suggestedRetrieval.expected_evidence }}</dd></div>
          <div><dt>停止条件</dt><dd>{{ suggestedRetrieval.stop_condition }}</dd></div>
        </dl>
      </section>

      <section class="detail-section">
        <h2>支持证据</h2>
        <blockquote
          v-for="item in evidence"
          :key="item.anchor_id"
          class="evidence-quote"
        >
          <p>{{ item.quoted_text }}</p>
          <footer>
            <strong>{{ item.provider_label || item.provider_id }}</strong>
            <span>{{ item.source_record_id }}</span>
            <span>{{ item.section_or_field }}</span>
          </footer>
        </blockquote>
        <p v-if="!evidence.length" class="page-state">没有可展示的证据段落。</p>
      </section>
    </template>
  </main>
</template>
