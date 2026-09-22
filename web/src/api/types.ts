export type AvailabilityState =
  | "READY" | "PARTIAL" | "DEGRADED" | "UNAVAILABLE"
  | "UNCONFIGURED" | "DISABLED" | "UNKNOWN";
export type ResultStatus = "PASS" | "PARTIAL" | "FAILED" | "FAIL" | "UNSUPPORTED";
export type ReplayMode = "STRICT_OPERATIONAL_PIT" | "RETROSPECTIVE_RECONSTRUCTED";
export type IdentitySemantics = "OBSERVED_KNOWLEDGE" | "RETROSPECTIVE_EFFECTIVE_TRUTH";

export interface HealthResponse {
  status: "ok"; service: string; api_version: "v1";
  runtime_mode: "LOCAL" | "DEMO";
  data_label: "LOCAL_PERSISTED_DATA" | "SYNTHETIC_FIXTURE";
  quantos_version: string; build_commit: string;
}
export interface ProviderStatus {
  provider_id: string; provider_type?: string; availability: AvailabilityState;
  capabilities?: string[]; credential_configured?: boolean; health_reason?: string | null;
}
export interface StatusResponse {
  providers: ProviderStatus[];
  data_availability: Record<string, unknown>;
  research_availability: Record<string, AvailabilityState>;
  replay_availability: { status: AvailabilityState; reason_code?: string | null; dataset_count?: number };
}
export interface ApiErrorBody { code: string; message: string; request_id: string; details: Array<Record<string, string>> }

export interface AskRequest { symbol: string; question: string; as_of_time?: string | null; no_research: boolean }
export interface AskResponse {
  request_id: string; trace_id: string; status: ResultStatus; answer: string;
  intent: string; entities: Array<Record<string, unknown>>; as_of_time: string;
  facts: Array<Record<string, unknown>>; references: string[]; evidence_refs: string[];
  knowledge_refs: string[]; report_refs: string[]; limitations: string[]; reason_codes: string[];
}
export interface AskTrace {
  trace_id: string; request_id: string; intent: string; raw_question: string;
  normalized_question: string; canonical_as_of_time: string;
  query_plan: Record<string, unknown>; tool_invocations: Array<Record<string, unknown>>;
  reason_codes: string[]; limitations: string[]; timings: Record<string, number>;
  [key: string]: unknown;
}

export interface MetricDefinition {
  metric_id: string; display_name: string; description: string; unit: string;
  value_type: string; supported_dimensions: string[]; time_semantics: string;
  source_capability: string; availability: "READY" | "PARTIAL" | "UNAVAILABLE"; version: string;
}
export interface DimensionDefinition {
  dimension_id: string; display_name: string; description: string; value_type: string;
  time_semantics: string; source_capability: string;
  availability: "READY" | "PARTIAL" | "UNAVAILABLE"; version: string;
}
export interface AnalyticsSchema {
  metrics: MetricDefinition[]; dimensions: DimensionDefinition[];
  query_limits: { max_metrics: number; max_dimensions: number; max_entities: number;
    max_filters: number; max_sorts: number; max_rows: number; max_date_range_days: number };
  metric_registry_version: string; dimension_registry_version: string;
}
export interface SemanticQuery {
  metrics: string[]; dimensions: string[]; entities: string[];
  time_range: { start: string; end: string };
  filters: Array<{ dimension: string; operator: "EQ"; value: string }>;
  sort: Array<{ field: string; direction: "ASC" | "DESC" }>;
  limit: number; as_of_time?: string | null;
}
export interface AvailabilityRecord { status: "READY" | "PARTIAL" | "UNAVAILABLE"; record_count: number | null; reason_code: string | null }
export interface ChartSpec { chart_type: "line" | "bar" | "table"; x: string | null; y: string[]; series: string | null; title: string; unit: string | null }
export interface SemanticResult {
  query_id: string; plan_hash: string; trace_id: string; status: "PASS" | "PARTIAL" | "FAILED";
  schema: Array<{ name: string; kind: string; value_type: string; unit?: string }>;
  rows: Array<Record<string, unknown>>; metrics: string[]; dimensions: string[]; as_of_time: string;
  availability: Record<string, AvailabilityRecord>; limitations: string[]; reason_codes: string[];
  provenance: { dataset_refs: string[]; providers: string[]; metric_definition_version: string;
    dimension_definition_version: string; data_cutoff: string; source_capabilities: string[] };
  insight: { title: string; summary: string; metrics: string[]; comparison: string | null;
    references: string[]; limitations: string[] };
  chart: ChartSpec;
}

export interface Pagination { limit: number; offset: number; total: number }
export interface ReportSummary { report_id: string; type: "daily"; trade_date: string; as_of_time: string; generated_at: string; mode: string; schema_version: string; refs: string[] }
export interface ReportList { items: ReportSummary[]; pagination: Pagination }
export type ReportDetail = Record<string, unknown> & { report_id: string };

export interface ReplayCampaignSummary {
  campaign_id: string; created_at: string; replay_mode: ReplayMode;
  identity_temporal_semantics: IdentitySemantics; universe: Record<string, unknown>;
  summary: Record<string, unknown>;
}
export interface ReplayCampaignList { items: ReplayCampaignSummary[]; pagination: Pagination }
export type ReplayCampaignDetail = Record<string, unknown> & ReplayCampaignSummary;
export interface ReplayFailures {
  campaign_id: string; replay_mode: ReplayMode; identity_temporal_semantics: IdentitySemantics;
  failures: Array<{ replay_id: string; date: string; symbol: string; stage: string;
    reason_codes: string[]; available_inputs: string[]; missing_inputs: string[]; trace_id: string | null }>;
}
