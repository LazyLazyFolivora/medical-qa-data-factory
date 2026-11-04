import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import QuestionDetailPage from "./QuestionDetailPage.vue";

describe("QuestionDetailPage", () => {
  it("shows a published question and its supporting evidence", () => {
    const wrapper = mount(QuestionDetailPage, {
      props: {
        dossier: {
          run_context: { run_id: "web-question" },
          question: {
            question_id: "draft.contract.1",
            text: "Which protein binds PHYHIP?",
            answer: ["BAI1"],
            explanation: "The interaction was experimentally confirmed.",
            status_label: "已发布",
          },
          validation: {
            verdict: "PASS",
            release_allowed: true,
          },
          evidence: [
            {
              role: "supporting",
              anchor_id: "anchor.1",
              quoted_text: "PHYHIP interacts with BAI1.",
              provider_label: "PubMed",
              source_record_id: "11245925",
            },
          ],
        },
      },
    });

    expect(wrapper.text()).toContain("Which protein binds PHYHIP?");
    expect(wrapper.text()).toContain("BAI1");
    expect(wrapper.text()).toContain("PHYHIP interacts with BAI1");
    expect(wrapper.text()).toContain("web-question");
  });
});
