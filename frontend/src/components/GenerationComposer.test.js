import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import GenerationComposer from "./GenerationComposer.vue";

describe("GenerationComposer", () => {
  it("submits the selected entity from the primary generation action", async () => {
    const entity = {
      value: "primekg:drug:1191",
      canonical_id: "primekg:drug:1191",
      name: "aspirin",
    };
    const wrapper = mount(GenerationComposer, {
      props: {
        selectedEntity: entity,
        model: { ready: true, detail: "ready", default_mode: "structured" },
        styles: [
          {
            skill_id: "layperson-search-v1",
            display_name: "普通用户搜索",
          },
        ],
        running: false,
      },
    });

    expect(wrapper.get(".primary-action").text()).toContain("开始生成");
    await wrapper.get("form").trigger("submit");

    expect(wrapper.emitted("submit")?.[0]?.[0]).toMatchObject({
      entity,
      questionCount: 3,
      difficulties: ["easy", "medium"],
      styleId: "layperson-search-v1",
    });
  });

  it("uses the evidence-first main-agent loop as the only engine", async () => {
    const wrapper = mount(GenerationComposer, {
      props: {
        selectedEntity: {
          value: "primekg:gene:672",
          canonical_id: "primekg:gene:672",
          name: "BRCA1",
          entity_type: "gene",
        },
        model: { ready: true, detail: "ready", default_mode: "structured" },
        styles: [],
        running: false,
      },
    });

    expect(wrapper.text()).toContain("主 Agent 循环");
    expect(wrapper.text()).not.toContain("规则基线");
    expect(wrapper.find('input[value="structured"]').exists()).toBe(false);
    expect(wrapper.find('input[value="rule-based"]').exists()).toBe(false);
    await wrapper.get("form").trigger("submit");

    expect(wrapper.emitted("submit")?.[0]?.[0]).not.toHaveProperty("modelMode");
  });

  it("cannot start the main-agent loop without a ready model", () => {
    const wrapper = mount(GenerationComposer, {
      props: {
        selectedEntity: {
          value: "primekg:gene:672",
          canonical_id: "primekg:gene:672",
          name: "BRCA1",
        },
        model: { ready: false, detail: "model unavailable" },
        styles: [],
        running: false,
      },
    });

    expect(wrapper.get(".primary-action").attributes("disabled")).toBeDefined();
  });
});
