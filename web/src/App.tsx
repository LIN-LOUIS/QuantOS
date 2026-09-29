import { lazy, Suspense, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import type { ResearchApi } from "./api/client";
import { ApiProvider, useApi } from "./api/context";
import { useAsync } from "./hooks/useAsync";
import { StatusBadge } from "./components/StatusBadge";
import { OverviewPage } from "./pages/OverviewPage";
import { AskPage } from "./pages/AskPage";
import { I18nProvider, useI18n } from "./i18n";
import { FirstRunGuide } from "./components/FirstRunGuide";
import { FeedbackPanel } from "./components/FeedbackPanel";

const AnalyticsPage = lazy(() => import("./pages/AnalyticsPage").then((module) => ({ default: module.AnalyticsPage })));
const ReportsPage = lazy(() => import("./pages/ReportsPage").then((module) => ({ default: module.ReportsPage })));
const ReplayPage = lazy(() => import("./pages/ReplayPage").then((module) => ({ default: module.ReplayPage })));

const navigation = [
  ["/", "nav.overview", "01"], ["/ask", "nav.ask", "02"], ["/analytics", "nav.analytics", "03"],
  ["/reports", "nav.reports", "04"], ["/replay", "nav.replay", "05"],
];

export function App({ api }: { api?: ResearchApi }) {
  return <I18nProvider><ApiProvider api={api}><Workspace /></ApiProvider></I18nProvider>;
}

function Workspace() {
  const api = useApi();
  const { locale, setLocale, t } = useI18n();
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const health = useAsync(() => api.health(), [api]);
  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">Q</div><div><strong>QuantOS</strong><span>{t("shell.subtitle")}</span></div></div>
      <nav aria-label="Primary navigation">{navigation.map(([path, label, index]) =>
        <NavLink key={path} to={path} end={path === "/"} className={({ isActive }) => isActive ? "nav-link active" : "nav-link"}>
          <span>{index}</span>{t(label)}
        </NavLink>)}</nav>
      <footer><span className="security-label">READ ONLY</span><p>{t("shell.boundary")}</p></footer>
    </aside>
    <div className="workspace">
      <header className="topbar"><div><span className="eyebrow">{t("shell.audit")}</span></div>
        <div className="topbar-tools"><button type="button" className="button-quiet topbar-button" onClick={() => setFeedbackOpen(true)}>{t("shell.feedback")}</button>
          <label className="language-select"><span className="sr-only">Language</span><select aria-label="Language" value={locale} onChange={(event) => setLocale(event.target.value as "zh-CN" | "en-US")}><option value="zh-CN">简体中文</option><option value="en-US">English</option></select></label>
          <div className="topbar-status"><span>API</span>{health.data ? <StatusBadge status="READY" /> : health.error ? <StatusBadge status="UNAVAILABLE" /> : <span className="muted">{t("shell.checking")}</span>}</div>
        </div>
      </header>
      {health.error ? <section className="runtime-alert api-disconnected" role="alert"><div><strong>{t("runtime.unavailable")}</strong><span>{t("runtime.retryHint")}</span></div><button type="button" className="button-secondary" onClick={health.reload}>{t("runtime.retry")}</button></section> : null}
      {health.data?.runtime_mode === "DEMO" && <section className="runtime-alert demo-banner"><div><strong>{t("runtime.demo")}</strong><span>{t("runtime.demoDetail")}</span></div><code>QuantOS {health.data.quantos_version} · {health.data.build_commit}</code></section>}
      {health.data?.runtime_mode === "LOCAL" && <section className="runtime-strip"><span>{t("runtime.local")}</span><code>QuantOS {health.data.quantos_version} · {health.data.build_commit}</code></section>}
      <FirstRunGuide />
      <main><Suspense fallback={<div className="state-panel" role="status">{t("shell.loading")}</div>}><Routes>
        <Route path="/" element={<OverviewPage health={health} />} />
        <Route path="/ask" element={<AskPage />} />
        <Route path="/analytics" element={<AnalyticsPage />} />
        <Route path="/reports" element={<ReportsPage />} />
        <Route path="/replay" element={<ReplayPage />} />
      </Routes></Suspense></main>
      <FeedbackPanel open={feedbackOpen} onClose={() => setFeedbackOpen(false)} />
    </div>
  </div>;
}
