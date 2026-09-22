import { useState } from "react";
import { useApi } from "../api/context";
import { useAsync } from "../hooks/useAsync";
import { AsyncPanel, ErrorNotice } from "../components/AsyncPanel";
import { DataTable } from "../components/DataTable";
import { Drawer } from "../components/Drawer";
import { StatusBadge } from "../components/StatusBadge";
import type { ReplayCampaignDetail, ReplayFailures, ReplayMode } from "../api/types";
import { PageHeading, SectionTitle } from "./OverviewPage";

export function ReplayPage() {
  const api = useApi(); const [mode, setMode] = useState("");
  const [offset, setOffset] = useState(0); const limit = 20;
  const campaigns = useAsync(() => api.replayCampaigns({ mode, limit, offset }), [api, mode, offset]);
  const [detail, setDetail] = useState<ReplayCampaignDetail | null>(null); const [failures, setFailures] = useState<ReplayFailures | null>(null);
  const [open, setOpen] = useState(false); const [error, setError] = useState<unknown>(null);
  const inspect = async (id: string) => { setOpen(true); setDetail(null); setFailures(null); setError(null); try { const [campaign, values] = await Promise.all([api.replayCampaign(id), api.replayFailures(id)]); setDetail(campaign); setFailures(values); } catch (value) { setError(value); } };
  return <div className="page"><PageHeading kicker="HISTORICAL REPLAY" title="Time semantics made visible" description="Inspect frozen campaigns, PIT counters, determinism, and structured failure records." />
    <div className="mode-guide"><ModeCard mode="STRICT_OPERATIONAL_PIT" /><ModeCard mode="RETROSPECTIVE_RECONSTRUCTED" /></div>
    <label className="mode-filter">Replay mode<select value={mode} onChange={(event) => { setOffset(0); setMode(event.target.value); }}><option value="">All modes</option><option value="STRICT_OPERATIONAL_PIT">Strict Operational</option><option value="RETROSPECTIVE_RECONSTRUCTED">Retrospective Reconstructed</option></select></label>
    <AsyncPanel loading={campaigns.loading} error={campaigns.error} isEmpty={campaigns.data?.items.length === 0} empty={<><strong>No replay campaign available</strong><p>Replay artifacts must be created through the explicit offline replay workflow.</p></>}>
      {campaigns.data && <section className="panel"><SectionTitle title="Replay campaigns" subtitle={`${campaigns.data.pagination.total} persisted campaign(s)`} /><div className="campaign-list">{campaigns.data.items.map((item) => <button type="button" className="campaign-row" key={item.campaign_id} onClick={() => inspect(item.campaign_id)}><div><StatusBadge status={summaryStatus(item.summary)} /><strong>{modeLabel(item.replay_mode)}</strong><span>{dateRange(item.universe)}</span></div><div className="campaign-metrics"><span>PASS {readNumber(item.summary.pass_points)}</span><span>PARTIAL {readNumber(item.summary.partial_points)}</span><span>FAILED {readNumber(item.summary.failed_points)}</span><code>{item.campaign_id.slice(0, 10)}</code></div></button>)}</div><nav className="pagination" aria-label="Replay pagination"><button type="button" className="button-quiet" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>Previous</button><span>{campaigns.data.pagination.total ? `${offset + 1}–${Math.min(offset + limit, campaigns.data.pagination.total)} of ${campaigns.data.pagination.total}` : "0 campaigns"}</span><button type="button" className="button-quiet" disabled={offset + limit >= campaigns.data.pagination.total} onClick={() => setOffset(offset + limit)}>Next</button></nav></section>}
    </AsyncPanel>
    <Drawer title="Replay campaign audit" open={open} onClose={() => setOpen(false)}>{error ? <ErrorNotice error={error} /> : detail && failures ? <ReplayDetail detail={detail} failures={failures} /> : <div className="state-panel">Loading campaign and failure dataset…</div>}</Drawer>
  </div>;
}

function ModeCard({ mode }: { mode: ReplayMode }) { return <article className="mode-card"><span className="eyebrow">{mode === "STRICT_OPERATIONAL_PIT" ? "STRICT OPERATIONAL" : "RETROSPECTIVE RECONSTRUCTED"}</span><h3>{modeLabel(mode)}</h3><p>{modeDescription(mode)}</p></article>; }
function ReplayDetail({ detail, failures }: { detail: ReplayCampaignDetail; failures: ReplayFailures }) {
  const summary = detail.summary as Record<string, unknown>;
  const counters = ["requested_points", "executed_points", "pass_points", "partial_points", "failed_points", "pit_rejection_count", "deterministic_mismatch_count", "market_pit_violation_count", "evidence_pit_violation_count", "report_pit_violation_count", "knowledge_pit_violation_count"];
  return <div className="trace-detail"><StatusBadge status={summaryStatus(summary)} /><h3>{modeLabel(detail.replay_mode)}</h3><p>{modeDescription(detail.replay_mode)}</p><dl><dt>Identity semantics</dt><dd><code>{detail.identity_temporal_semantics}</code></dd><dt>Campaign ID</dt><dd><code>{detail.campaign_id}</code></dd><dt>Created</dt><dd>{detail.created_at}</dd><dt>Universe</dt><dd>{JSON.stringify(detail.universe)}</dd></dl>
    <h3>Coverage & integrity</h3><div className="metric-grid">{counters.map((key) => <div key={key}><span>{key.replaceAll("_", " ")}</span><strong>{readNumber(summary[key])}</strong></div>)}</div>
    <h3>Failure analysis</h3>{failures.failures.length ? <DataTable rows={failures.failures} columns={["date", "symbol", "stage", "reason_codes", "missing_inputs"]} /> : <p className="ready-copy">No PARTIAL or FAILED records in the persisted failure dataset.</p>}
  </div>;
}

function modeLabel(mode: ReplayMode) { return mode === "STRICT_OPERATIONAL_PIT" ? "Strict Operational" : "Retrospective Reconstructed"; }
function modeDescription(mode: ReplayMode) { return mode === "STRICT_OPERATIONAL_PIT" ? "What QuantOS actually knew at that point in time." : "Historical market truth reconstructed from authoritative effective-time identity records; not an operational-knowledge replay."; }
function summaryStatus(summary: Record<string, unknown>): string { return readNumber(summary.failed_points) > 0 ? "FAILED" : readNumber(summary.partial_points) > 0 ? "PARTIAL" : "PASS"; }
function readNumber(value: unknown): number { return typeof value === "number" ? value : 0; }
function dateRange(universe: Record<string, unknown>): string { return `${String(universe.start_date ?? "—")} → ${String(universe.end_date ?? "—")}`; }
