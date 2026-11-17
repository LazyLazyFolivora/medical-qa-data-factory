<script setup>
import { computed, reactive, watch } from "vue";

const props = defineProps({
  selectedEntity: { type: Object, default: null },
  model: { type: Object, required: true },
  styles: { type: Array, default: () => [] },
  defaults: { type: Object, default: null },
  running: { type: Boolean, default: false },
});

const emit = defineEmits(["submit"]);

const providerOptions = [
  ["pubmed", "PubMed"],
  ["primekg", "PrimeKG"],
  ["hgnc", "HGNC"],
  ["ncbi_gene", "NCBI Gene"],
  ["ensembl", "Ensembl"],
  ["ctd", "CTD"],
];

const form = reactive({
  questionCount: 3,
  difficulties: null,
  styleId: "",
  modelProviderId: "",
  providers: null,
});

const selectedDifficulties = computed(
  () => form.difficulties ?? props.defaults?.difficulty_targets ?? ["easy"],
);
const selectedProviders = computed(
  () => form.providers ?? props.defaults?.providers ??
    ["hgnc", "ncbi_gene", "ensembl", "primekg", "pubmed"],
);

watch(
  () => [props.model.default_provider, props.model.providers],
  () => {
    const options = props.model.providers || [];
    const selected = options.find(
      (item) => item.provider_id === form.modelProviderId && item.ready,
    );
    if (!selected) {
      form.modelProviderId =
        options.find(
          (item) =>
            item.provider_id === props.model.default_provider && item.ready,
        )?.provider_id ||
        options.find((item) => item.ready)?.provider_id ||
        "";
    }
  },
  { immediate: true, deep: true },
);

const selectedModel = computed(() =>
  (props.model.providers || []).find(
    (item) => item.provider_id === form.modelProviderId,
  ),
);
const selectedModelReady = computed(
  () => selectedModel.value?.ready ?? props.model.ready,
);

const difficultyLabel = computed(() => {
  const labels = { easy: "简单", medium: "适中", hard: "困难" };
  return selectedDifficulties.value.map((item) => labels[item] || item).join("、");
});

const styleLabel = computed(
  () =>
    props.styles.find((item) => item.skill_id === form.styleId)?.display_name ||
    "简洁（默认）",
);

const toggleDifficulty = (value) => {
  form.difficulties ??= [...selectedDifficulties.value];
  const existing = form.difficulties.indexOf(value);
  if (existing >= 0) {
    if (form.difficulties.length === 1) return;
    form.difficulties.splice(existing, 1);
  } else {
    form.difficulties.push(value);
  }
};

const toggleProvider = (value) => {
  form.providers ??= [...selectedProviders.value];
  const existing = form.providers.indexOf(value);
  if (existing >= 0) {
    if (form.providers.length === 1) return;
    form.providers.splice(existing, 1);
  } else {
    form.providers.push(value);
  }
};

const submit = () => {
  if (
    !props.selectedEntity ||
    props.running ||
    !selectedModelReady.value ||
    !selectedProviders.value.length
  ) {
    return;
  }
  emit("submit", {
    entity: props.selectedEntity,
    questionCount: Number(form.questionCount),
    difficulties: [...selectedDifficulties.value],
    styleId: form.styleId,
    modelProviderId: form.modelProviderId,
    providers: [...selectedProviders.value],
  });
};
</script>

<template>
  <form class="composer" @submit.prevent="submit">
    <header class="composer-heading">
      <div>
        <p class="field-kicker">生成配置</p>
        <h3>定义这一组问题的范围</h3>
      </div>
      <span class="composer-summary">
        {{ styleLabel }} · 目标 {{ form.questionCount }} 题 · {{ difficultyLabel }}
      </span>
    </header>

    <div class="config-grid">
      <label class="config-field count-field">
        <span>目标题数</span>
        <input
          v-model.number="form.questionCount"
          type="number"
          min="1"
          max="20"
          :disabled="running"
        />
        <small>这是发布目标；不会为了凑数放宽质量门禁</small>
      </label>

      <fieldset class="config-field difficulty-field" :disabled="running">
        <legend>难度</legend>
        <div class="choice-row">
          <button
            v-for="item in [
              ['easy', '简单'],
              ['medium', '适中'],
              ['hard', '困难'],
            ]"
            :key="item[0]"
            type="button"
            class="choice-chip"
            :class="{ active: selectedDifficulties.includes(item[0]) }"
            @click="toggleDifficulty(item[0])"
          >
            {{ item[1] }}
          </button>
        </div>
      </fieldset>

      <label class="config-field style-field">
        <span>提问风格</span>
        <select v-model="form.styleId" :disabled="running">
          <option value="">简洁（默认）</option>
          <option
            v-for="style in styles"
            :key="style.skill_id"
            :value="style.skill_id"
          >
            {{ style.display_name }}
          </option>
        </select>
      </label>

      <label class="config-field model-field">
        <span>主 Agent 模型</span>
        <select
          v-model="form.modelProviderId"
          data-model-provider
          :disabled="running"
        >
          <option
            v-for="provider in model.providers || []"
            :key="provider.provider_id"
            :value="provider.provider_id"
            :disabled="!provider.ready"
          >
            {{ provider.provider_id }} · {{ provider.model_id }}{{
              provider.ready ? "" : "（未就绪）"
            }}
          </option>
        </select>
        <small v-if="selectedModel">{{ selectedModel.detail }}</small>
      </label>

      <fieldset class="config-field provider-field" :disabled="running">
        <legend>材料来源</legend>
        <div class="choice-row">
          <button
            v-for="[value, label] in providerOptions"
            :key="value"
            type="button"
            class="choice-chip"
            :class="{ active: selectedProviders.includes(value) }"
            @click="toggleProvider(value)"
          >
            {{ label }}
          </button>
        </div>
      </fieldset>
    </div>

    <footer class="composer-actions">
      <p>
        <span class="ready-dot"></span>
        {{
          !selectedEntity
            ? "请先选择实体"
            : selectedModelReady
              ? "实体与主 Agent 循环已就绪"
              : selectedModel?.detail || model.detail
        }}
      </p>
      <button
        class="primary-action"
        type="submit"
        :disabled="running || !selectedEntity || !selectedModelReady"
      >
        <span>{{ running ? "正在生成" : "开始生成" }}</span>
        <span aria-hidden="true">{{ running ? "···" : "→" }}</span>
      </button>
    </footer>
  </form>
</template>
