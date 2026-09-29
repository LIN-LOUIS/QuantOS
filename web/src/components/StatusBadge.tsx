import type { AvailabilityState, ResultStatus } from "../api/types";
import { useI18n } from "../i18n";

export function StatusBadge({ status }: { status: AvailabilityState | ResultStatus | string }) {
  const { t } = useI18n();
  const normalized = status.toUpperCase();
  const key = `status.${normalized}`;
  const label = t(key) === key ? normalized : t(key);
  return <span className={`status-badge status-${normalized.toLowerCase()}`} aria-label={`Status ${normalized}`}>
    <span aria-hidden="true" className="status-dot" />{label}
  </span>;
}
