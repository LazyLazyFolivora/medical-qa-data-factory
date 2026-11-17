import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import RunDetailPage from "./RunDetailPage.vue";

const task = {
  task_id: "web-paused",
  status: "failed",
  status_label: "等待从断点继续",
  run_state: "RECOVERY_REQUIRED",
  can_resume: true,
  published_count: 0,
  candidate_count: 13,
  target_question_count: 10,
  last_successful_step: 17,
  next_step: 18,
  stop_reason: "ModelError: model provider returned HTTP 400",
  goal: { target_entity: "PHYHIP" },
};

describe("RunDetailPage", () => {
  it("shows fixed summary, tabs, and the same-run resume action", async () => {
    const wrapper = mount(RunDetailPage, {
      props: {
        task,
        workbench: {
          ...task,
          candidate_questions: [],
          rejected_questions: [],
          indeterminate_questions: [],
          retrieval_sources: [],
          published_questions: [],
        },
        activeTab: "overview",
      },
    });

    const text = wrapper.text().replace(/\s+/g, " ");
    expect(text).toContain("web-paused");
    expect(text).toMatch(/目标上限\s*10/);
    expect(text).toMatch(/候选假设\s*13/);
    expect(text).toContain("最后完成：第 17 步");
    expect(text).toContain("第 18 步");

    await wrapper.get("[data-resume-run]").trigger("click");
    expect(wrapper.emitted("resume")).toHaveLength(1);
  });

  it("navigates between stable detail tabs", async () => {
    const wrapper = mount(RunDetailPage, {
      props: { task, workbench: null, activeTab: "overview" },
    });

    await wrapper.get('[data-run-tab="execution"]').trigger("click");
    expect(wrapper.emitted("select-tab")?.[0]).toEqual(["execution"]);
  });

  it("uses the latest status when the saved workbench is older", async () => {
    const wrapper = mount(RunDetailPage, {
      props: { task, workbench: { ...task }, activeTab: "overview" },
    });
    await wrapper.setProps({ task: { ...task, status: "running", status_label: "运行中", can_resume: false } });

    expect(wrapper.text()).toContain("运行中");
    expect(wrapper.find("[data-resume-run]").exists()).toBe(false);
  });

  it("collapses past interruptions after resume and expands only the current interruption", async () => {
    const previous = { at: "2026-11-04T08:06:13Z", code: "model_bad_request", attempts: 4 };
    const wrapper = mount(RunDetailPage, { props: { activeTab: "diagnostics",
      task: { ...task, run_state: "CANDIDATES_PROCESSING", can_resume: false, stop_reason: null,
        recovery_events: [previous] } } });
    expect(wrapper.get("summary").text()).toContain("历史中断");
    expect(wrapper.get("details").element.open).toBe(false);
    expect(wrapper.get("time").attributes("datetime")).toBe(previous.at);
    await wrapper.setProps({ task: { ...task, recovery_events: [
      previous, { at: "2026-11-04T09:10:00Z", code: "another_interruption" },
    ] } });
    const [current, history] = wrapper.findAll("details");
    expect(current.get("summary").text()).toContain("当前中断");
    expect(current.element.open).toBe(true);
    expect(history.element.open).toBe(false);
  });
});

it("shows the original retrieval failure alongside its query", () => {
  const wrapper = mount(RunDetailPage, {
    props: {
      task,
      activeTab: "evidence",
      workbench: {
        retrieval_sources: [{
          provider_id: "pubmed",
          query_label: "PHYHIP",
          terminal_status: "FAILED",
          error_category: "PUBMED_PROVIDER_ERROR",
          error_message: "Read timed out (read timeout=12)",
        }],
      },
    },
  });
  expect(wrapper.text()).toContain("Read timed out (read timeout=12)");
  expect(wrapper.text()).toContain("PHYHIP");
});
