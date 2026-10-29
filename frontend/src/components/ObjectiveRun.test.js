import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import ObjectiveRun from "./ObjectiveRun.vue";

describe("ObjectiveRun", () => {
  it("shows the main-agent decisions and validated public questions", () => {
    const wrapper = mount(ObjectiveRun, {
      props: {
        task: {
          task_id: "objective-web-1",
          engine: "objective",
          status: "completed",
          status_label: "已完成",
          run_state: "COMPLETED",
          target_question_count: 1,
          published_count: 1,
          artifact_counts: { candidate_claim: 3, evidence_material: 6 },
          main_agent_steps: [
            {
              sequence: 1,
              action: "retrieve_materials",
              action_label: "收集材料",
              target_subagent_role: "material_retrieval",
              state_label: "材料已收集",
              information_gain_label: "90%",
            },
            {
              sequence: 2,
              action: "review_release",
              action_label: "发布复核",
              target_subagent_role: "objective_release_review",
              state_label: "候选处理中",
              information_gain_label: "70%",
            },
          ],
        },
        questions: [
          {
            question_id: "draft.brca1",
            text: "On which human chromosome is BRCA1 located?",
            answer: ["chromosome 17"],
            explanation: "BRCA1 maps to chromosome 17.",
          },
        ],
      },
    });

    expect(wrapper.text()).toContain("主 Agent");
    expect(wrapper.text()).toContain("收集材料");
    expect(wrapper.text()).toContain("发布复核");
    expect(wrapper.text()).toContain("chromosome 17");
    expect(wrapper.findAll("[data-agent-step]")).toHaveLength(2);
  });

  it("explains a valid zero-question result", () => {
    const wrapper = mount(ObjectiveRun, {
      props: {
        task: {
          task_id: "objective-web-zero",
          engine: "objective",
          status: "completed",
          status_label: "已完成",
          run_state: "COMPLETED_WITH_ZERO_QUESTIONS",
          target_question_count: 1,
          published_count: 0,
          stop_reason: "No candidate passed independent verification.",
          artifact_counts: { candidate_claim: 2 },
          main_agent_steps: [],
        },
        questions: [],
      },
    });

    expect(wrapper.text()).toContain("0 题是正常结果");
    expect(wrapper.text()).toContain(
      "No candidate passed independent verification.",
    );
  });
});
