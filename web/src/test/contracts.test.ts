import { describe, expect, it, vi } from "vitest";
import { HttpResearchApi, ResearchApiError } from "../api/client";
import { buildChartOption } from "../charts/ChartRenderer";

describe("transport and chart contracts", () => {
  it("uses only the versioned Research API and safely encodes resource ids", async () => {
    const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ trace_id: "ok" }), { status: 200, headers: { "content-type": "application/json" } }));
    const api = new HttpResearchApi(); await api.trace("../secret");
    expect(fetcher).toHaveBeenCalledWith("/v1/traces/..%2Fsecret", undefined);
    fetcher.mockRestore();
  });

  it("preserves the safe structured error contract", async () => {
    const body = { code: "TRACE_NOT_FOUND", message: "Resource was not found.", request_id: "corr", details: [] };
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(JSON.stringify(body), { status: 404 }));
    await expect(new HttpResearchApi().trace("a".repeat(32))).rejects.toEqual(new ResearchApiError(404, body));
    vi.restoreAllMocks();
  });

  it("renders only the series dictated by ChartSpec", () => {
    const option = buildChartOption({ chart_type: "line", x: "trading_date", y: ["close"], series: null, title: "Close", unit: "CNY" }, [{ trading_date: "2026-09-17", close: "1266.98", unrequested: 999 }]);
    expect(option.xAxis.data).toEqual(["2026-09-17"]); expect(option.series).toHaveLength(1); expect(option.series[0].data).toEqual(["1266.98"]); expect(JSON.stringify(option)).not.toContain("unrequested");
  });
});
