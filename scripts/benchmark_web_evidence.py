"""Real Tavily/Exa A/B benchmark for the 2026-08-31 Top20 anomalies.

Credentials are read only by provider adapters from environment variables. This
command emits no headers, payloads, environment values, or exception messages.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from quantos.anomalies import detect_stock_anomaly
from quantos.collectors import BaoStockSecurityMasterCollector, ExaWebSearchProvider, TavilyWebSearchProvider
from quantos.config import DEFAULT_SETTINGS, MARKET_TIMEZONE
from quantos.discovery import (
    ProviderPayload, RegisteredEvidenceProvider, build_provider_benchmark_reports,
    discover_candidate_evidence, web_search_runner,
)
from quantos.fundflow import build_fund_flow_evidence
from quantos.schemas import EvidenceDiscoveryRequest
from quantos.storage import MarketDataRepository, MoneyFlowRepository, NewsEvidenceRepository
from quantos.triage import rank_anomaly_candidates

TARGET_DATE = date(2026, 8, 31)
MARKET_EVENT_TIME = datetime(2026, 8, 31, 15, tzinfo=MARKET_TIMEZONE)


def main() -> None:
    security_request_time = datetime.now(MARKET_TIMEZONE)
    securities = BaoStockSecurityMasterCollector().fetch_security_master(as_of_time=security_request_time)
    as_of_time = datetime.now(MARKET_TIMEZONE)
    candidates, flows = _load_candidates(as_of_time)
    master = {item.symbol: item for item in securities if item.available_at <= as_of_time}
    selected = [item for item in candidates if item.symbol in master][:20]
    if len(selected) != 20:
        raise RuntimeError("SecurityMaster coverage is insufficient for Top20")
    names = tuple(master[item.symbol].company_name for item in selected)
    aliases = tuple(master[item.symbol].aliases for item in selected)
    tavily, exa = TavilyWebSearchProvider(), ExaWebSearchProvider()
    providers = {"tavily": tavily, "exa": exa}
    aggregates = {name: [] for name in providers}
    timings = {name: 0 for name in providers}
    stages = (("A", selected[:1]), ("B", selected[1:3]), ("C", selected[3:20]))
    stage_status = {}
    for stage_name, stage_candidates in stages:
        if not stage_candidates:
            continue
        indices = [selected.index(item) for item in stage_candidates]
        request = _request(stage_candidates, names, aliases, indices, as_of_time)
        current = {}
        for provider_name, provider in providers.items():
            started = time.monotonic_ns()
            payload = web_search_runner(provider, provider_name)(request)
            timings[provider_name] += (time.monotonic_ns() - started) // 1_000_000
            aggregates[provider_name].append(payload)
            current[provider_name] = "PASS" if payload.successful_symbols else "UNAVAILABLE"
        stage_status[stage_name] = current
        if stage_name in {"A", "B"} and not all(value == "PASS" for value in current.values()):
            break
    combined = {name: _combine(parts) for name, parts in aggregates.items()}
    # Freeze the PIT cutoff after all real responses have received collected_at.
    benchmark_as_of_time = datetime.now(MARKET_TIMEZONE)
    full_request = _request(selected, names, aliases, list(range(20)), benchmark_as_of_time)
    output = discover_candidate_evidence(
        full_request, selected, flows,
        [RegisteredEvidenceProvider(name, lambda _, value=value: value) for name, value in combined.items()],
        max_provider_workers=2,
    )
    repository = NewsEvidenceRepository(DEFAULT_SETTINGS)
    repository.write_web_search_results(output.search_results)
    reports = build_provider_benchmark_reports(output)
    report = _serialize(output, reports, selected, names, flows, stage_status, timings)
    target = DEFAULT_SETTINGS.project_root / "reports" / "web_evidence_ab_2026-08-31.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def _load_candidates(as_of_time):
    market = MarketDataRepository(DEFAULT_SETTINGS)
    history_by_symbol = defaultdict(list)
    for day_dir in sorted(DEFAULT_SETTINGS.normalized_market_dir.glob("trade_date=*")):
        trade_date = date.fromisoformat(day_dir.name.split("=", 1)[1])
        if trade_date <= TARGET_DATE:
            for bar in market.read_by_date(trade_date, as_of_time=as_of_time):
                history_by_symbol[bar.symbol].append(bar)
    anomalies = []
    current_bars = {}
    for symbol, bars in history_by_symbol.items():
        bars.sort(key=lambda item: item.timestamp)
        current = next((item for item in bars if item.timestamp.date() == TARGET_DATE), None)
        if current is not None:
            current_bars[symbol] = current
            anomalies.append(detect_stock_anomaly(current, [item for item in bars if item.timestamp < current.timestamp], as_of_time=as_of_time))
    candidates = rank_anomaly_candidates(anomalies, [], [], as_of_time=as_of_time, top_n=20)
    money = MoneyFlowRepository(DEFAULT_SETTINGS).read_by_date(TARGET_DATE, as_of_time=as_of_time)
    money_by_symbol = {item.symbol: item for item in money}
    flows = [build_fund_flow_evidence(money_by_symbol[item.symbol], current_bars[item.symbol], as_of_time=as_of_time)
             for item in candidates if item.symbol in money_by_symbol]
    return candidates, flows


def _request(candidates, names, aliases, indices, as_of_time):
    return EvidenceDiscoveryRequest(
        target_trade_date=TARGET_DATE, market_event_time=MARKET_EVENT_TIME,
        as_of_time=as_of_time, symbols=tuple(item.symbol for item in candidates),
        company_names=tuple(names[index] for index in indices),
        company_aliases=tuple(aliases[index] for index in indices),
        before_hours=24, after_hours=12, top_n=20, mode="proxy_research",
        max_results_per_candidate=5,
    )


def _combine(parts):
    records = {item.result_id: item for part in parts for item in part.records}
    mentions = {(item.evidence_id, item.symbol, item.provider, item.match_method): item
                for part in parts for item in part.mentions}
    return ProviderPayload(
        tuple(sorted(records.values(), key=lambda item: item.result_id)),
        tuple(sorted(mentions.values(), key=lambda item: (item.symbol, item.evidence_id, item.match_method))),
        sum(item.raw_result_count for item in parts),
        tuple(sorted({symbol for item in parts for symbol in item.successful_symbols})),
        tuple(sorted({symbol for item in parts for symbol in item.failed_symbols})),
        sum(item.request_count for item in parts),
    )


def _serialize(output, reports, candidates, names, flows, stages, timings):
    flow_map = {item.symbol: item for item in flows}
    mention_map = defaultdict(list)
    for result in output.provider_results:
        for mention in result.mentions:
            mention_map[(mention.symbol, mention.evidence_id)].append(mention)
    rows = []
    for candidate, name, bundle in zip(candidates, names, output.bundles):
        evidence = []
        for item in bundle.news_evidence[:3]:
            canonical_native_ids = set(item.source_record_ids)
            result_ids = {search.result_id for search in bundle.search_evidence
                          if search.provider_record_id in canonical_native_ids}
            source_mentions = [m for key, values in mention_map.items() if key[0] == candidate.symbol
                               for m in values if m.evidence_id in result_ids]
            best = min(source_mentions, key=lambda m: (m.evidence_tier, m.match_method)) if source_mentions else None
            evidence.append({"discovered_by": item.discovered_by, "published_at": item.published_at,
                             "timestamp_basis": item.timestamp_basis, "domain": item.domain,
                             "evidence_tier": best.evidence_tier if best else None,
                             "match_method": best.match_method if best else None, "title": item.title,
                             "url": item.url, "pit_mode": item.pit_mode})
        providers = [set(item.discovered_by) for item in bundle.news_evidence]
        flow = flow_map.get(candidate.symbol)
        rows.append({"rank": candidate.rank, "symbol": candidate.symbol, "company_name": name,
                     "return_pct": candidate.return_pct, "return_zscore": candidate.return_zscore,
                     "net_mf_amount": flow.net_mf_amount if flow else None,
                     "net_mf_ratio": flow.net_mf_ratio if flow else None,
                     "tavily_count": sum("tavily" in item for item in providers),
                     "exa_count": sum("exa" in item for item in providers),
                     "shared_count": sum(item == {"tavily", "exa"} for item in providers),
                     "total_unique_evidence": len(bundle.news_evidence), "evidence": evidence})
    tavily_candidates = {m.symbol for r in output.provider_results if r.provider_name == "tavily" for m in r.mentions}
    exa_candidates = {m.symbol for r in output.provider_results if r.provider_name == "exa" for m in r.mentions}
    shared = [item for item in output.canonical_news if set(item.discovered_by) == {"tavily", "exa"}]
    return {"target_date": TARGET_DATE, "market_event_time": MARKET_EVENT_TIME,
            "window": {"start": datetime(2026, 8, 30, 15, tzinfo=MARKET_TIMEZONE),
                       "end": datetime(2026, 9, 1, 3, tzinfo=MARKET_TIMEZONE)},
            "pit_mode": "source_timestamp_proxy", "strict_pit": False,
            "stages": stages,
            "capabilities": {item.provider_name: ("PASS" if item.successful_candidates == item.requested_candidates
                                                   else "PARTIAL" if item.successful_candidates else "UNAVAILABLE")
                             for item in reports},
            "provider_reports": [{**asdict(item), "duration_ms": timings[item.provider_name]}
                                 for item in reports],
            "request_duration_ms": timings,
            "failure_matrix": {item.provider_name: {"status": ("PASS" if item.successful_candidates == item.requested_candidates
                                                               else "PARTIAL" if item.successful_candidates else "UNAVAILABLE"),
                                                           "failed_candidates": item.requested_candidates - item.successful_candidates}
                               for item in reports},
            "comparison": {"ONLY_TAVILY": sorted(tavily_candidates - exa_candidates),
                           "ONLY_EXA": sorted(exa_candidates - tavily_candidates),
                           "BOTH_PROVIDERS": sorted(tavily_candidates & exa_candidates),
                           "UNIQUE_TAVILY_EVIDENCE": sum(item.discovered_by == ("tavily",) for item in output.canonical_news),
                           "UNIQUE_EXA_EVIDENCE": sum(item.discovered_by == ("exa",) for item in output.canonical_news),
                           "SHARED_CANONICAL_EVIDENCE": len(shared)},
            "top20": rows, "candidate_ranking_modified": False,
            "fund_flow_modified": False, "system_health_affected": False}


if __name__ == "__main__":
    main()
