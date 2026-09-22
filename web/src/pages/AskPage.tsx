import { useState, type FormEvent } from "react";
import { useApi } from "../api/context";
import type { AskResponse, AskTrace } from "../api/types";
import { ErrorNotice } from "../components/AsyncPanel";
import { DataTable } from "../components/DataTable";
import { Drawer } from "../components/Drawer";
import { ReasonCodes } from "../components/ReasonCodes";
import { StatusBadge } from "../components/StatusBadge";
import { PageHeading, SectionTitle } from "./OverviewPage";

export function AskPage() {
  const api = useApi();
  const [symbol, setSymbol] = useState("600519.SH");
  const [question, setQuestion] = useState("最近一个交易日表现怎么样？");
  const [noResearch, setNoResearch] = useState(true);
  const [result, setResult] = useState<AskResponse | null>(null);
  const [trace, setTrace] = useState<AskTrace | null>(null);
  const [traceOpen, setTraceOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setLoading(true); setError(null); setResult(null);
    try { setResult(await api.ask({ symbol, question, no_research: noResearch })); }
    catch (value) { setError(value); }
    finally { setLoading(false); }
  };
  const showTrace = async () => {
    if (!result) return;
    setTraceOpen(true); setTrace(null);
    try { setTrace(await api.trace(result.trace_id)); } catch (value) { setError(value); setTraceOpen(false); }
  };
  return <div className="page"><PageHeading kicker="GROUNDED ASK" title="Ask, then inspect why" description="Answers are separated into facts, references, limitations, and execution lineage." />
    <form className="ask-form panel" onSubmit={submit}>
      <label>Security<input value={symbol} onChange={(event) => setSymbol(event.target.value)} pattern="[0-9]{6}\.(SH|SZ|BJ)" required /></label>
      <label className="question-field">Research question<textarea value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={1000} required /></label>
      <label className="checkbox"><input type="checkbox" checked={noResearch} onChange={(event) => setNoResearch(event.target.checked)} />Market-only offline context</label>
      <button className="button-primary" type="submit" disabled={loading}>{loading ? "Building grounded answer…" : "Ask QuantOS"}</button>
    </form>
    {error !== null && <ErrorNotice error={error} />}
    {result && <article className="result-stack">
      <section className="panel answer-panel"><div className="result-header"><div><span className="eyebrow">ANSWER · {result.intent}</span><h2>{result.entities[0]?.canonical_id as string ?? symbol}</h2></div><StatusBadge status={result.status} /></div><p className="answer-copy">{result.answer}</p><div className="result-actions"><button className="button-secondary" type="button" onClick={showTrace}>Why this answer?</button><code className="hash">TRACE {result.trace_id}</code></div></section>
      <div className="two-column"><section className="panel"><SectionTitle title="Facts" subtitle={`Cutoff ${result.as_of_time}`} />{result.facts.length ? <DataTable rows={result.facts} /> : <p className="empty-copy">No validated facts were returned.</p>}</section>
        <section className="panel"><SectionTitle title="Availability & limitations" />{result.limitations.length ? <ul className="limitations">{result.limitations.map((item) => <li key={item}>{item}</li>)}</ul> : <p className="ready-copy">No additional limitations reported.</p>}<ReasonCodes codes={result.reason_codes} /></section></div>
      <section className="panel"><SectionTitle title="Grounding references" subtitle="Typed references are audit links, not generic footnotes." />{result.references.length ? <div className="reference-list">{result.references.map((ref) => <span key={ref}>{referenceKind(ref)} · <code>{ref}</code></span>)}</div> : <p className="empty-copy">No research references. The answer is limited to structured facts.</p>}</section>
    </article>}
    <Drawer title="Why this answer?" open={traceOpen} onClose={() => setTraceOpen(false)}>{trace ? <TraceDetail trace={trace} /> : <div className="state-panel" role="status">Loading persisted trace…</div>}</Drawer>
  </div>;
}

function referenceKind(ref: string): string { return ref.startsWith("E") ? "Evidence" : ref.startsWith("K") ? "Knowledge" : ref.startsWith("R") ? "Report" : "Fact"; }
function TraceDetail({ trace }: { trace: AskTrace }) {
  return <div className="trace-detail"><dl><dt>Trace ID</dt><dd><code>{trace.trace_id}</code></dd><dt>Intent</dt><dd>{trace.intent}</dd><dt>Canonical cutoff</dt><dd>{trace.canonical_as_of_time ?? String(trace.query_plan.as_of_time ?? "—")}</dd><dt>Question</dt><dd>{trace.normalized_question}</dd></dl>
    <h3>Entity resolution</h3><pre>{JSON.stringify(trace.resolved_entities ?? trace.entities ?? [], null, 2)}</pre>
    <h3>Query plan</h3><pre>{JSON.stringify(trace.query_plan, null, 2)}</pre>
    <h3>Capabilities executed</h3>{trace.tool_invocations?.length ? <DataTable rows={trace.tool_invocations} /> : <p className="empty-copy">No capability invocations recorded.</p>}
    <h3>References & duration</h3><pre>{JSON.stringify({ evidence_refs: trace.evidence_refs ?? [], knowledge_refs: trace.knowledge_refs ?? [], report_refs: trace.report_refs ?? [], timings: trace.timings ?? {} }, null, 2)}</pre>
    <ReasonCodes codes={trace.reason_codes ?? []} />
    {!!trace.limitations?.length && <ul className="limitations">{trace.limitations.map((item) => <li key={item}>{item}</li>)}</ul>}
  </div>;
}
