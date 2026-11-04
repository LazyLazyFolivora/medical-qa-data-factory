import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import ObjectiveStatus from "./ObjectiveStatus.vue";

describe("ObjectiveStatus", () => {
  it("turns the active run into a compact evidence production summary", () => {
    const wrapper = mount(ObjectiveStatus, {
      props: {
        task: {
          task_id: "objective-web-1",
          status: "completed",
          status_label: "已完成",
          target_question_count: 2,
          published_count: 1,
        },
        tasks: [
          {
            task_id: "objective-web-1",
            status_label: "已完成",
            question_count: 1,
          },
        ],
        workbench: {
          task_id: "objective-web-1",
          published_count: 1,
          artifact_counts: { evidence_material: 6 },
          current_action: {
            action: "retrieve_materials",
            action_label: "收集材料",
            parameters: {
              query: { term: "ZRSR2 splicing" },
            },
          },
        },
        notice: "本次发布 1 道题。",
      },
    });

    expect(wrapper.get(".run-number").text()).toContain("1");
    expect(wrapper.get(".run-number").text()).toContain("/ 2");
    expect(wrapper.text()).toContain("6 份");
    expect(wrapper.text()).toContain("1 题通过");
    expect(wrapper.text()).toContain("收集材料");
    expect(wrapper.text()).toContain("term=ZRSR2 splicing");
    expect(wrapper.get(".run-note").text()).toContain("本次发布 1 道题");
  });

  it("never borrows counts from another run and completes zero-question progress", () => {
    const wrapper = mount(ObjectiveStatus, {
      props: {
        task: {
          task_id: "objective-web-zero",
          status: "completed",
          status_label: "完成，0 题通过",
          is_terminal: true,
          target_question_count: 3,
          published_count: 0,
          current_action: { action_label: "最新动作" },
        },
        tasks: [
          {
            task_id: "objective-web-other",
            status_label: "已完成",
            published_count: 2,
          },
        ],
        workbench: {
          task_id: "objective-web-zero",
          published_count: 2,
          current_action: { action_label: "旧动作" },
        },
      },
    });

    expect(wrapper.get(".run-number").text()).toContain("0");
    expect(wrapper.get("[role='progressbar']").attributes("aria-valuenow")).toBe(
      "100",
    );
    expect(wrapper.find(".history-select").exists()).toBe(false);
    expect(wrapper.text()).toContain("最新动作");
    expect(wrapper.text()).not.toContain("旧动作");
  });

  it("emits task switching and detail loading without hiding the run state", async () => {
    const wrapper = mount(ObjectiveStatus, {
      props: {
        task: {
          task_id: "objective-web-2",
          status: "running",
          status_label: "正在验证",
          target_question_count: 3,
          published_count: 0,
        },
        tasks: [
          {
            task_id: "objective-web-1",
            status_label: "已完成",
            question_count: 3,
          },
          {
            task_id: "objective-web-2",
            status_label: "正在验证",
            question_count: 0,
          },
        ],
        running: false,
      },
    });

    await wrapper.get("select").setValue("objective-web-1");
    await wrapper.get(".status-actions button").trigger("click");

    expect(wrapper.emitted("select-task")?.[0]).toEqual([
      "objective-web-1",
    ]);
    expect(wrapper.emitted("load-details")).toHaveLength(1);
    expect(wrapper.text()).toContain("正在验证");
  });
});
