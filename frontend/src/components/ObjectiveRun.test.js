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
          retrieval_sources: [
            {
              provider_id: "primekg",
              provider_label: "PrimeKG",
              purpose: "discovery",
              terminal_status: "SUCCEEDED",
              query_label: "BRCA1",
              material_count: 1,
            },
            {
              provider_id: "pubmed",
              provider_label: "PubMed",
              purpose: "verification",
              terminal_status: "SUCCEEDED",
              query_label: "BRCA1 chromosome 17",
              material_count: 1,
            },
          ],
          materials: [
            {
              material_id: "material.primekg.1",
              provider_id: "primekg",
              provider_label: "PrimeKG",
              source_record_id: "edge-1",
              material_label: "知识图谱边",
              purpose: "discovery",
              preview: "BRCA1 --located_on--> chromosome 17",
              anchor_count: 1,
            },
            {
              material_id: "material.pubmed.1",
              provider_id: "pubmed",
              provider_label: "PubMed",
              source_record_id: "pmid:123",
              material_label: "文献摘要",
              purpose: "verification",
              preview: "Independent report: BRCA1 maps to chromosome 17.",
              anchor_count: 1,
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
    expect(wrapper.text()).toContain("PrimeKG");
    expect(wrapper.text()).toContain("PubMed");
    expect(wrapper.text()).toContain("BRCA1 --located_on--> chromosome 17");
    expect(wrapper.findAll("[data-collected-material]")).toHaveLength(2);
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
