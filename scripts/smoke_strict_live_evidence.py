"""One bounded real strict-live smoke for a complete daily Top20 candidate set."""

from __future__ import annotations

import json
from datetime import date, datetime

from benchmark_web_evidence import _load_candidates
from quantos.calibration import calibrate_attribution_evidence, calibrate_event_evidence
from quantos.collectors import BaoStockSecurityMasterCollector, ExaWebSearchProvider, TavilyWebSearchProvider
from quantos.config import DEFAULT_SETTINGS, MARKET_TIMEZONE
from quantos.discovery import collect_strict_live_candidate_evidence
from quantos.storage import NewsEvidenceRepository

TARGET_DATE = date(2026, 8, 31)
EVENT = datetime(2026, 8, 31, 15, tzinfo=MARKET_TIMEZONE)


def main() -> None:
    security_request_time = datetime.now(MARKET_TIMEZONE)
    securities = BaoStockSecurityMasterCollector().fetch_security_master(as_of_time=security_request_time)
    collection_cutoff = datetime.now(MARKET_TIMEZONE)
    candidates, flows = _load_candidates(collection_cutoff)
    master = {item.symbol: item for item in securities if item.available_at <= collection_cutoff}
    candidates = [item for item in candidates if item.symbol in master][:20]
    if len(candidates) != 20:
        raise RuntimeError("SecurityMaster coverage is insufficient for strict-live Top20")
    repository = NewsEvidenceRepository(DEFAULT_SETTINGS)
    output = collect_strict_live_candidate_evidence(
        trade_date=TARGET_DATE, as_of_time=collection_cutoff, candidates=candidates,
        securities=securities,
        providers={"tavily": TavilyWebSearchProvider(), "exa": ExaWebSearchProvider()},
        fund_flows=flows, max_queries_per_provider_per_run=20, repository=repository,
    )
    completed_at = datetime.now(MARKET_TIMEZONE)
    records = [record for result in output.provider_results for record in result.records]
    mentions = [mention for result in output.provider_results for mention in result.mentions]
    events = calibrate_event_evidence(records, mentions, market_event_time=EVENT)
    attribution = calibrate_attribution_evidence(
        events, candidates, flows, market_event_time=EVENT, as_of_time=completed_at,
    )
    facts = attribution.attribution_facts
    provider_rows = []
    for result in output.provider_results:
        provider_records = [item for item in records if item.provider == result.provider_name]
        provider_rows.append({
            "provider": result.provider_name, "status": result.status,
            "safe_error_type": result.safe_error_type, "requests": result.request_count,
            "results": len(provider_records), "failed_candidates": list(result.failed_candidates),
        })
    report = {
        "strict_live_target_date": TARGET_DATE, "collection_cutoff": collection_cutoff,
        "completed_at": completed_at, "query_strategy": "query_a_only",
        "max_queries_per_provider_per_run": 20, "providers": provider_rows,
        "strict_live_record_count": len(records),
        "strict_visible_at_initial_cutoff": len(output.search_results),
        "strict_event_evidence": len(events.event_evidence),
        "strict_historical_attribution_count": sum(item.strict_attribution_eligible for item in facts),
        "strict_post_event_context": sum(len(item.post_event_context) for item in attribution.bundles),
        "candidates_with_strict_evidence": len({item.query_symbol for item in records}),
        "candidates_with_strict_historical_attribution": len({item.symbol for item in facts if item.strict_attribution_eligible}),
        "all_records_strict": all(item.strict_pit and item.pit_mode == "strict_live" for item in records),
        "all_available_at_valid": all(item.available_at >= item.collected_at for item in records),
        "query_only_strict_attribution": sum(item.strict_attribution_eligible and item.entity_strength == "weak" for item in facts),
        "post_event_strict_attribution": sum(item.strict_attribution_eligible and item.temporal_relation == "post_event" for item in facts),
    }
    target = DEFAULT_SETTINGS.project_root / "reports" / "strict_live_smoke_2026-08-31.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
