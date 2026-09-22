import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ResearchApiError } from "../api/client";
import { askResult, fakeApi, semanticResult } from "./fixtures";

function renderAt(path: string, api = fakeApi()) { return { api, ...render(<MemoryRouter initialEntries={[path]}><App api={api} /></MemoryRouter>) }; }

describe("QuantOS workspace", () => {
  it("renders the five-page research shell and navigates by keyboard", async () => {
    renderAt("/");
    expect(await screen.findByRole("heading", { name: /Research readiness/ })).toBeInTheDocument();
    const navigation = screen.getByRole("navigation", { name: "Primary navigation" });
    const analytics = within(navigation).getByRole("link", { name: /Analytics/ });
    analytics.focus(); await userEvent.keyboard("{Enter}");
    expect(await screen.findByRole("heading", { name: /Numbers with provenance/ })).toBeInTheDocument();
    expect(within(navigation).getAllByRole("link")).toHaveLength(5);
  });

  it("preserves READY, PARTIAL, UNAVAILABLE, and DISABLED status semantics", async () => {
    renderAt("/");
    expect((await screen.findAllByLabelText("Status READY")).length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText("Status PARTIAL").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Status UNAVAILABLE")).toBeInTheDocument();
    expect(screen.getByLabelText("Status DISABLED")).toBeInTheDocument();
  });

  it("labels Demo data and exposes auditable runtime identity", async () => {
    const api = fakeApi({ health: vi.fn().mockResolvedValue({
      status: "ok", service: "quantos-research-api", api_version: "v1",
      runtime_mode: "DEMO", data_label: "SYNTHETIC_FIXTURE",
      quantos_version: "0.3.1", build_commit: "demo123",
    }) });
    renderAt("/", api);

    expect(await screen.findByText("DEMO DATA")).toBeInTheDocument();
    expect(screen.getByText(/Synthetic fixture data/)).toBeInTheDocument();
    expect(screen.getByText(/Not investment advice/)).toBeInTheDocument();
    expect(screen.getByText(/0.3.1/)).toBeInTheDocument();
  });

  it("shows an actionable API disconnect state and retries", async () => {
    const health = vi.fn()
      .mockRejectedValueOnce(new TypeError("fetch failed"))
      .mockResolvedValue({ status: "ok", service: "quantos-research-api", api_version: "v1", runtime_mode: "LOCAL", data_label: "LOCAL_PERSISTED_DATA", quantos_version: "0.3.1", build_commit: "fixture" });
    renderAt("/", fakeApi({ health }));

    expect(await screen.findByText("QuantOS Research API is unavailable.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry API" }));
    await waitFor(() => expect(health).toHaveBeenCalledTimes(2));
  });

  it("renders a grounded Ask result and opens its persisted trace", async () => {
    const { api } = renderAt("/ask");
    await userEvent.click(screen.getByRole("button", { name: "Ask QuantOS" }));
    expect(await screen.findByText(askResult.answer)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Facts" })).toBeInTheDocument();
    expect(screen.getByText("Fact ·", { exact: false })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Why this answer?" }));
    expect(await screen.findByRole("dialog", { name: "Why this answer?" })).toHaveTextContent("market.snapshot");
    expect(api.trace).toHaveBeenCalledWith(askResult.trace_id);
  });

  it("treats a partial causal answer as usable with an explicit limitation", async () => {
    const api = fakeApi({ ask: vi.fn().mockResolvedValue({ ...askResult, status: "PARTIAL", limitations: ["Causal explanation unavailable"], reason_codes: ["ATTRIBUTION_INSUFFICIENT"] }) });
    renderAt("/ask", api); await userEvent.click(screen.getByRole("button", { name: "Ask QuantOS" }));
    expect(await screen.findByLabelText("Status PARTIAL")).toBeInTheDocument();
    expect(screen.getByText(/Causal explanation unavailable: current evidence/)).toBeInTheDocument();
  });

  it("shows only the safe API error contract", async () => {
    const api = fakeApi({ ask: vi.fn().mockRejectedValue(new ResearchApiError(500, { code: "INTERNAL_ERROR", message: "Internal server error.", request_id: "corr-1", details: [] })) });
    renderAt("/ask", api); await userEvent.click(screen.getByRole("button", { name: "Ask QuantOS" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("INTERNAL_ERROR"); expect(alert).toHaveTextContent("corr-1"); expect(alert).not.toHaveTextContent("stack");
  });

  it("discovers registries, sends a bounded semantic query, and renders chart plus table", async () => {
    const { api } = renderAt("/analytics");
    expect(await screen.findByRole("option", { name: /收盘价/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run analytics" }));
    expect(await screen.findByRole("img", { name: /line chart/ })).toBeInTheDocument();
    expect(screen.getByRole("table")).toHaveTextContent("1266.98");
    expect(api.analyticsQuery).toHaveBeenCalledWith(expect.objectContaining({ metrics: ["close"], dimensions: ["trading_date"], limit: 20 }));
  });

  it("distinguishes available zero rows from unavailable history", async () => {
    const zero = { ...semanticResult, rows: [], availability: { market_data: { status: "READY" as const, record_count: 0, reason_code: null } }, reason_codes: ["NO_DATA"] };
    renderAt("/analytics", fakeApi({ analyticsQuery: vi.fn().mockResolvedValue(zero) }));
    await screen.findByRole("option", { name: /收盘价/ }); await userEvent.click(screen.getByRole("button", { name: "Run analytics" }));
    expect((await screen.findAllByText("0 matching records")).length).toBeGreaterThan(0);
  });

  it("shows unknown coverage for unavailable analytics instead of zero", async () => {
    const unavailable = { ...semanticResult, status: "PARTIAL" as const, rows: [], availability: { market_data: { status: "UNAVAILABLE" as const, record_count: null, reason_code: "MARKET_DATA_UNAVAILABLE" } }, limitations: ["Persisted market history is unavailable."], reason_codes: ["MARKET_DATA_UNAVAILABLE"] };
    renderAt("/analytics", fakeApi({ analyticsQuery: vi.fn().mockResolvedValue(unavailable) }));
    await screen.findByRole("option", { name: /收盘价/ }); await userEvent.click(screen.getByRole("button", { name: "Run analytics" }));
    expect(await screen.findByText("Coverage unknown")).toBeInTheDocument();
    expect(screen.queryByText("0 matching records")).not.toBeInTheDocument();
  });

  it("opens analytics provenance with registry versions and source references", async () => {
    renderAt("/analytics"); await screen.findByRole("option", { name: /收盘价/ }); await userEvent.click(screen.getByRole("button", { name: "Run analytics" }));
    await userEvent.click(await screen.findByRole("button", { name: "Data provenance" }));
    const drawer = screen.getByRole("dialog", { name: "Data provenance" });
    expect(drawer).toHaveTextContent("market-metrics:v1"); expect(drawer).toHaveTextContent("bar:1");
  });

  it("lists reports and presents structured detail before raw JSON", async () => {
    renderAt("/reports"); const report = await screen.findByRole("button", { name: /2026-09-17/ }); await userEvent.click(report);
    const drawer = await screen.findByRole("dialog", { name: "Report detail" });
    expect(drawer).toHaveTextContent("Candidates"); expect(within(drawer).getByRole("table")).toHaveTextContent("600519.SH"); expect(drawer).toHaveTextContent("Developer / Raw JSON");
  });

  it("applies bounded report filters and pagination through the API client", async () => {
    const api = fakeApi();
    vi.mocked(api.reports).mockImplementation(async (params) => ({
      items: params?.offset === 20 ? [] : [{ report_id: "d".repeat(64), type: "daily", trade_date: "2026-09-17", as_of_time: "2026-09-17T18:00:00+08:00", generated_at: "2026-09-17T19:00:00+08:00", mode: "POST_CLOSE", schema_version: "daily:v1", refs: [] }],
      pagination: { limit: 20, offset: params?.offset ?? 0, total: 25 },
    }));
    renderAt("/reports", api);
    await screen.findByRole("button", { name: /2026-09-17/ });
    await userEvent.type(screen.getByLabelText("Date"), "2026-09-17");
    await userEvent.type(screen.getByLabelText("Security"), "600519.SH");
    await userEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(api.reports).toHaveBeenCalledWith(expect.objectContaining({ date: "2026-09-17", symbol: "600519.SH", offset: 0 })));
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(api.reports).toHaveBeenCalledWith(expect.objectContaining({ offset: 20, limit: 20 })));
  });

  it("renders a report-specific empty state", async () => {
    renderAt("/reports", fakeApi({ reports: vi.fn().mockResolvedValue({ items: [], pagination: { limit: 20, offset: 0, total: 0 } }) }));
    expect(await screen.findByText("No report available")).toBeInTheDocument();
  });

  it("labels retrospective replay and exposes PIT, determinism, and failures", async () => {
    renderAt("/replay");
    expect((await screen.findAllByText(/not an operational-knowledge replay/)).length).toBeGreaterThan(0);
    await userEvent.click(await screen.findByRole("button", { name: /Retrospective Reconstructed/ }));
    const drawer = await screen.findByRole("dialog", { name: "Replay campaign audit" });
    expect(drawer).toHaveTextContent("RETROSPECTIVE_EFFECTIVE_TRUTH"); expect(drawer).toHaveTextContent("deterministic mismatch count"); expect(drawer).toHaveTextContent("EVIDENCE_UNAVAILABLE");
  });

  it("labels strict replay as observed operational knowledge", async () => {
    const strict = { campaign_id: "s".repeat(32), created_at: "2026-09-20T12:00:00+08:00", replay_mode: "STRICT_OPERATIONAL_PIT" as const, identity_temporal_semantics: "OBSERVED_KNOWLEDGE" as const, universe: { start_date: "2026-09-01", end_date: "2026-09-18" }, summary: { pass_points: 20, partial_points: 0, failed_points: 0 } };
    const api = fakeApi({ replayCampaigns: vi.fn().mockResolvedValue({ items: [strict], pagination: { limit: 20, offset: 0, total: 1 } }), replayCampaign: vi.fn().mockResolvedValue(strict), replayFailures: vi.fn().mockResolvedValue({ campaign_id: strict.campaign_id, replay_mode: strict.replay_mode, identity_temporal_semantics: strict.identity_temporal_semantics, failures: [] }) });
    renderAt("/replay", api); await userEvent.click(await screen.findByRole("button", { name: /Strict Operational/ }));
    expect(await screen.findByText("OBSERVED_KNOWLEDGE")).toBeInTheDocument();
    expect(screen.getAllByText(/actually knew at that point/).length).toBeGreaterThan(0);
  });

  it("filters and paginates replay discovery without touching storage", async () => {
    const api = fakeApi();
    const first = await api.replayCampaigns();
    vi.mocked(api.replayCampaigns).mockImplementation(async (params) => ({
      items: params?.offset === 20 ? [] : first.items,
      pagination: { limit: 20, offset: params?.offset ?? 0, total: 24 },
    }));
    renderAt("/replay", api);
    await screen.findByRole("button", { name: /Retrospective Reconstructed/ });
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Replay mode" }), "RETROSPECTIVE_RECONSTRUCTED");
    await waitFor(() => expect(api.replayCampaigns).toHaveBeenCalledWith(expect.objectContaining({ mode: "RETROSPECTIVE_RECONSTRUCTED", offset: 0 })));
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(api.replayCampaigns).toHaveBeenCalledWith(expect.objectContaining({ offset: 20, limit: 20 })));
  });

  it("contains no SQL, provider, credential, or storage control", async () => {
    renderAt("/analytics"); await screen.findByRole("option", { name: /收盘价/ });
    expect(screen.queryByLabelText(/SQL|table|DuckDB|Tushare token|API key/i)).not.toBeInTheDocument();
  });
});
