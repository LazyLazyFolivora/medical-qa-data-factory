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
      modelMode: "structured",
    });
  });

  it("allows selecting the evidence-first main-agent loop", async () => {
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

    await wrapper.get('input[value="objective"]').setValue(true);
    await wrapper.get("form").trigger("submit");

    expect(wrapper.emitted("submit")?.[0]?.[0]).toMatchObject({
      modelMode: "objective",
    });
  });
});
