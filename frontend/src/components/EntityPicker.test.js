import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";
import EntityPicker from "./EntityPicker.vue";

const entities = Array.from({ length: 12 }, (_, index) => ({
  value: `primekg:drug:${index + 1}`,
  entity_ref_id: `entity-primekg:drug:${index + 1}`,
  canonical_id: `primekg:drug:${index + 1}`,
  name: `entity-${index + 1}`,
  entity_type: "drug",
}));

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("EntityPicker", () => {
  it("always exposes at most ten database entities in a native select", async () => {
    const wrapper = mount(EntityPicker, {
      props: {
        entities,
        selectedId: entities[0].value,
      },
    });

    const select = wrapper.get("[data-entity-select]");
    expect(select.findAll("option")).toHaveLength(10);
    expect(select.element.value).toBe(entities[0].value);
  });

  it("searches PrimeKG in real time and replaces the select options", async () => {
    vi.useFakeTimers();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        entities: [
          {
            entity_ref_id: "entity-primekg:drug:1191",
            canonical_id: "primekg:drug:1191",
            name: "aspirin",
            entity_type: "drug",
          },
        ],
      }),
    });
    const wrapper = mount(EntityPicker, {
      props: {
        entities,
        selectedId: entities[0].value,
      },
    });

    await wrapper.get("[data-entity-search-input]").setValue("aspirin");
    await vi.advanceTimersByTimeAsync(300);
    await flushPromises();

    expect(global.fetch).toHaveBeenCalledWith(
      "/api/entities?q=aspirin&limit=10",
      expect.any(Object),
    );
    const options = wrapper.get("[data-entity-select]").findAll("option");
    expect(options).toHaveLength(1);
    expect(options[0].text()).toContain("aspirin");
  });

  it("emits the selected database entity", async () => {
    const wrapper = mount(EntityPicker, {
      props: {
        entities,
        selectedId: entities[0].value,
      },
    });

    await wrapper
      .get("[data-entity-select]")
      .setValue(entities[3].value);

    expect(wrapper.emitted("select")?.[0]?.[0]).toEqual(entities[3]);
  });
});
