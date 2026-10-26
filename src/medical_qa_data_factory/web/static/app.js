document.addEventListener('DOMContentLoaded', () => {
  const entitySelector = document.querySelector('[data-entity-selector]');
  const entitySearch = document.querySelector('[data-entity-search]');
  const entitySelection = document.querySelector('[data-entity-selection]');
  const entitySearchStatus = document.querySelector('[data-entity-search-status]');
  const generationForm = document.querySelector('[data-generation-form]');
  const startGeneration = document.querySelector('[data-start-generation]');
  const generationNotice = document.querySelector('[data-generation-notice]');
  const liveGeneration = document.querySelector('[data-live-generation]');
  const liveCount = document.querySelector('[data-live-count]');
  const liveStatus = document.querySelector('[data-live-status]');
  const liveProgressBar = document.querySelector('[data-live-progress-bar]');
  const liveCurrentLabel = document.querySelector('[data-live-current-label]');
  const liveCurrentMeta = document.querySelector('[data-live-current-meta]');
  const liveOperationCount = document.querySelector('[data-live-operation-count]');
  const livePlannerCount = document.querySelector('[data-live-planner-count]');
  const liveQuestionCount = document.querySelector('[data-live-question-count]');
  const liveOperations = document.querySelector('[data-live-operations]');
  const liveAgentSection = document.querySelector('[data-live-agent-section]');
  const livePlannerSteps = document.querySelector('[data-live-planner-steps]');

  const updateEntitySelection = () => {
    const option = entitySelector?.selectedOptions?.[0];
    if (!option || !entitySelection) return;
    const canonicalId = option.dataset.canonicalId || option.value;
    entitySelection.replaceChildren();
    const check = document.createElement('i');
    check.setAttribute('aria-hidden', 'true');
    check.textContent = '✓';
    entitySelection.append(check, ` 已验证 · ${canonicalId}`);
  };

  const populateEntityOptions = (entities) => {
    if (!entitySelector) return;
    entitySelector.replaceChildren();
    entities.forEach((entity) => {
      const option = document.createElement('option');
      option.value = entity.canonical_id || entity.entity_ref_id;
      option.dataset.canonicalId = option.value;
      option.dataset.entityName = entity.name;
      option.dataset.entityType = entity.entity_type || 'unknown';
      option.textContent = `${entity.name} · ${entity.entity_type || 'unknown'} · ${option.value}`;
      entitySelector.append(option);
    });
    if (entities.length) entitySelector.selectedIndex = 0;
    updateEntitySelection();
  };

  entitySelector?.addEventListener('change', updateEntitySelection);

  let searchTimer = null;
  let activeSearch = null;
  entitySearch?.addEventListener('input', () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(async () => {
      const query = entitySearch.value.trim();
      activeSearch?.abort();
      activeSearch = new AbortController();
      if (entitySearchStatus) {
        entitySearchStatus.textContent = query ? '正在搜索 PrimeKG 全库…' : '正在恢复默认候选…';
      }
      startGeneration.disabled = true;
      try {
        const response = await fetch(`/api/entities?q=${encodeURIComponent(query)}&limit=10`, {
          signal: activeSearch.signal,
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || '实体搜索失败');
        populateEntityOptions(payload.entities || []);
        startGeneration.disabled = !payload.entities?.length;
        if (entitySearchStatus) {
          if (!(payload.entities || []).length) {
            entitySearchStatus.textContent = '没有找到匹配的实体';
          } else {
            entitySearchStatus.textContent = query
              ? `找到 ${payload.entities.length} 个候选${payload.truncated ? '（仅显示前 10 个）' : ''}`
              : '已恢复默认候选';
          }
        }
      } catch (error) {
        if (error.name === 'AbortError') return;
        if (entitySearchStatus) entitySearchStatus.textContent = error.message || String(error);
        startGeneration.disabled = false;
      }
    }, 300);
  });

  const traceRow = ({
    marker,
    status,
    label,
    statusLabel,
    description,
    metrics,
    node,
  }) => {
    const row = document.createElement('li');
    row.className = `trace-step ${status}`;
    const markerNode = document.createElement('span');
    markerNode.className = 'trace-marker';
    markerNode.textContent = marker;
    const copy = document.createElement('div');
    copy.className = 'trace-copy';
    const title = document.createElement('div');
    title.className = 'trace-title';
    const strong = document.createElement('strong');
    strong.textContent = label;
    const titleStatus = document.createElement('span');
    titleStatus.textContent = statusLabel;
    title.append(strong, titleStatus);
    const detail = document.createElement('p');
    detail.textContent = description || '';
    const metricRow = document.createElement('div');
    metricRow.className = 'trace-metrics';
    (metrics || []).filter(Boolean).forEach((metric) => {
      const chip = document.createElement('span');
      chip.textContent = metric;
      metricRow.append(chip);
    });
    const code = document.createElement('code');
    code.textContent = node;
    copy.append(title, detail, metricRow, code);
    row.append(markerNode, copy);
    return row;
  };

  const setPersistedTraceVisible = (visible) => {
    const aside = document.querySelector('.generation-activity');
    if (!aside) return;
    aside.querySelectorAll('.trace-summary, .execution-trace, .trace-footer, .trace-empty')
      .forEach((element) => {
        if (element.closest('[data-live-generation]')) return;
        element.style.display = visible ? '' : 'none';
      });
  };

  const renderLiveProgress = (payload, targetCount) => {
    if (!liveGeneration) return;
    const operations = payload.operations || [];
    const plannerSteps = payload.planner_steps || [];
    const target = payload.target_question_count || targetCount || 1;
    const completed = payload.question_count || 0;
    liveGeneration.hidden = false;
    setPersistedTraceVisible(false);
    if (liveCount) liveCount.textContent = `${completed} / ${target}`;
    if (liveStatus) liveStatus.textContent = payload.status_label || payload.status;
    if (liveQuestionCount) liveQuestionCount.textContent = String(completed);
    if (liveOperationCount) liveOperationCount.textContent = String(operations.length);
    if (livePlannerCount) livePlannerCount.textContent = String(plannerSteps.length);
    const current = payload.current_step;
    const latestPlanner = plannerSteps[plannerSteps.length - 1];
    if (liveCurrentLabel) {
      liveCurrentLabel.textContent = current
        ? current.label
        : latestPlanner
          ? `Agent 第 ${latestPlanner.turn} 轮已完成`
          : '正在启动生成任务…';
    }
    if (liveCurrentMeta) {
      const pieces = [];
      if (current?.status_label) pieces.push(current.status_label);
      if (latestPlanner) {
        pieces.push(`最近决策：${latestPlanner.action_label}`);
        if (latestPlanner.evidence_gain) {
          pieces.push(`新增证据 ${latestPlanner.evidence_gain}`);
        }
      }
      liveCurrentMeta.textContent = pieces.join(' · ');
    }
    const terminal = ['completed', 'partial', 'failed'].includes(payload.status);
    const ratio = Math.min(100, Math.round((completed / target) * 100));
    const stageRatio = Math.min(90, Math.max(8, operations.length * 8));
    if (liveProgressBar) {
      liveProgressBar.style.width = `${terminal ? 100 : Math.max(ratio, stageRatio)}%`;
    }
    if (liveOperations) {
      liveOperations.replaceChildren(
        ...operations.map((operation) =>
          traceRow({
            marker: operation.status === 'completed'
              ? '✓'
              : operation.status === 'failed'
                ? '!'
                : String(operation.sequence),
            status: operation.status,
            label: operation.label,
            statusLabel: operation.status_label,
            description: operation.description,
            metrics: [
              operation.tool_call_count ? `${operation.tool_call_count} 次查询` : '',
              operation.records_returned ? `${operation.records_returned} 条返回` : '',
              operation.elapsed_label && operation.elapsed_label !== '—'
                ? operation.elapsed_label
                : '',
            ],
            node: operation.node,
          })
        )
      );
    }
    if (liveAgentSection) liveAgentSection.hidden = plannerSteps.length === 0;
    if (livePlannerSteps) {
      livePlannerSteps.replaceChildren(
        ...plannerSteps.map((step) =>
          traceRow({
            marker: String(step.turn),
            status: step.policy_result === 'accepted' ? 'completed' : 'failed',
            label: `Agent 第 ${step.turn} 轮 · ${step.action_label}`,
            statusLabel: step.status === 'finish' ? '结束探索' : step.policy_result,
            description: step.expected_information,
            metrics: [
              step.evidence_gain ? `新增证据 ${step.evidence_gain}` : '',
              step.rejection_code || '',
            ],
            node: step.subject_entity_id || '—',
          })
        )
      );
    }
  };

  generationForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!generationNotice || !startGeneration || !entitySelector) return;

    startGeneration.disabled = true;
    startGeneration.textContent = '正在生成…';
    generationNotice.textContent = '正在探索 PrimeKG 并生成问题，请保持页面打开。';
    generationNotice.dataset.state = 'working';
    generationNotice.hidden = false;
    const runId = window.crypto?.randomUUID
      ? `web-${window.crypto.randomUUID()}`
      : `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const targetCount = Number(document.querySelector('[data-question-count]')?.value || 3);
    let progressTimer = null;
    const pollProgress = async () => {
      try {
        const progressResponse = await fetch(`/api/generation/${encodeURIComponent(runId)}`, {
          cache: 'no-store',
        });
        const progress = await progressResponse.json();
        if (!progressResponse.ok) throw new Error(progress.error || '无法读取生成进度');
        renderLiveProgress(progress, targetCount);
        return progress;
      } catch (error) {
        if (liveCurrentMeta) liveCurrentMeta.textContent = '进度读取暂时失败，生成仍在继续…';
        return null;
      }
    };
    renderLiveProgress(
      {
        status: 'pending',
        status_label: '准备生成',
        current_step: null,
        operations: [],
        planner_steps: [],
        question_count: 0,
        target_question_count: targetCount,
      },
      targetCount
    );
    await pollProgress();
    progressTimer = window.setInterval(pollProgress, 750);

    try {
      const difficulty = document.querySelector('[data-difficulty]')?.value || 'easy,medium';
      const styleChoice = document.querySelector('[data-question-style]')?.value || 'layperson-search-v1';
      const questionStyleIds = styleChoice === 'mixed'
        ? ['layperson-search-v1', 'patient-caregiver-v1', 'medical-learner-v1']
        : [styleChoice];
      const response = await fetch('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          run_id: runId,
          entity_canonical_id: entitySelector.value,
          model_mode: document.querySelector('[data-model-mode]')?.value || 'rule-based',
          question_style_ids: questionStyleIds,
          question_count: targetCount,
          difficulty_targets: difficulty.split(',').filter(Boolean),
          language: document.querySelector('[data-language]')?.value || 'zh-CN',
        }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || '生成失败');
      }
      window.clearInterval(progressTimer);
      await pollProgress();
      generationNotice.textContent = `生成完成：${payload.question_ids.length} 个候选问题，正在刷新…`;
      generationNotice.dataset.state = 'success';
      window.location.reload();
    } catch (error) {
      window.clearInterval(progressTimer);
      generationNotice.textContent = `生成失败：${error.message || error}`;
      generationNotice.dataset.state = 'error';
      startGeneration.disabled = false;
      startGeneration.textContent = '重新生成';
      if (liveCurrentLabel) liveCurrentLabel.textContent = '生成失败';
      if (liveCurrentMeta) liveCurrentMeta.textContent = error.message || String(error);
    }
  });

  // ---- grouped review -------------------------------------------------
  const groupSections = [...document.querySelectorAll('.group[data-group-key]')];
  groupSections.forEach((group) => {
    const key = group.dataset.groupKey;
    group.querySelectorAll('button[data-action]').forEach((button) => {
      button.addEventListener('click', async () => {
        const action = button.dataset.action;
        const questionId = button.dataset.questionId;
        let targetId = null;
        if (action === 'set_variant_of') {
          const select = group.querySelector(`select[data-variant-for="${questionId}"]`);
          targetId = select ? select.value : null;
        }
        button.disabled = true;
        try {
          const response = await fetch(`/api/groups/${encodeURIComponent(key)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              action,
              question_id: questionId,
              target_question_id: targetId,
            }),
          });
          const payload = await response.json();
          if (!response.ok) {
            window.alert(payload.error || '处置失败');
            button.disabled = false;
            return;
          }
          window.location.reload();
        } catch (error) {
          window.alert(`处置失败: ${error}`);
          button.disabled = false;
        }
      });
    });
  });

  const searchInputs = [...document.querySelectorAll('[data-filter-table]')];
  const statusTabs = [...document.querySelectorAll('[data-status-filter]')];
  const statusSelects = [...document.querySelectorAll('[data-table-status-target]')];
  let activeStatus = 'all';

  const applyTableFilters = () => {
    searchInputs.forEach((search) => {
      const query = search.value.trim().toLowerCase();
      const table = document.getElementById(search.dataset.filterTable);
      const statusSelect = statusSelects.find(
        (select) => select.dataset.tableStatusTarget === table?.id
      );
      const selectedStatus = statusSelect?.value
        || (table?.id === 'task-table' ? activeStatus : 'all');
      table?.querySelectorAll('tbody tr[data-search]').forEach((row) => {
        const queryMatch = row.dataset.search.toLowerCase().includes(query);
        const status = row.dataset.filterStatus;
        const statusMatch =
          selectedStatus === 'all' ||
          status === selectedStatus ||
          (selectedStatus === 'attention' &&
            ['needs_input', 'partial', 'failed'].includes(status));
        row.hidden = !(queryMatch && statusMatch);
      });
    });
  };

  searchInputs.forEach((search) => {
    search.addEventListener('input', applyTableFilters);
  });

  statusTabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      activeStatus = tab.dataset.statusFilter;
      statusTabs.forEach((item) => item.classList.toggle('active', item === tab));
      applyTableFilters();
    });
  });

  statusSelects.forEach((select) => {
    select.addEventListener('change', applyTableFilters);
  });

  document.addEventListener('keydown', (event) => {
    if (
      event.key === '/' &&
      !event.metaKey &&
      !event.ctrlKey &&
      !event.altKey &&
      !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)
    ) {
      const search = searchInputs[0];
      if (search) {
        event.preventDefault();
        search.focus();
      }
    }
  });

  const claims = [...document.querySelectorAll('.claim-node')];
  const evidence = [...document.querySelectorAll('.evidence-item')];
  claims.forEach((claim) => {
    claim.addEventListener('click', () => {
      const selected = claim.getAttribute('aria-pressed') !== 'true';
      claims.forEach((item) => item.setAttribute('aria-pressed', 'false'));
      evidence.forEach((item) => item.classList.remove('related', 'dimmed'));
      if (!selected) return;
      claim.setAttribute('aria-pressed', 'true');
      const ids = new Set((claim.dataset.evidenceIds || '').split(',').filter(Boolean));
      evidence.forEach((item) => {
        item.classList.add(ids.has(item.dataset.evidenceId) ? 'related' : 'dimmed');
      });
    });
  });

  const questionEditor = document.querySelector('.question-editor');
  if (questionEditor) {
    const questionId = window.location.pathname.split('/').filter(Boolean).pop();
    const summary = document.getElementById('review-summary');
    const errorBox = questionEditor.querySelector('[data-review-error]');
    const buttons = [...questionEditor.querySelectorAll('[data-review-action]')];
    const canonical = document.getElementById('canonical-question');
    const goldShortAnswer = document.getElementById('gold-short-answer');

    const collectChanges = () => {
      const changed = [];
      if (canonical && canonical.value.trim() !== canonical.dataset.original) {
        changed.push('canonical_question');
      }
      if (goldShortAnswer && goldShortAnswer.value.trim() !== goldShortAnswer.dataset.original) {
        changed.push('gold_answer');
      }
      const taxonomyFields = [...questionEditor.querySelectorAll('[data-taxonomy-field]')];
      if (taxonomyFields.some((field) => field.value !== field.dataset.original)) {
        changed.push('taxonomy');
      }
      const variantInputs = [...questionEditor.querySelectorAll('[data-variant-text]')];
      if (variantInputs.some((input) => input.value.trim() !== input.dataset.original)) {
        changed.push('question_variants');
      }
      return changed;
    };

    buttons.forEach((button) => {
      button.addEventListener('click', async () => {
        const note = summary.value.trim();
        if (!note) {
          errorBox.textContent = '请先填写审核说明';
          errorBox.hidden = false;
          summary.focus();
          return;
        }
        buttons.forEach((item) => (item.disabled = true));
        errorBox.hidden = true;
        const split = document.getElementById('dataset-split').value;
        const changedFields = collectChanges();
        const taxonomy = {};
        questionEditor.querySelectorAll('[data-taxonomy-field]').forEach((field) => {
          taxonomy[field.dataset.taxonomyField] = field.value;
        });
        const response = await fetch(`/api/review/questions/${questionId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            action: button.dataset.reviewAction,
            summary: note,
            verified_route: document.getElementById('verified-route').value,
            gold_short_answer: document.getElementById('gold-short-answer').value.trim(),
            dataset_split: split || null,
            canonical_question: canonical ? canonical.value.trim() : null,
            taxonomy: Object.keys(taxonomy).length ? taxonomy : null,
            changed_fields: changedFields,
          }),
        });
        const data = await response.json();
        if (!response.ok) {
          errorBox.textContent = data.error || '审核写入失败';
          errorBox.hidden = false;
          buttons.forEach((item) => (item.disabled = false));
          return;
        }
        window.location.reload();
      });
    });
  }

  const exportPage = document.querySelector('[data-export-mode]');
  if (exportPage) {
    const exportButtons = [...document.querySelectorAll('[data-export-mode]')];
    const exportError = document.querySelector('[data-review-error]');
    exportButtons.forEach((button) => {
      button.addEventListener('click', async () => {
        exportButtons.forEach((item) => (item.disabled = true));
        const response = await fetch('/api/export', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ development: button.dataset.exportMode === 'development' }),
        });
        const data = await response.json();
        if (!response.ok) {
          exportError.textContent = data.error || '导出被阻止';
          exportError.hidden = false;
          exportButtons.forEach((item) => (item.disabled = false));
          return;
        }
        exportError.textContent = '已导出：' + data.path;
        exportError.hidden = false;
        exportButtons.forEach((item) => (item.disabled = false));
      });
    });
  }
});

