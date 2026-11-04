import { describe, expect, it } from "vitest";
import { readApiPayload } from "./api";

describe("readApiPayload", () => {
  it("returns a readable error for an empty response", async () => {
    const result = await readApiPayload({
      status: 502,
      text: async () => "",
    });

    expect(result.payload).toBeNull();
    expect(result.problem).toContain("空响应");
    expect(result.problem).toContain("502");
  });

  it("returns a readable error for an HTML proxy response", async () => {
    const result = await readApiPayload({
      status: 504,
      text: async () => "<html>Gateway timeout</html>",
    });

    expect(result.payload).toBeNull();
    expect(result.problem).toContain("非 JSON");
    expect(result.problem).toContain("504");
  });

  it("parses a normal JSON response", async () => {
    const result = await readApiPayload({
      status: 200,
      text: async () => '{"status":"ok"}',
    });

    expect(result.problem).toBeNull();
    expect(result.payload).toEqual({ status: "ok" });
  });
});
