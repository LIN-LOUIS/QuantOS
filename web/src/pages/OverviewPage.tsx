import { useApi } from "../api/context";
import { useAsync } from "../hooks/useAsync";
import { AsyncPanel } from "../components/AsyncPanel";
import { StatusBadge } from "../components/StatusBadge";
import type { AvailabilityState, HealthResponse } from "../api/types";
import type { AsyncState } from "../hooks/useAsync";

export function OverviewPage({ health }: { health: AsyncState<HealthResponse> }) {
  const api = useApi();
  const status = useAsync(() => api.status(), [api]);
  if (health.error) return <div className="page"><PageHeading kicker="SYSTEM VIEW" title="Research readiness, without false certainty" description="See what QuantOS can support now, what is partial, and what remains unavailable before starting analysis." />
    <div className="state-panel state-error">Research context will load after the API reconnects.</div>
  </div>;
  return <div className="page"><PageHeading kicker="SYSTEM VIEW" title="Research readiness, without false certainty" description="See what QuantOS can support now, what is partial, and what remains unavailable before starting analysis." />
    <AsyncPanel loading={health.loading || status.loading} error={status.error} onRetry={status.reload}>
      {health.data && status.data && <>
        <section className="overview-hero panel"><div><span className="eyebrow">PROCESS HEALTH</span><h2>{health.data.service}</h2><p>Versioned {health.data.api_version} process is responding on the local research boundary.</p></div><StatusBadge status="READY" /></section>
        <section className="first-use panel"><div><span className="eyebrow">START A RESEARCH FLOW</span><h2>Observe, ask, analyze, then inspect provenance.</h2><p>Use Ask for grounded facts, Analytics for bounded metrics, and Replay to inspect historical semantics.</p></div><div><a className="button-secondary" href="/ask">Start with Ask</a><a className="button-secondary" href="/analytics">Explore Analytics</a><a className="button-secondary" href="/replay">Inspect Replay</a></div></section>
        <section><SectionTitle title="Capability availability" subtitle="Exact backend states are preserved." />
          <div className="availability-grid">
            {Object.entries(status.data.research_availability).map(([name, value]) => <AvailabilityCard key={name} name={name} status={value} />)}
            {availabilityEntries(status.data.data_availability).map(([name, value]) => <AvailabilityCard key={name} name={name} status={value} />)}
            <AvailabilityCard name="historical replay" status={status.data.replay_availability.status} detail={status.data.replay_availability.reason_code ?? undefined} />
          </div>
        </section>
        <section><SectionTitle title="Provider boundary" subtitle="Only configuration and health metadata are exposed." />
          <div className="provider-list">{status.data.providers.length ? status.data.providers.map((provider) => <article className="provider-row" key={provider.provider_id}>
            <div><strong>{provider.provider_id}</strong><span>{provider.provider_type ?? "provider"}</span></div><StatusBadge status={provider.availability} />
          </article>) : <div className="state-panel state-empty">No provider health records are exposed.</div>}</div>
        </section>
      </>}
    </AsyncPanel>
  </div>;
}

function availabilityEntries(value: Record<string, unknown>): Array<[string, AvailabilityState]> {
  const found: Array<[string, AvailabilityState]> = [];
  for (const [name, item] of Object.entries(value)) {
    if (name === "historical_replay") continue;
    if (typeof item === "string") found.push([name, item as AvailabilityState]);
    else if (item && typeof item === "object") {
      const nested = item as Record<string, unknown>;
      const state = nested.availability ?? nested.status ?? nested.readiness;
      if (typeof state === "string") found.push([name, state as AvailabilityState]);
    }
  }
  return found;
}

function AvailabilityCard({ name, status, detail }: { name: string; status: AvailabilityState; detail?: string }) {
  return <article className="availability-card"><div className="availability-heading"><span>{name.replaceAll("_", " ")}</span><StatusBadge status={status} /></div>{detail && <small>{detail}</small>}</article>;
}

export function PageHeading({ kicker, title, description }: { kicker: string; title: string; description: string }) {
  return <header className="page-heading"><span className="eyebrow">{kicker}</span><h1>{title}</h1><p>{description}</p></header>;
}
export function SectionTitle({ title, subtitle }: { title: string; subtitle?: string }) {
  return <div className="section-title"><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>;
}
