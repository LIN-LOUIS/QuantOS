import { describe, expect, it } from "vitest";
import { DEFAULT_QUANTOS_API_TARGET, resolveQuantosApiTarget } from "./proxy";

describe("Vite Research API proxy", () => {
  it("keeps the product default on loopback port 8000", () => {
    expect(resolveQuantosApiTarget({})).toBe(DEFAULT_QUANTOS_API_TARGET);
    expect(DEFAULT_QUANTOS_API_TARGET).toBe("http://127.0.0.1:8000");
  });

  it("allows an explicit loopback port override for local development", () => {
    expect(resolveQuantosApiTarget({ QUANTOS_API_TARGET: "http://127.0.0.1:8010" }))
      .toBe("http://127.0.0.1:8010");
  });

  it("rejects non-loopback, credential-bearing, and path-bearing targets", () => {
    for (const value of ["http://0.0.0.0:8010", "https://api.example.com", "http://user:secret@127.0.0.1:8010", "http://127.0.0.1:8010/private"]) {
      expect(() => resolveQuantosApiTarget({ QUANTOS_API_TARGET: value })).toThrow(
        "QUANTOS_API_TARGET must be a loopback HTTP origin",
      );
    }
  });
});
