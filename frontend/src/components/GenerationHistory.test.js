import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import GenerationHistory from "./GenerationHistory.vue";

describe("GenerationHistory", () => {
  it("lists and filters runs without treating candidates as questions", async () => {
    const wrapper = mount(GenerationHistory, {
      props: {
        runs: [
          {
            task_id: "web-completed",
            status: "completed",
            status_label: "已完成",
            published_count: 2,
            candidate_count: 6,
            goal: { target_entity: "PHYHIP" },
          },
          {
            task_id: "web-recovery",
            status: "failed",
            status_label: "等待从断点继续",
            can_resume: true,
            published_count: 0,
            candidate_count: 13,
            goal: { target_entity: "BRCA1" },
          },
        ],
      },
    });

    expect(wrapper.text()).toContain("PHYHIP");
    expect(wrapper.text()).toContain("候选假设 6");
    expect(wrapper.text()).toContain("已发布 2");

    await wrapper.get("[data-history-search]").setValue("BRCA1");
    expect(wrapper.text()).not.toContain("PHYHIP");
    expect(wrapper.text()).toContain("BRCA1");

    await wrapper.get("[data-open-run]").trigger("click");
    expect(wrapper.emitted("open-run")?.[0]).toEqual(["web-recovery"]);
  });
});
