import { vi } from "vitest";
import type { ResearchApi } from "../api/client";
import type { AskResponse, SemanticResult } from "../api/types";

export const askResult: AskResponse = {
  request_id: "r".repeat(64), trace_id: "a".repeat(32), status: "PASS",
  answer: "贵州茅台最近一个可见交易日收盘 1266.98 元。", intent: "MARKET_OVERVIEW",
  entities: [{ canonical_id: "600519.SH", resolution_status: "RESOLVED" }],
  as_of_time: "2026-09-18T18:00:00+08:00",
  facts: [{ source_ref: "M1", trade_date: "2026-09-17", close: "1266.98", pct_change: "0.7138" }],
  references: ["M1"], evidence_refs: [], knowledge_refs: [], report_refs: [],
  limitations: [], reason_codes: ["OK"],
};

export const semanticResult: SemanticResult = {
  query_id: "q".repeat(64), plan_hash: "p".repeat(64), trace_id: "t".repeat(32), status: "PASS",
  schema: [{ name: "trading_date", kind: "dimension", value_type: "date" }, { name: "close", kind: "metric", value_type: "decimal", unit: "CNY" }],
  rows: [{ trading_date: "2026-09-16", close: "1258.00" }, { trading_date: "2026-09-17", close: "1266.98" }],
  metrics: ["close"], dimensions: ["trading_date"], as_of_time: "2026-09-18T18:00:00+08:00",
  availability: { market_data: { status: "READY", record_count: 2, reason_code: null } },
  limitations: [], reason_codes: ["OK"],
  provenance: { dataset_refs: ["bar:1", "bar:2"], providers: ["tushare"], metric_definition_version: "market-metrics:v1", dimension_definition_version: "market-dimensions:v1", data_cutoff: "2026-09-18T18:00:00+08:00", source_capabilities: ["market.history"] },
  insight: { title: "QuantOS Market Analytics", summary: "Returned 2 PIT-visible market rows.", metrics: ["close"], comparison: null, references: ["bar:1", "bar:2"], limitations: [] },
  chart: { chart_type: "line", x: "trading_date", y: ["close"], series: null, title: "QuantOS Market Analytics", unit: "CNY" },
};

export function fakeApi(overrides: Partial<ResearchApi> = {}): ResearchApi {
  const campaign = {
    campaign_id: "c".repeat(32), created_at: "2026-09-20T12:00:00+08:00",
    replay_mode: "RETROSPECTIVE_RECONSTRUCTED" as const,
    identity_temporal_semantics: "RETROSPECTIVE_EFFECTIVE_TRUTH" as const,
    universe: { symbols: ["600519.SH"], start_date: "2026-08-20", end_date: "2026-09-18", trading_days: 22 },
    summary: { requested_points: 22, executed_points: 22, pass_points: 0, partial_points: 22, failed_points: 0, pit_rejection_count: 0, deterministic_mismatch_count: 0, market_pit_violation_count: 0, evidence_pit_violation_count: 0, report_pit_violation_count: 0, knowledge_pit_violation_count: 0 },
  };
  return {
    health: vi.fn().mockResolvedValue({ status: "ok", service: "quantos-research-api", api_version: "v1", runtime_mode: "LOCAL", data_label: "LOCAL_PERSISTED_DATA", quantos_version: "0.3.0", build_commit: "fixture" }),
    status: vi.fn().mockResolvedValue({ providers: [{ provider_id: "tushare", availability: "READY" }], data_availability: { security_master: { availability: "READY" }, historical_market: { availability: "PARTIAL" } }, research_availability: { evidence: "UNAVAILABLE", attribution: "PARTIAL", knowledge: "DISABLED", reports: "READY" }, replay_availability: { status: "READY", dataset_count: 1 } }),
    ask: vi.fn().mockResolvedValue(askResult),
    trace: vi.fn().mockResolvedValue({ trace_id: "a".repeat(32), request_id: "r".repeat(64), intent: "MARKET_OVERVIEW", raw_question: "最近表现？", normalized_question: "最近表现？", canonical_as_of_time: "2026-09-18T18:00:00+08:00", query_plan: { as_of_time: "2026-09-18T18:00:00+08:00", steps: ["market.snapshot"] }, tool_invocations: [{ capability: "market.snapshot", status: "PASS" }], reason_codes: ["OK"], limitations: [], timings: { total_ms: 12 } }),
    analyticsSchema: vi.fn().mockResolvedValue({ metrics: [{ metric_id: "close", display_name: "收盘价", description: "日线收盘价", unit: "CNY", value_type: "decimal", supported_dimensions: ["trading_date"], time_semantics: "MARKET_EVENT_TIME_WITH_AVAILABILITY_CUTOFF", source_capability: "market.history", availability: "READY", version: "market-metrics:v1" }], dimensions: [{ dimension_id: "trading_date", display_name: "交易日", description: "market event date", value_type: "date", time_semantics: "MARKET_EVENT_TIME", source_capability: "market.history", availability: "READY", version: "market-dimensions:v1" }], query_limits: { max_metrics: 5, max_dimensions: 4, max_entities: 10, max_filters: 10, max_sorts: 5, max_rows: 1000, max_date_range_days: 366 }, metric_registry_version: "market-metrics:v1", dimension_registry_version: "market-dimensions:v1" }),
    analyticsQuery: vi.fn().mockResolvedValue(semanticResult),
    reports: vi.fn().mockResolvedValue({ items: [{ report_id: "d".repeat(64), type: "daily", trade_date: "2026-09-17", as_of_time: "2026-09-17T18:00:00+08:00", generated_at: "2026-09-17T19:00:00+08:00", mode: "POST_CLOSE", schema_version: "daily:v1", refs: ["R:" + "d".repeat(64)] }], pagination: { limit: 20, offset: 0, total: 1 } }),
    report: vi.fn().mockResolvedValue({ report_id: "d".repeat(64), trade_date: "2026-09-17", as_of_time: "2026-09-17T18:00:00+08:00", generated_at: "2026-09-17T19:00:00+08:00", mode: "POST_CLOSE", schema_version: "daily:v1", market_overview: { close: "1266.98" }, candidate_briefs: [{ symbol: "600519.SH", reason_codes: ["OK"] }] }),
    replayCampaigns: vi.fn().mockResolvedValue({ items: [campaign], pagination: { limit: 20, offset: 0, total: 1 } }),
    replayCampaign: vi.fn().mockResolvedValue({ ...campaign, dataset_refs: ["dataset:real"], results: [] }),
    replayFailures: vi.fn().mockResolvedValue({ campaign_id: campaign.campaign_id, replay_mode: campaign.replay_mode, identity_temporal_semantics: campaign.identity_temporal_semantics, failures: [{ replay_id: "f".repeat(64), date: "2026-08-20", symbol: "600519.SH", stage: "supplemental", reason_codes: ["EVIDENCE_UNAVAILABLE"], available_inputs: ["identity", "market"], missing_inputs: ["evidence"], trace_id: null }] }),
    ...overrides,
  };
}
