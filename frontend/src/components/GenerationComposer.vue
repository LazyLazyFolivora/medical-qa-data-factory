<script setup>
import { computed, reactive, watch } from "vue";

const props = defineProps({
  selectedEntity: { type: Object, default: null },
  model: { type: Object, required: true },
  styles: { type: Array, default: () => [] },
  running: { type: Boolean, default: false },
});

const emit = defineEmits(["submit"]);

const form = reactive({
  questionCount: 3,
  difficulties: ["easy", "medium"],
  styleId: "",
});

watch(
  () => props.styles,
  (styles) => {
    if (!form.styleId && styles.length) form.styleId = styles[0].skill_id;
  },
  { immediate: true },
);

const difficultyLabel = computed(() => {
  const labels = { easy: "简单", medium: "适中", hard: "困难" };
  return form.difficulties.map((item) => labels[item] || item).join("、");
});

const styleLabel = computed(
  () =>
    props.styles.find((item) => item.skill_id === form.styleId)?.display_name ||
    "默认风格",
);

const toggleDifficulty = (value) => {
  const existing = form.difficulties.indexOf(value);
  if (existing >= 0) {
    if (form.difficulties.length === 1) return;
    form.difficulties.splice(existing, 1);
  } else {
    form.difficulties.push(value);
  }
};

const submit = () => {
  if (!props.selectedEntity || props.running || !props.model.ready) return;
  emit("submit", {
    entity: props.selectedEntity,
    questionCount: Number(form.questionCount),
    difficulties: [...form.difficulties],
    styleId: form.styleId,
  });
};
</script>

<template>
  <form class="composer" @submit.prevent="submit">
    <header class="section-heading">
      <div>
        <span class="step-index">01</span>
        <div>
          <p class="field-kicker">生成配置</p>
          <h2>定义这一组问题的范围</h2>
        </div>
      </div>
      <span class="composer-summary">
        {{ styleLabel }} · 最多 {{ form.questionCount }} 题 · {{ difficultyLabel }}
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
        <small>这是上限，不会为了凑数放宽校验</small>
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
            :class="{ active: form.difficulties.includes(item[0]) }"
            @click="toggleDifficulty(item[0])"
          >
            {{ item[1] }}
          </button>
        </div>
      </fieldset>

      <label class="config-field">
        <span>提问风格</span>
        <select v-model="form.styleId" :disabled="running">
          <option
            v-for="style in styles"
            :key="style.skill_id"
            :value="style.skill_id"
          >
            {{ style.display_name }}
          </option>
        </select>
      </label>

      <div class="config-field engine-field">
        <span>生成引擎</span>
        <div class="engine-switch">
          <div class="engine-option selected">
            <strong>主 Agent 循环</strong>
            <small>
              {{ model.ready ? "自主检索、验证与发布复核" : model.detail }}
            </small>
          </div>
        </div>
      </div>
    </div>

    <footer class="composer-actions">
      <p>
        <span class="ready-dot"></span>
        {{ selectedEntity ? "实体已验证" : "请先选择实体" }}
      </p>
      <button
        class="primary-action"
        type="submit"
        :disabled="running || !selectedEntity || !model.ready"
      >
        <span>{{ running ? "正在生成" : "开始生成" }}</span>
        <span aria-hidden="true">{{ running ? "···" : "→" }}</span>
      </button>
    </footer>
  </form>
</template>
