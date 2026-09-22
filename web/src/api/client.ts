import type {
  AnalyticsSchema, ApiErrorBody, AskRequest, AskResponse, AskTrace, HealthResponse,
  ReplayCampaignDetail, ReplayCampaignList, ReplayFailures, ReportDetail, ReportList,
  SemanticQuery, SemanticResult, StatusResponse,
} from "./types";

export class ResearchApiError extends Error {
  constructor(public readonly status: number, public readonly body: ApiErrorBody) {
    super(body.message);
  }
}

export interface ResearchApi {
  health(): Promise<HealthResponse>;
  status(): Promise<StatusResponse>;
  ask(request: AskRequest): Promise<AskResponse>;
  trace(traceId: string): Promise<AskTrace>;
  analyticsSchema(): Promise<AnalyticsSchema>;
  analyticsQuery(query: SemanticQuery): Promise<SemanticResult>;
  reports(params?: { date?: string; symbol?: string; limit?: number; offset?: number }): Promise<ReportList>;
  report(reportId: string): Promise<ReportDetail>;
  replayCampaigns(params?: { mode?: string; limit?: number; offset?: number }): Promise<ReplayCampaignList>;
  replayCampaign(campaignId: string): Promise<ReplayCampaignDetail>;
  replayFailures(campaignId: string): Promise<ReplayFailures>;
}

export class HttpResearchApi implements ResearchApi {
  constructor(private readonly baseUrl = "/v1") {}
  health = () => this.get<HealthResponse>("/health");
  status = () => this.get<StatusResponse>("/status");
  ask = (body: AskRequest) => this.send<AskResponse>("/ask", body);
  trace = (id: string) => this.get<AskTrace>(`/traces/${encodeURIComponent(id)}`);
  analyticsSchema = () => this.get<AnalyticsSchema>("/analytics/schema");
  analyticsQuery = (body: SemanticQuery) => this.send<SemanticResult>("/analytics/query", body);
  reports = (params = {}) => this.get<ReportList>(`/reports${query(params)}`);
  report = (id: string) => this.get<ReportDetail>(`/reports/${encodeURIComponent(id)}`);
  replayCampaigns = (params = {}) => this.get<ReplayCampaignList>(`/replay/campaigns${query(params)}`);
  replayCampaign = (id: string) => this.get<ReplayCampaignDetail>(`/replay/campaigns/${encodeURIComponent(id)}`);
  replayFailures = (id: string) => this.get<ReplayFailures>(`/replay/campaigns/${encodeURIComponent(id)}/failures`);

  private async get<T>(path: string): Promise<T> { return this.request<T>(path); }
  private async send<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  }
  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, init);
    const value = await response.json() as T | ApiErrorBody;
    if (!response.ok) throw new ResearchApiError(response.status, value as ApiErrorBody);
    return value as T;
  }
}

function query(params: Record<string, string | number | undefined>): string {
  const values = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== "") values.set(key, String(value)); });
  const encoded = values.toString();
  return encoded ? `?${encoded}` : "";
}
