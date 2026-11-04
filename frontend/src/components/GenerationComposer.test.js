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
        model: {
          ready: true,
          detail: "ready",
          default_mode: "structured",
          default_provider: "local-a",
          providers: [
            {
              provider_id: "local-a",
              model_id: "model-a",
              ready: true,
            },
            {
              provider_id: "local-b",
              model_id: "model-b",
              ready: true,
            },
          ],
        },
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
    expect(wrapper.get(".composer-summary").text()).toContain("目标 3 题");
    await wrapper.get("form").trigger("submit");

    expect(wrapper.emitted("submit")?.[0]?.[0]).toMatchObject({
      entity,
      questionCount: 3,
      difficulties: ["easy", "medium"],
      styleId: "layperson-search-v1",
      providers: [
        "hgnc",
        "ncbi_gene",
        "ensembl",
        "primekg",
        "pubmed",
      ],
      modelProviderId: "local-a",
    });
  });

  it("can select another configured self-hosted model", async () => {
    const wrapper = mount(GenerationComposer, {
      props: {
        selectedEntity: {
          value: "primekg:gene:672",
          canonical_id: "primekg:gene:672",
          name: "BRCA1",
        },
        model: {
          ready: true,
          detail: "ready",
          default_provider: "local-a",
          providers: [
            { provider_id: "local-a", model_id: "model-a", ready: true },
            { provider_id: "local-b", model_id: "model-b", ready: true },
          ],
        },
        styles: [],
      },
    });

    await wrapper.get("[data-model-provider]").setValue("local-b");
    await wrapper.get("form").trigger("submit");

    expect(wrapper.emitted("submit")?.[0]?.[0].modelProviderId).toBe(
      "local-b",
    );
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
