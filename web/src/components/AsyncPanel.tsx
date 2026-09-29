import type { ReactNode } from "react";
import { ResearchApiError } from "../api/client";
import { useI18n } from "../i18n";

export function AsyncPanel({ loading, error, children, empty, isEmpty = false, onRetry }:
  { loading: boolean; error: unknown; children: ReactNode; empty?: ReactNode; isEmpty?: boolean; onRetry?: () => void }) {
  const { t } = useI18n();
  if (loading) return <div className="state-panel" role="status">{t("common.loading")}</div>;
  if (error) return <ErrorNotice error={error} onRetry={onRetry} />;
  if (isEmpty) return <div className="state-panel state-empty">{empty ?? t("common.noRecords")}</div>;
  return <>{children}</>;
}

export function ErrorNotice({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const { t } = useI18n();
  if (error instanceof ResearchApiError) {
    const text = `${error.body.code}: ${error.body.message}\nCorrelation ID: ${error.body.request_id}`;
    return <section className="error-notice" role="alert">
      <strong>{error.body.code}</strong><p>{error.body.message}</p>
      <small>Correlation ID: {error.body.request_id}</small>
      <button type="button" className="button-quiet" onClick={() => navigator.clipboard?.writeText(text)}>{t("common.copyError")}</button>{onRetry && <button type="button" className="button-secondary" onClick={onRetry}>{t("common.retry")}</button>}
    </section>;
  }
  return <section className="error-notice" role="alert"><strong>REQUEST_FAILED</strong><p>{t("runtime.unavailable")}</p>{onRetry && <button type="button" className="button-secondary" onClick={onRetry}>{t("common.retry")}</button>}</section>;
}
