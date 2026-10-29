<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";

const props = defineProps({
  entities: { type: Array, default: () => [] },
  selectedId: { type: String, default: "" },
  disabled: { type: Boolean, default: false },
});

const emit = defineEmits(["select"]);

const query = ref("");
const searching = ref(false);
const searchError = ref("");
const remoteEntities = ref([]);
let searchTimer = null;
let activeSearch = null;

const selected = computed(
  () => props.entities.find((item) => item.value === props.selectedId) ?? null,
);

const candidates = computed(() => {
  if (query.value.trim() && remoteEntities.value.length) {
    return remoteEntities.value.slice(0, 10);
  }
  return props.entities.slice(0, 10);
});

const entityTypeLabel = (value) => value?.replaceAll("_", "/") || "未知类型";

const search = async () => {
  const value = query.value.trim();
  activeSearch?.abort();
  activeSearch = new AbortController();
  searching.value = true;
  searchError.value = "";
  try {
    const response = await fetch(
      `/api/entities?q=${encodeURIComponent(value)}&limit=10`,
      { signal: activeSearch.signal },
    );
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "实体搜索失败");
    remoteEntities.value = (payload.entities || []).map((entity) => ({
      ...entity,
      value: entity.canonical_id || entity.entity_ref_id,
    }));
    if (value && !remoteEntities.value.length) {
      searchError.value = "没有找到匹配的 PrimeKG 实体";
    }
  } catch (error) {
    if (error.name === "AbortError") return;
    searchError.value = error.message || String(error);
  } finally {
    searching.value = false;
  }
};

watch(query, () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(search, 260);
});

const chooseFromSelect = (event) => {
  const entity = candidates.value.find(
    (item) => item.value === event.target.value,
  );
  if (entity) emit("select", entity);
};

const chooseFirstResult = () => {
  if (candidates.value.length) emit("select", candidates.value[0]);
};

onBeforeUnmount(() => {
  window.clearTimeout(searchTimer);
  activeSearch?.abort();
});
</script>

<template>
  <section class="entity-picker" aria-labelledby="entity-picker-label">
    <div class="entity-picker-copy">
      <p id="entity-picker-label" class="field-kicker">种子实体</p>
      <h2>{{ selected?.name || "选择一个医学实体" }}</h2>
      <p v-if="selected" class="entity-identity">
        {{ entityTypeLabel(selected.entity_type) }}
        <span aria-hidden="true">·</span>
        <code>{{ selected.canonical_id }}</code>
      </p>
      <p v-else class="entity-identity">从 PrimeKG 中搜索名称或 ID。</p>
    </div>

    <div class="entity-selector-controls">
      <label class="search-control">
        <span class="search-icon" aria-hidden="true">⌕</span>
        <input
          v-model="query"
          type="search"
          autocomplete="off"
          placeholder="实时搜索名称或 ID"
          aria-label="搜索 PrimeKG 实体"
          data-entity-search-input
          :disabled="disabled"
          @keydown.enter.prevent="chooseFirstResult"
        />
        <span v-if="searching" class="search-spinner" aria-label="搜索中"></span>
      </label>

      <label class="database-select">
        <span>数据库候选 · 前 {{ candidates.length }} 条</span>
        <select
          :value="selectedId"
          data-entity-select
          :disabled="disabled || !candidates.length"
          @change="chooseFromSelect"
        >
          <option
            v-for="entity in candidates"
            :key="entity.entity_ref_id || entity.value"
            :value="entity.value"
          >
            {{ entity.name }} · {{ entityTypeLabel(entity.entity_type) }} ·
            {{ entity.canonical_id || entity.value }}
          </option>
        </select>
      </label>
      <p class="entity-selector-status">
        {{ searchError || (searching ? "正在更新 PrimeKG 候选…" : "搜索会实时刷新下拉列表") }}
      </p>
    </div>
  </section>
</template>
