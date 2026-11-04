import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App.vue";

const jsonResponse = (payload, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => payload,
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

describe("App generation feedback", () => {
  it("submits the model provider selected in the workbench", async () => {
    const bootstrap = {
      entities: [
        {
          value: "primekg:gene:672",
          canonical_id: "primekg:gene:672",
          name: "BRCA1",
          entity_type: "gene",
        },
      ],
      selected_entity_id: "primekg:gene:672",
      model: {
        ready: true,
        detail: "local-a / model-a",
        default_provider: "local-a",
        providers: [
          { provider_id: "local-a", model_id: "model-a", ready: true },
          { provider_id: "local-b", model_id: "model-b", ready: true },
        ],
      },
      question_styles: [],
    };
    const completed = {
      task_id: "web-selected-model",
      status: "completed",
      status_label: "已完成",
      is_terminal: true,
      target_question_count: 3,
      published_count: 0,
      published_questions: [],
    };
    let submittedBody;
    global.fetch = vi.fn((url, options = {}) => {
      if (url.startsWith("/api/workbench/bootstrap")) {
        return Promise.resolve(jsonResponse(bootstrap));
      }
      if (url.startsWith("/api/objective/runs")) {
        return Promise.resolve(jsonResponse({ runs: [], run_count: 0 }));
      }
      if (url === "/api/objective/generate") {
        submittedBody = JSON.parse(options.body);
        return Promise.resolve(jsonResponse(completed));
      }
      if (url.endsWith("/workbench")) {
        return Promise.resolve(jsonResponse(completed));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("crypto", { randomUUID: () => "selected-model" });

    const wrapper = mount(App);
    await flushPromises();
    await wrapper.get("[data-model-provider]").setValue("local-b");
    await wrapper.get(".composer").trigger("submit");
    await flushPromises();

    expect(submittedBody.model_provider_id).toBe("local-b");
    wrapper.unmount();
  });

  it("does not poll a run before the create request succeeds", async () => {
    const bootstrap = {
      entities: [
        {
          value: "primekg:gene/protein:672",
          entity_ref_id: "entity-primekg:gene/protein:672",
          canonical_id: "primekg:gene/protein:672",
          name: "BRCA1",
          entity_type: "gene/protein",
        },
      ],
      selected_entity_id: "primekg:gene/protein:672",
      model: { ready: true, detail: "ready" },
      question_styles: [
        {
          skill_id: "layperson-search-v1",
          display_name: "普通用户搜索",
        },
      ],
    };
    global.fetch = vi.fn((url) => {
      if (url.startsWith("/api/workbench/bootstrap")) {
        return Promise.resolve(jsonResponse(bootstrap));
      }
      if (url.startsWith("/api/objective/runs")) {
        return Promise.resolve(jsonResponse({ runs: [], run_count: 0 }));
      }
      if (url === "/api/objective/generate") {
        return new Promise(() => {});
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const wrapper = mount(App);
    await flushPromises();
    await wrapper.get(".composer").trigger("submit");
    await flushPromises();

    expect(wrapper.get("[data-objective-status]").text()).toContain(
      "正在创建运行",
    );
    expect(global.fetch.mock.calls.some(([url]) => url.endsWith("/summary"))).toBe(
      false,
    );

    wrapper.unmount();
  });

  it("clears the previous run result before a newly selected run loads", async () => {
    const bootstrap = {
      entities: [
        {
          value: "primekg:gene:672",
          canonical_id: "primekg:gene:672",
          name: "BRCA1",
          entity_type: "gene",
        },
      ],
      selected_entity_id: "primekg:gene:672",
      model: { ready: true, detail: "ready" },
      question_styles: [],
    };
    const runs = [
      {
        task_id: "run-one",
        status: "completed",
        status_label: "已完成",
        target_question_count: 1,
        published_count: 1,
      },
      {
        task_id: "run-two",
        status: "completed",
        status_label: "已完成",
        target_question_count: 1,
        published_count: 0,
      },
    ];
    let resolveSecondSummary;
    const secondSummary = new Promise((resolve) => {
      resolveSecondSummary = resolve;
    });
    let resolveSecondWorkbench;
    const secondWorkbench = new Promise((resolve) => {
      resolveSecondWorkbench = resolve;
    });
    global.fetch = vi.fn((url) => {
      if (url.startsWith("/api/workbench/bootstrap")) {
        return Promise.resolve(jsonResponse(bootstrap));
      }
      if (url.startsWith("/api/objective/runs")) {
        return Promise.resolve(jsonResponse({ runs, run_count: 2 }));
      }
      if (url === "/api/objective/run-one/workbench") {
        return Promise.resolve(
          jsonResponse({
            ...runs[0],
            published_questions: [
              {
                question_id: "question-one",
                text: "Old run question",
                answer: ["old"],
              },
            ],
          }),
        );
      }
      if (url === "/api/objective/run-two/workbench") {
        return secondWorkbench;
      }
      if (url === "/api/objective/run-two/summary") {
        return secondSummary;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const wrapper = mount(App);
    await flushPromises();
    expect(wrapper.text()).toContain("Old run question");

    await wrapper.get(".history-select select").setValue("run-two");
    await flushPromises();

    expect(wrapper.text()).not.toContain("Old run question");
    expect(wrapper.text()).toContain("正在读取生成详情");

    resolveSecondSummary(jsonResponse(runs[1]));
    await flushPromises();
    resolveSecondWorkbench(
      jsonResponse({ ...runs[1], published_questions: [] }),
    );
    await flushPromises();
    wrapper.unmount();
  });

  it("keeps polling a restored stalled run because it is not terminal", async () => {
    const timeoutSpy = vi.spyOn(window, "setTimeout");
    const bootstrap = {
      entities: [
        {
          value: "primekg:gene:672",
          canonical_id: "primekg:gene:672",
          name: "BRCA1",
          entity_type: "gene",
        },
      ],
      selected_entity_id: "primekg:gene:672",
      model: { ready: true, detail: "ready" },
      question_styles: [],
    };
    const stalled = {
      task_id: "run-stalled",
      status: "stalled",
      status_label: "长时间无进展",
      is_terminal: false,
      stalled: true,
      seconds_since_progress: 700,
      target_question_count: 2,
      published_count: 0,
      published_questions: [],
    };
    global.fetch = vi.fn((url) => {
      if (url.startsWith("/api/workbench/bootstrap")) {
        return Promise.resolve(jsonResponse(bootstrap));
      }
      if (url.startsWith("/api/objective/runs")) {
        return Promise.resolve(jsonResponse({ runs: [stalled], run_count: 1 }));
      }
      if (url === "/api/objective/run-stalled/workbench") {
        return Promise.resolve(jsonResponse(stalled));
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("长时间无进展");
    expect(
      timeoutSpy.mock.calls.some(([, delay]) => delay === 900),
    ).toBe(true);
    wrapper.unmount();
  });

  it("restores a direct run URL before applying entity filtering", async () => {
    window.history.replaceState({}, "", "/?run=run-cftr");
    const bootstrap = {
      entities: [
        {
          value: "primekg:gene:672",
          canonical_id: "primekg:gene:672",
          name: "BRCA1",
          entity_type: "gene",
        },
      ],
      selected_entity_id: "primekg:gene:672",
      model: { ready: true, detail: "ready" },
      question_styles: [],
    };
    const run = {
      task_id: "run-cftr",
      status: "completed",
      status_label: "已完成",
      is_terminal: true,
      target_question_count: 1,
      published_count: 0,
      published_questions: [],
      goal: {
        target_entity: "CFTR",
        target_entity_id: "1080",
        target_entity_type: "gene",
      },
    };
    global.fetch = vi.fn((url) => {
      if (url.startsWith("/api/workbench/bootstrap")) {
        return Promise.resolve(jsonResponse(bootstrap));
      }
      if (url === "/api/objective/runs?") {
        return Promise.resolve(jsonResponse({ runs: [run], run_count: 1 }));
      }
      if (url === "/api/objective/run-cftr/summary") {
        return Promise.resolve(jsonResponse(run));
      }
      if (url === "/api/objective/run-cftr/trace") {
        return Promise.resolve(jsonResponse({
          ...run,
          main_agent_steps: [],
        }));
      }
      if (url.includes("entity_id=primekg%3Agene%3A1080")) {
        return Promise.resolve(jsonResponse({ runs: [run], run_count: 1 }));
      }
      if (url === "/api/objective/run-cftr/workbench") {
        return Promise.resolve(jsonResponse(run));
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const wrapper = mount(App);
    await flushPromises();

    expect(window.location.pathname).toBe("/runs/run-cftr");
    expect(wrapper.get("[data-run-detail]").text()).toContain("run-cftr");
    wrapper.unmount();
  });

  it("opens the generation history route without loading the composer", async () => {
    window.history.replaceState({}, "", "/runs");
    const run = {
      task_id: "web-history",
      status: "completed",
      status_label: "已完成",
      published_count: 2,
      candidate_count: 6,
      goal: { target_entity: "PHYHIP" },
    };
    global.fetch = vi.fn((url) => {
      if (url === "/api/objective/runs") {
        return Promise.resolve(jsonResponse({ runs: [run], run_count: 1 }));
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.find("[data-generation-history]").exists()).toBe(true);
    expect(wrapper.find("[data-generation-workspace]").exists()).toBe(false);
    expect(wrapper.text()).toContain("PHYHIP");
    expect(wrapper.text()).toMatch(/候选假设\s*6/);
    wrapper.unmount();
  });

  it("renders a stable question detail route", async () => {
    window.history.replaceState({}, "", "/questions/draft.contract.1");
    global.fetch = vi.fn((url) => {
      if (url === "/api/questions/draft.contract.1/evidence") {
        return Promise.resolve(
          jsonResponse({
            run_context: { run_id: "web-question" },
            question: {
              question_id: "draft.contract.1",
              text: "Which protein binds PHYHIP?",
              answer: ["BAI1"],
              explanation: "Confirmed experimentally.",
              status_label: "已发布",
            },
            validation: { verdict: "PASS", release_allowed: true },
            evidence: [],
          }),
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.find("[data-question-detail]").exists()).toBe(true);
    expect(wrapper.text()).toContain("Which protein binds PHYHIP?");
    expect(wrapper.text()).toContain("BAI1");
    wrapper.unmount();
  });
});


it("allows pausing a running task without locking entity selection", async () => {
  window.history.replaceState({}, "", "/generate");
  const task = { task_id: "web-pause", status: "running", is_terminal: false,
    status_label: "运行中", published_questions: [], target_question_count: 3 };
  const fetch = vi.fn((url) => {
    if (url.startsWith("/api/workbench/bootstrap")) return Promise.resolve(jsonResponse({
      entities: [{ value: "primekg:gene:672", canonical_id: "primekg:gene:672", name: "BRCA1", entity_type: "gene" }],
      selected_entity_id: "primekg:gene:672", model: { ready: true, providers: [] }, question_styles: [] }));
    if (url.startsWith("/api/objective/runs")) return Promise.resolve(jsonResponse({ runs: [task] }));
    if (url.endsWith("/pause")) return Promise.resolve(jsonResponse({ pause_requested: true }, 202));
    if (url.endsWith("/workbench") || url.endsWith("/summary")) return Promise.resolve(jsonResponse(task));
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetch);
  const wrapper = mount(App);
  await flushPromises();
  expect(wrapper.findComponent({ name: "EntityPicker" }).props("disabled")).toBe(false);
  await wrapper.get("[data-pause-run]").trigger("click");
  await flushPromises();
  expect(fetch).toHaveBeenCalledWith("/api/objective/web-pause/pause", expect.objectContaining({ method: "POST" }));
  expect(wrapper.get("[data-pause-run]").text()).toBe("正在暂停…");
  expect(wrapper.text()).toContain("当前步骤完成并保存后暂停");
  wrapper.unmount();
});
