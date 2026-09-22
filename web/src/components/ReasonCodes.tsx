const explanations: Record<string, string> = {
  ATTRIBUTION_INSUFFICIENT: "Causal explanation unavailable: current evidence does not meet attribution policy.",
  EVIDENCE_UNAVAILABLE: "Evidence history is unavailable for this boundary.",
  KNOWLEDGE_TEMPORAL_UNVERIFIED: "Knowledge has no verified historical availability time.",
  REPORT_NOT_VISIBLE: "No report is visible at the selected cutoff.",
  MARKET_DATA_UNAVAILABLE: "Persisted market data is unavailable.",
  NO_DATA: "The dataset is available, but no records matched.",
  OK: "The requested grounded result is available.",
};

export function ReasonCodes({ codes }: { codes: string[] }) {
  if (!codes.length) return null;
  return <div className="reason-list" aria-label="Reason codes">
    {codes.map((code) => <div className="reason-item" key={code}>
      <code>{code}</code><span>{explanations[code] ?? "QuantOS returned this deterministic reason code."}</span>
    </div>)}
  </div>;
}
