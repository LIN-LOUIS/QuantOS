"""Offline quality calibration of stored 2026-08-31 Tavily/Exa evidence."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime

from quantos.calibration import (
    calibrate_attribution_evidence, calibrate_event_evidence,
    link_stored_query_a_results,
)
from quantos.config import DEFAULT_SETTINGS, MARKET_TIMEZONE
from quantos.storage import NewsEvidenceRepository
from benchmark_web_evidence import _load_candidates

EVENT = datetime(2026, 8, 31, 15, tzinfo=MARKET_TIMEZONE)


def main() -> None:
    baseline_path = DEFAULT_SETTINGS.project_root / "reports" / "web_evidence_ab_2026-08-31.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    records = NewsEvidenceRepository(DEFAULT_SETTINGS).query_web_search_results(
        as_of_time=datetime.now(MARKET_TIMEZONE), pit_mode="source_timestamp_proxy"
    )
    mentions = link_stored_query_a_results(records)
    selection = calibrate_event_evidence(records, mentions, market_event_time=EVENT)
    candidates, flows = _load_candidates(datetime.now(MARKET_TIMEZONE))
    attribution = calibrate_attribution_evidence(
        selection, candidates, flows, market_event_time=EVENT,
        as_of_time=datetime.now(MARKET_TIMEZONE),
    )
    attribution_facts = {(item.provider, item.symbol, item.evidence_id): item
                         for item in attribution.attribution_facts}
    bundles = {item.candidate.symbol: item for item in attribution.bundles}
    strict_records = NewsEvidenceRepository(DEFAULT_SETTINGS).query_web_search_results(
        as_of_time=datetime.now(MARKET_TIMEZONE), pit_mode="strict_live"
    )
    strict_events = calibrate_event_evidence(
        strict_records, link_stored_query_a_results(strict_records), market_event_time=EVENT
    )
    strict_attribution = calibrate_attribution_evidence(
        strict_events, candidates, flows, market_event_time=EVENT,
        as_of_time=datetime.now(MARKET_TIMEZONE),
    )
    strict_bundles = {item.candidate.symbol: item for item in strict_attribution.bundles}
    strict_facts = strict_attribution.attribution_facts
    flags = {(item.provider, item.symbol, item.evidence_id): item for item in selection.quality_flags}
    records_by_symbol = defaultdict(list)
    for item in records:
        records_by_symbol[item.query_symbol].append(item)
    rows = []
    for candidate in baseline["top20"]:
        symbol = candidate["symbol"]
        discovered = records_by_symbol[symbol]
        eligible = [item for item in selection.event_evidence if item.query_symbol == symbol]
        bundle = bundles[symbol]
        attribution_records = list(bundle.attribution_evidence)
        post_records = list(bundle.post_event_context)
        evidence = []
        for item in eligible[:3]:
            quality = flags[(item.provider, symbol, item.result_id)]
            evidence.append({
                "provider": item.provider, "discovered_by": [item.provider], "title": item.title,
                "published_at": item.published_at, "time_delta_seconds": quality.time_delta_seconds,
                "inside_event_window": quality.inside_event_window,
                "evidence_tier": quality.evidence_tier, "match_method": quality.match_method,
                "eligibility_reason": quality.eligibility_reason, "domain": item.domain, "url": item.url,
            })
        def attribution_view(item):
            fact = attribution_facts[(item.provider, symbol, item.result_id)]
            return {"provider": item.provider, "title": item.title,
                    "published_at": item.published_at,
                    "source_temporal_relation": fact.source_temporal_relation,
                    "knowledge_temporal_relation": fact.knowledge_temporal_relation,
                    "time_delta_seconds": fact.time_delta_seconds, "entity_strength": fact.entity_strength,
                    "match_method": fact.match_method, "attribution_reason": fact.attribution_reason,
                    "pit_mode": item.pit_mode, "domain": item.domain, "url": item.url}
        rows.append({
            "rank": candidate["rank"], "symbol": symbol, "company_name": candidate["company_name"],
            "discovery_count": len(discovered), "event_count": len(eligible),
            "attribution_count": len(attribution_records),
            "tavily_event_count": sum(item.provider == "tavily" for item in eligible),
            "exa_event_count": sum(item.provider == "exa" for item in eligible),
            "query_only_count": sum(flags[(item.provider, symbol, item.result_id)].query_only_match for item in discovered),
            "post_event_count": sum(attribution_facts[(item.provider, symbol, item.result_id)].temporal_relation == "post_event"
                                    for item in discovered),
            "unknown_timestamp_count": sum(item.published_at is None for item in discovered),
            "strict_attribution_count": len(strict_bundles[symbol].strict_attribution_evidence),
            "attribution_evidence": [attribution_view(item) for item in attribution_records[:3]],
            "post_event_context": [attribution_view(item) for item in post_records[:2]],
        })
    provider_attribution = []
    for metrics in selection.provider_metrics:
        provider = metrics.provider_name
        provider_facts = [item for item in attribution.attribution_facts if item.provider == provider]
        event_ids = {(item.provider, item.result_id) for item in selection.event_evidence}
        query_only = [item for item in provider_facts if item.entity_strength == "weak" and item.match_method == "provider_exact_company_query"]
        provider_attribution.append({
            "provider_name": provider, "discovery_count": len(provider_facts),
            "event_evidence_count": metrics.event_eligible_count,
            "attribution_eligible_count": sum(item.attribution_eligible for item in provider_facts),
            "query_only_discovery": len(query_only),
            "query_only_event_evidence": sum((item.provider, item.evidence_id) in event_ids for item in query_only),
            "query_only_attribution_eligible": sum(item.attribution_eligible for item in query_only),
            "query_only_excluded_count": sum(not item.attribution_eligible for item in query_only),
            "pre_event_count": sum(item.temporal_relation == "pre_event" for item in provider_facts),
            "at_event_count": sum(item.temporal_relation == "at_event" for item in provider_facts),
            "post_event_count": sum(item.temporal_relation == "post_event" for item in provider_facts),
            "unknown_temporal_count": sum(item.temporal_relation == "unknown" for item in provider_facts),
            "post_event_attribution_eligible": sum(item.temporal_relation == "post_event" and item.attribution_eligible
                                                    for item in provider_facts),
            "post_event_excluded_count": sum(item.temporal_relation == "post_event" and not item.attribution_eligible
                                              for item in provider_facts),
            "timestamp_unknown_excluded_count": sum(item.temporal_relation == "unknown" and not item.attribution_eligible
                                                     for item in provider_facts),
            "strict_attribution_eligible_count": sum(item.strict_attribution_eligible for item in provider_facts),
        })
    report = {
        "target_date": "2026-08-31", "market_event_time": EVENT,
        "event_window": {"before_hours": 24, "after_hours": 12},
        "api_requests_executed": 0,
        "discovery_evidence_count": len(selection.discovery_evidence),
        "event_eligible_evidence_count": len(selection.event_evidence),
        "attribution_eligible_count": sum(item.attribution_eligible for item in attribution.attribution_facts),
        "provider_metrics": [asdict(item) for item in selection.provider_metrics],
        "provider_attribution_audit": provider_attribution,
        "strict_live_summary": {
            "strict_live_record_count": len(strict_records),
            "event_count": len(strict_events.event_evidence),
            "strict_historical_attribution_count": sum(item.strict_attribution_eligible for item in strict_facts),
            "post_event_context_count": sum(len(item.post_event_context) for item in strict_attribution.bundles),
            "candidates_with_evidence": len({item.query_symbol for item in strict_records}),
            "candidates_with_strict_historical_attribution": len({item.symbol for item in strict_facts if item.strict_attribution_eligible}),
            "query_only_attribution": sum(item.strict_attribution_eligible and item.entity_strength == "weak" for item in strict_facts),
            "post_event_attribution": sum(item.strict_attribution_eligible and item.temporal_relation == "post_event" for item in strict_facts),
        },
        "cross_provider_dedup_audit": asdict(selection.cross_provider_audit),
        "top20": rows,
        "candidate_ranking_modified": False, "fund_flow_modified": False,
        "system_health_affected": False,
    }
    target = DEFAULT_SETTINGS.project_root / "reports" / "evidence_quality_calibration_2026-08-31.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
