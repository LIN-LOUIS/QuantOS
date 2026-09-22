import type { ReactNode } from "react";
import { ResearchApiError } from "../api/client";

export function AsyncPanel({ loading, error, children, empty, isEmpty = false, onRetry }:
  { loading: boolean; error: unknown; children: ReactNode; empty?: ReactNode; isEmpty?: boolean; onRetry?: () => void }) {
  if (loading) return <div className="state-panel" role="status">Loading research context…</div>;
  if (error) return <ErrorNotice error={error} onRetry={onRetry} />;
  if (isEmpty) return <div className="state-panel state-empty">{empty ?? "No matching records."}</div>;
  return <>{children}</>;
}

export function ErrorNotice({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  if (error instanceof ResearchApiError) {
    const text = `${error.body.code}: ${error.body.message}\nCorrelation ID: ${error.body.request_id}`;
    return <section className="error-notice" role="alert">
      <strong>{error.body.code}</strong><p>{error.body.message}</p>
      <small>Correlation ID: {error.body.request_id}</small>
      <button type="button" className="button-quiet" onClick={() => navigator.clipboard?.writeText(text)}>Copy error details</button>{onRetry && <button type="button" className="button-secondary" onClick={onRetry}>Retry</button>}
    </section>;
  }
  return <section className="error-notice" role="alert"><strong>REQUEST_FAILED</strong><p>QuantOS Research API is unavailable.</p>{onRetry && <button type="button" className="button-secondary" onClick={onRetry}>Retry</button>}</section>;
}
