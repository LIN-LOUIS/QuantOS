import { useState, type FormEvent } from "react";
import { useApi } from "../api/context";
import type { SemanticQuery, SemanticResult } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { AsyncPanel, ErrorNotice } from "../components/AsyncPanel";
import { ChartRenderer } from "../charts/ChartRenderer";
import { DataTable } from "../components/DataTable";
import { Drawer } from "../components/Drawer";
import { ReasonCodes } from "../components/ReasonCodes";
import { StatusBadge } from "../components/StatusBadge";
import { PageHeading, SectionTitle } from "./OverviewPage";

export function AnalyticsPage() {
  const api = useApi();
  const registry = useAsync(() => api.analyticsSchema(), [api]);
  const [metric, setMetric] = useState("close");
  const [dimension, setDimension] = useState("trading_date");
  const [symbol, setSymbol] = useState("600519.SH");
  const [start, setStart] = useState("2026-08-20");
  const [end, setEnd] = useState("2026-09-18");
  const [limit, setLimit] = useState(20);
  const [direction, setDirection] = useState<"ASC" | "DESC">("ASC");
  const [result, setResult] = useState<SemanticResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [provenanceOpen, setProvenanceOpen] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setLoading(true); setError(null); setResult(null);
    const query: SemanticQuery = { metrics: [metric], dimensions: [dimension], entities: [symbol], time_range: { start, end }, filters: [], sort: [{ field: dimension, direction }], limit };
    try { setResult(await api.analyticsQuery(query)); } catch (value) { setError(value); }
    finally { setLoading(false); }
  };
  return <div className="page"><PageHeading kicker="SEMANTIC ANALYTICS" title="Numbers with provenance" description="Build a bounded query from registered metrics and dimensions. There is no SQL surface." />
    <AsyncPanel loading={registry.loading} error={registry.error}>{registry.data && <form className="query-builder panel" onSubmit={submit}>
      <label>Metric<select value={metric} onChange={(event) => setMetric(event.target.value)}>{registry.data.metrics.map((item) => <option key={item.metric_id} value={item.metric_id}>{item.display_name} · {item.metric_id}</option>)}</select></label>
      <label>Dimension<select value={dimension} onChange={(event) => setDimension(event.target.value)}>{registry.data.dimensions.map((item) => <option key={item.dimension_id} value={item.dimension_id}>{item.display_name} · {item.dimension_id}</option>)}</select></label>
      <label>Security<input value={symbol} onChange={(event) => setSymbol(event.target.value)} required /></label>
      <label>Start date<input type="date" value={start} onChange={(event) => setStart(event.target.value)} required /></label>
      <label>End date<input type="date" value={end} onChange={(event) => setEnd(event.target.value)} required /></label>
      <label>Sort direction<select value={direction} onChange={(event) => setDirection(event.target.value as "ASC" | "DESC")}><option value="ASC">Ascending</option><option value="DESC">Descending</option></select></label>
      <label>Row limit<input type="number" min="1" max={registry.data.query_limits.max_rows} value={limit} onChange={(event) => setLimit(Number(event.target.value))} /></label>
      <button className="button-primary" type="submit" disabled={loading}>{loading ? "Running bounded query…" : "Run analytics"}</button>
      <small className="form-note">Registry {registry.data.metric_registry_version} · max {registry.data.query_limits.max_rows} rows</small>
    </form>}</AsyncPanel>
    {error !== null && <ErrorNotice error={error} />}
    {result && <AnalyticsResult result={result} onProvenance={() => setProvenanceOpen(true)} />}
    <Drawer title="Data provenance" open={provenanceOpen} onClose={() => setProvenanceOpen(false)}>{result && <Provenance result={result} />}</Drawer>
  </div>;
}

function AnalyticsResult({ result, onProvenance }: { result: SemanticResult; onProvenance: () => void }) {
  const availability = result.availability.market_data;
  const unavailable = availability?.status === "UNAVAILABLE" && availability.record_count === null;
  const empty = availability?.status === "READY" && availability.record_count === 0;
  return <div className="result-stack"><section className="panel"><div className="result-header"><div><span className="eyebrow">SEMANTIC RESULT</span><h2>{result.insight.title}</h2></div><StatusBadge status={result.status} /></div><p>{result.insight.summary}</p><div className="result-actions"><button className="button-secondary" type="button" onClick={onProvenance}>Data provenance</button><code className="hash">PLAN {result.plan_hash.slice(0, 12)}</code></div></section>
    <section className="panel"><SectionTitle title="Availability" subtitle="Unknown coverage is never represented as a numeric zero." /><div className="availability-grid">{Object.entries(result.availability).map(([name, value]) => <article className="availability-card" key={name}><div className="availability-heading"><span>{name}</span><StatusBadge status={value.status} /></div><strong>{value.record_count === null ? "Coverage unknown" : `${value.record_count} matching records`}</strong>{value.reason_code && <code>{value.reason_code}</code>}</article>)}</div></section>
    {unavailable ? <section className="state-panel state-unavailable"><strong>Historical data unavailable</strong><p>{result.limitations.join(" ")}</p></section> : empty ? <section className="state-panel state-empty"><strong>0 matching records</strong><p>The dataset is available, but this query returned no rows.</p></section> : <>
      <section className="panel"><SectionTitle title="Chart" subtitle={`Rendered from backend ChartSpec · ${result.chart.chart_type}`} /><ChartRenderer spec={result.chart} rows={result.rows} /></section>
      <section className="panel"><SectionTitle title="Exact values" subtitle="The table is the auditable view of the returned rows." /><DataTable rows={result.rows} columns={result.schema.map((item) => item.name)} /></section></>}
    <section className="panel"><SectionTitle title="Insight & limitations" /><p>{result.insight.summary}</p>{result.limitations.length ? <ul className="limitations">{result.limitations.map((item) => <li key={item}>{item}</li>)}</ul> : null}<ReasonCodes codes={result.reason_codes} /></section>
  </div>;
}

function Provenance({ result }: { result: SemanticResult }) {
  const value = result.provenance;
  return <div className="trace-detail"><dl><dt>Data cutoff</dt><dd>{value.data_cutoff}</dd><dt>Providers</dt><dd>{value.providers.join(", ") || "No provider rows returned"}</dd><dt>Metric registry</dt><dd>{value.metric_definition_version}</dd><dt>Dimension registry</dt><dd>{value.dimension_definition_version}</dd><dt>Source capabilities</dt><dd>{value.source_capabilities.join(", ")}</dd></dl><h3>Dataset references</h3>{value.dataset_refs.length ? <div className="reference-list">{value.dataset_refs.map((item) => <code key={item}>{item}</code>)}</div> : <p className="empty-copy">No rows means no dataset record references.</p>}</div>;
}
