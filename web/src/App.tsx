import { lazy, Suspense } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import type { ResearchApi } from "./api/client";
import { ApiProvider, useApi } from "./api/context";
import { useAsync } from "./hooks/useAsync";
import { StatusBadge } from "./components/StatusBadge";
import { OverviewPage } from "./pages/OverviewPage";
import { AskPage } from "./pages/AskPage";

const AnalyticsPage = lazy(() => import("./pages/AnalyticsPage").then((module) => ({ default: module.AnalyticsPage })));
const ReportsPage = lazy(() => import("./pages/ReportsPage").then((module) => ({ default: module.ReportsPage })));
const ReplayPage = lazy(() => import("./pages/ReplayPage").then((module) => ({ default: module.ReplayPage })));

const navigation = [
  ["/", "Overview", "01"], ["/ask", "Ask", "02"], ["/analytics", "Analytics", "03"],
  ["/reports", "Reports", "04"], ["/replay", "Replay", "05"],
];

export function App({ api }: { api?: ResearchApi }) {
  return <ApiProvider api={api}><Workspace /></ApiProvider>;
}

function Workspace() {
  const api = useApi();
  const health = useAsync(() => api.health(), [api]);
  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">Q</div><div><strong>QuantOS</strong><span>Research Workspace</span></div></div>
      <nav aria-label="Primary navigation">{navigation.map(([path, label, index]) =>
        <NavLink key={path} to={path} end={path === "/"} className={({ isActive }) => isActive ? "nav-link active" : "nav-link"}>
          <span>{index}</span>{label}
        </NavLink>)}</nav>
      <footer><span className="security-label">LOCAL / READ ONLY</span><p>Research API is the only data boundary.</p></footer>
    </aside>
    <div className="workspace">
      <header className="topbar"><div><span className="eyebrow">AUDITABLE FINANCIAL RESEARCH</span></div>
        <div className="topbar-status"><span>API</span>{health.data ? <StatusBadge status="READY" /> : health.error ? <StatusBadge status="UNAVAILABLE" /> : <span className="muted">CHECKING</span>}</div>
      </header>
      {health.error ? <section className="runtime-alert api-disconnected" role="alert"><div><strong>QuantOS Research API is unavailable.</strong><span>Check the local launcher, then retry the bounded health request.</span></div><button type="button" className="button-secondary" onClick={health.reload}>Retry API</button></section> : null}
      {health.data?.runtime_mode === "DEMO" && <section className="runtime-alert demo-banner"><div><strong>DEMO DATA</strong><span>Synthetic fixture data · For research/product demonstration · Not investment advice.</span></div><code>QuantOS {health.data.quantos_version} · {health.data.build_commit}</code></section>}
      {health.data?.runtime_mode === "LOCAL" && <section className="runtime-strip"><span>LOCAL PERSISTED DATA</span><code>QuantOS {health.data.quantos_version} · {health.data.build_commit}</code></section>}
      <main><Suspense fallback={<div className="state-panel" role="status">Loading workspace module…</div>}><Routes>
        <Route path="/" element={<OverviewPage health={health} />} />
        <Route path="/ask" element={<AskPage />} />
        <Route path="/analytics" element={<AnalyticsPage />} />
        <Route path="/reports" element={<ReportsPage />} />
        <Route path="/replay" element={<ReplayPage />} />
      </Routes></Suspense></main>
    </div>
  </div>;
}
