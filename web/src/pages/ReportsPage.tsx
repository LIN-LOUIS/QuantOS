import { useState } from "react";
import { useApi } from "../api/context";
import { useAsync } from "../hooks/useAsync";
import { AsyncPanel, ErrorNotice } from "../components/AsyncPanel";
import { DataTable, formatValue } from "../components/DataTable";
import { Drawer } from "../components/Drawer";
import type { ReportDetail } from "../api/types";
import { PageHeading, SectionTitle } from "./OverviewPage";

export function ReportsPage() {
  const api = useApi();
  const [date, setDate] = useState(""); const [symbol, setSymbol] = useState("");
  const [filters, setFilters] = useState({ date: "", symbol: "" });
  const [offset, setOffset] = useState(0); const limit = 20;
  const reports = useAsync(() => api.reports({ ...filters, limit, offset }), [api, filters.date, filters.symbol, offset]);
  const [detail, setDetail] = useState<ReportDetail | null>(null); const [open, setOpen] = useState(false); const [detailError, setDetailError] = useState<unknown>(null);
  const inspect = async (id: string) => { setOpen(true); setDetail(null); setDetailError(null); try { setDetail(await api.report(id)); } catch (error) { setDetailError(error); } };
  return <div className="page"><PageHeading kicker="REPORT ARCHIVE" title="Generated research, read in context" description="Browse existing report artifacts. Opening this page never generates or refreshes a report." />
    <form className="filter-bar panel" onSubmit={(event) => { event.preventDefault(); setOffset(0); setFilters({ date, symbol }); }}><label>Date<input type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label><label>Security<input placeholder="600519.SH" value={symbol} onChange={(event) => setSymbol(event.target.value)} /></label><button type="submit" className="button-secondary">Apply filters</button></form>
    <AsyncPanel loading={reports.loading} error={reports.error} isEmpty={reports.data?.items.length === 0} empty={<><strong>No report available</strong><p>No persisted report matches these bounded filters.</p></>}>
      {reports.data && <section className="panel"><SectionTitle title="Daily intelligence reports" subtitle={`${reports.data.pagination.total} persisted artifact(s)`} /><div className="report-list">{reports.data.items.map((item) => <button type="button" className="report-row" key={item.report_id} onClick={() => inspect(item.report_id)}><div><strong>{item.trade_date}</strong><span>{item.type} · {item.mode}</span></div><div><span>{new Date(item.generated_at).toLocaleString()}</span><code>{item.report_id.slice(0, 10)}</code></div></button>)}</div><Pagination offset={offset} limit={limit} total={reports.data.pagination.total} onChange={setOffset} /></section>}
    </AsyncPanel>
    <Drawer title="Report detail" open={open} onClose={() => setOpen(false)}>{detailError ? <ErrorNotice error={detailError} /> : detail ? <ReportView report={detail} /> : <div className="state-panel">Loading report…</div>}</Drawer>
  </div>;
}

function Pagination({ offset, limit, total, onChange }: { offset: number; limit: number; total: number; onChange: (value: number) => void }) {
  return <nav className="pagination" aria-label="Report pagination"><button type="button" className="button-quiet" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>Previous</button><span>{total ? `${offset + 1}–${Math.min(offset + limit, total)} of ${total}` : "0 reports"}</span><button type="button" className="button-quiet" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>Next</button></nav>;
}

function ReportView({ report }: { report: ReportDetail }) {
  const candidateRows = Array.isArray(report.candidate_briefs) ? report.candidate_briefs as Array<Record<string, unknown>> : [];
  const sector = report.sector_overview ?? report.sector_summary;
  const refs = collectRefs(report);
  return <div className="report-detail"><h3>Summary</h3><dl>{["trade_date", "as_of_time", "generated_at", "mode", "schema_version"].map((key) => <span key={key}><dt>{key}</dt><dd>{formatValue(report[key])}</dd></span>)}</dl>
    <h3>Candidates</h3>{candidateRows.length ? <DataTable rows={candidateRows} /> : <p className="empty-copy">No candidate briefs in this report.</p>}
    <h3>Sector</h3><p>{sector ? formatValue(sector) : "No sector section available."}</p>
    <h3>References</h3>{refs.length ? <div className="reference-list">{refs.map((ref) => <code key={ref}>{ref}</code>)}</div> : <p className="empty-copy">No typed references.</p>}
    <details><summary>Developer / Raw JSON</summary><pre>{JSON.stringify(report, null, 2)}</pre></details></div>;
}
function collectRefs(value: unknown): string[] {
  const refs = new Set<string>();
  const walk = (item: unknown) => { if (typeof item === "string" && /^(?:E|K|R|M)[:0-9]/.test(item)) refs.add(item); else if (Array.isArray(item)) item.forEach(walk); else if (item && typeof item === "object") Object.values(item as Record<string, unknown>).forEach(walk); };
  walk(value); return [...refs].sort();
}
