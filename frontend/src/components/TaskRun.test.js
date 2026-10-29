import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import TaskRun from "./TaskRun.vue";

describe("TaskRun", () => {
  it("shows compiler details instead of a generic candidate failure", () => {
    const wrapper = mount(TaskRun, {
      props: {
        task: {
          task_id: "web-rgs20",
          status: "partial",
          status_label: "部分完成",
          target_question_count: 3,
          detail_url: "/tasks/web-rgs20",
          operations: [],
          planner_steps: [
            {
              turn: 3,
              phase: 2,
              node: "candidate_validation",
              action_type: "candidate_validation",
              action_label: "候选终结拒绝",
              status_label: "terminal_rejection",
              policy_result: "rejected",
              error_code: "candidate_not_compilable",
              error_message:
                "TaskCompilationError: single-answer candidate must propose exactly one entity",
            },
          ],
        },
        questions: [],
      },
    });

    expect(wrapper.get(".run-alert h3").text()).toContain("候选终结拒绝");
    expect(wrapper.text()).toContain(
      "single-answer candidate must propose exactly one entity",
    );
  });
});
