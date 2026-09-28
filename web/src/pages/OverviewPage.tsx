import { useApi } from "../api/context";
import { useAsync } from "../hooks/useAsync";
import { AsyncPanel } from "../components/AsyncPanel";
import { StatusBadge } from "../components/StatusBadge";
import type { AvailabilityState, HealthResponse } from "../api/types";
import type { AsyncState } from "../hooks/useAsync";
import { Link } from "react-router-dom";
import { statusHelpKey, useI18n } from "../i18n";

export function OverviewPage({ health }: { health: AsyncState<HealthResponse> }) {
  const api = useApi();
  const { t } = useI18n();
  const status = useAsync(() => api.status(), [api]);
  if (health.error) return <div className="page"><PageHeading kicker={t("overview.kicker")} title={t("overview.title")} description={t("overview.description")} />
    <div className="state-panel state-error">{t("runtime.retryHint")}</div>
  </div>;
  return <div className="page"><PageHeading kicker={t("overview.kicker")} title={t("overview.title")} description={t("overview.description")} />
    <div className="overview-actions"><Link className="button-primary" to="/ask">{t("overview.start")}</Link><Link className="button-secondary" to="/reports">{t("overview.report")}</Link></div>
    <AsyncPanel loading={health.loading || status.loading} error={status.error} onRetry={status.reload}>
      {health.data && status.data && <>
        <section className="overview-hero panel"><div><span className="eyebrow">{t("overview.health")}</span><h2>{health.data.service}</h2><p>{t("overview.healthDetail", { version: health.data.api_version })}</p></div><StatusBadge status="READY" /></section>
        <section><SectionTitle title={t("overview.capabilities")} subtitle={t("overview.capabilitySubtitle")} />
          <div className="availability-grid">
            {Object.entries(status.data.research_availability).map(([name, value]) => <AvailabilityCard key={name} name={name} status={value} />)}
            {availabilityEntries(status.data.data_availability).map(([name, value]) => <AvailabilityCard key={name} name={name} status={value} />)}
            <AvailabilityCard name="historical replay" status={status.data.replay_availability.status} detail={status.data.replay_availability.reason_code ?? undefined} />
          </div>
        </section>
        <details className="technical-status panel"><summary><strong>{t("overview.technical")}</strong><span>{t("overview.technicalHint")}</span></summary>
          <div className="provider-list">{status.data.providers.length ? status.data.providers.map((provider) => <article className="provider-row" key={provider.provider_id}>
            <div><strong>{provider.provider_id}</strong><span>{provider.provider_type ?? "provider"}</span></div><StatusBadge status={provider.availability} />
          </article>) : <div className="state-panel state-empty">{t("overview.noProviders")}</div>}</div>
        </details>
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
  const { locale, t } = useI18n();
  const normalized = status.toUpperCase();
  const explanation = name.toLowerCase().includes("evidence") && normalized === "UNAVAILABLE" ? t("cap.evidenceUnavailable") : t(statusHelpKey(normalized));
  return <article className="availability-card"><div className="availability-heading"><span>{humanCapability(name, locale)}</span><StatusBadge status={status} /></div>{detail && <small><code>{detail}</code></small>}
    {normalized !== "READY" && normalized !== "PASS" && <details className="capability-explanation"><summary>{t("overview.why")}</summary><p>{explanation}</p></details>}
  </article>;
}

function humanCapability(name: string, locale: "zh-CN" | "en-US"): string {
  const key = name.toLowerCase().replaceAll(" ", "_");
  const labels: Record<string, [string, string]> = {
    market: ["Market data", "市场数据"], market_data: ["Market data", "市场数据"], security_master: ["Security Master", "证券主数据"], ask: ["Research Ask", "研究提问"],
    evidence: ["External evidence", "外部证据"], attribution: ["Attribution", "归因"], knowledge: ["Knowledge", "知识库"], reports: ["Reports", "研究报告"], report: ["Reports", "研究报告"], historical_market: ["Historical market", "历史行情"], historical_replay: ["Historical replay", "历史回放"],
  };
  return labels[key]?.[locale === "zh-CN" ? 1 : 0] ?? name.replaceAll("_", " ");
}

export function PageHeading({ kicker, title, description }: { kicker: string; title: string; description: string }) {
  return <header className="page-heading"><span className="eyebrow">{kicker}</span><h1>{title}</h1><p>{description}</p></header>;
}
export function SectionTitle({ title, subtitle }: { title: string; subtitle?: string }) {
  return <div className="section-title"><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>;
}
