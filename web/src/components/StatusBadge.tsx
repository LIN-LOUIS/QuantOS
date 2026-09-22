import type { AvailabilityState, ResultStatus } from "../api/types";

export function StatusBadge({ status }: { status: AvailabilityState | ResultStatus | string }) {
  const normalized = status.toUpperCase();
  return <span className={`status-badge status-${normalized.toLowerCase()}`} aria-label={`Status ${normalized}`}>
    <span aria-hidden="true" className="status-dot" />{normalized}
  </span>;
}
