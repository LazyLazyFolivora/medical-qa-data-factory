import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import ProductionApp from './ProductionApp.vue';
import { apiFetch } from './api';

vi.mock('./api', () => ({
  apiFetch: vi.fn(),
  readApiPayload: async response => ({ payload: response.payload, problem: null }),
}));
let wrapper;
afterEach(() => { wrapper?.unmount(); vi.clearAllMocks(); window.history.replaceState({}, '', '/'); });
const trigger = { id: 'production-test', state: 'running', created_at: '2026-11-05', config: { target_count: 30, difficulty: 'medium' }, published_count: 4, counted_count: 3, entity_counts: { completed: 2 }, entities: [] };
const question = { question_id: 'question-1', question: '测试题目', answer: ['答案'], assessed_difficulty: 'medium', trigger_id: trigger.id };

it('submits production limits and displays published and target counts separately', async () => {
  window.history.replaceState({}, '', '/production');
  let started = false;
  apiFetch.mockImplementation(async (path, options) => {
    if (options?.method === 'POST') { started = true; return { ok: true, payload: { trigger_id: trigger.id } }; }
    return { ok: true, payload: { triggers: started ? [trigger] : [] } };
  });
  wrapper = mount(ProductionApp);
  await flushPromises();
  await wrapper.get('form').trigger('submit');
  await flushPromises();
  const call = apiFetch.mock.calls.find(([, options]) => options?.method === 'POST');
  expect(JSON.parse(call[1].body)).toMatchObject({
    target_count: 30, per_entity_count: 1, workers: 2, difficulty: 'any', generation_strategy: 'free',
  });
  expect(wrapper.text()).toContain('3 / 30');
  expect(wrapper.text()).toContain('已发布题目');
  expect(wrapper.text()).toContain(trigger.id);
});

it('shows the entity history and links to the latest attempt and the dataset', async () => {
  window.history.replaceState({}, '', '/production');
  const entity = { identity: 'primekg:gene/protein:1', name: 'A1BG', run_id: 'web-latest', state: 'completed', attempts: 2, published_count: 3 };
  apiFetch.mockImplementation(async path => ({
    ok: true, payload: path.includes('/entities?') ? { total: 1, page: 1, entities: [entity] } : { triggers: [trigger] },
  }));
  wrapper = mount(ProductionApp);
  await flushPromises();
  const history = wrapper.findAll('details').find(section => section.text().includes('实体处理历史'));
  await history.get('input').setValue('A1BG');
  await history.get('form').trigger('submit');
  await flushPromises();
  expect(apiFetch.mock.calls.some(([url]) => url.includes('search=A1BG'))).toBe(true);
  expect(history.get('a').attributes('href')).toBe('/runs/web-latest');
  expect(history.text()).toContain('2 次 · 累计 3 题');
  expect(wrapper.get('a[href^="/api/production/dataset"]').attributes('href')).toContain(trigger.id);
});

it('requests pause on the selected trigger', async () => {
  window.history.replaceState({}, '', '/production');
  apiFetch.mockResolvedValue({ ok: true, payload: { triggers: [trigger] } });
  wrapper = mount(ProductionApp);
  await flushPromises();
  await wrapper.findAll('button').find(button => button.text() === '暂停').trigger('click');
  await flushPromises();
  expect(apiFetch).toHaveBeenCalledWith(`/api/production/${trigger.id}/pause`, { method: 'POST' });
});

it('switches batch progress, execution rows and export together', async () => {
  window.history.replaceState({}, '', '/production');
  const completed = { ...trigger, id: 'batch-completed', state: 'completed', counted_count: 12, published_count: 12, entities: [
    { run_id: 'run-nat2', name: 'NAT2', state: 'completed', published_count: 1 },
    { run_id: 'run-failed', name: 'Failed entity', state: 'failed', published_count: 0, error: '检索服务暂不可用' },
  ] };
  apiFetch.mockResolvedValue({ ok: true, payload: { triggers: [trigger, completed] } });
  wrapper = mount(ProductionApp);
  await flushPromises();
  await wrapper.get('select[aria-label="当前批次"]').setValue(completed.id);
  expect(wrapper.get('progress').attributes('value')).toBe('12');
  expect(wrapper.get('.production-table').text()).toContain('NAT2');
  expect(wrapper.get('.production-table').text()).toContain('已完成');
  expect(wrapper.get('.production-table').text()).toContain('检索服务暂不可用');
  expect(wrapper.get('a[href="/runs/run-nat2"]').text()).toBe('NAT2');
  expect(wrapper.get('a[href^="/api/production/dataset"]').attributes('href')).toContain(completed.id);
  expect(wrapper.findAll('button').some(button => button.text() === '暂停')).toBe(false);
  expect(wrapper.get('.production-start').attributes('disabled')).toBeDefined();
});

it('shows an empty execution table without invented progress', async () => {
  window.history.replaceState({}, '', '/production');
  apiFetch.mockResolvedValue({ ok: true, payload: { triggers: [] } });
  wrapper = mount(ProductionApp);
  await flushPromises();
  expect(wrapper.find('progress').exists()).toBe(false);
  expect(wrapper.get('.production-table-empty').text()).toContain('启动后');
  expect(wrapper.get('.production-start').attributes('disabled')).toBeUndefined();
});

it('expands a trigger and fetches only its published questions', async () => {
  window.history.replaceState({}, '', '/publication-runs');
  apiFetch.mockImplementation(async path => ({ ok: true, payload: path.includes('/questions') ? { total: 1, questions: [question] } : { triggers: [trigger] } }));
  wrapper = mount(ProductionApp);
  await flushPromises();
  expect(wrapper.text()).not.toContain(question.question);
  await wrapper.get('.production-group-toggle').trigger('click');
  await flushPromises();
  expect(apiFetch.mock.calls.some(([url]) => url.includes(`trigger_id=${trigger.id}`))).toBe(true);
  expect(wrapper.text()).toContain(question.question);
});

it('shows the global question list with difficulty and search filters', async () => {
  window.history.replaceState({}, '', '/published');
  apiFetch.mockImplementation(async path => ({ ok: true, payload: path.includes('/questions') ? { total: 1, questions: [question] } : { triggers: [trigger] } }));
  wrapper = mount(ProductionApp);
  await flushPromises();
  expect(wrapper.text()).toContain(question.question);
  await wrapper.get('input[type="search"]').setValue('测试');
  await wrapper.get('select').setValue('medium');
  await wrapper.get('form').trigger('submit');
  await flushPromises();
  const url = apiFetch.mock.calls.at(-1)[0];
  expect(url).toContain('difficulty=medium');
  expect(url).not.toContain('trigger_id=');
});
