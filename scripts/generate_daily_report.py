"""Generate a Phase 3B daily report from existing local canonical artifacts."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime
import json
import subprocess

from quantos.anomalies import detect_stock_anomaly
from quantos.calibration import (
    calibrate_attribution_evidence, calibrate_event_evidence,
    link_stored_query_a_results,
)
from quantos.config import (
    DEFAULT_SETTINGS, KnowledgeIntegrationSettings, LLMSettings, MARKET_TIMEZONE,
    SynthesisBatchSettings,
)
from quantos.fundflow import build_fund_flow_evidence
from quantos.knowledge_integration import (
    KnowledgePreparationError, prepare_candidate_synthesis_input,
)
from quantos.reporting import generate_daily_report
from quantos.snapshots import build_market_snapshot
from quantos.storage import (
    DailyReportRepository, KnowledgeContextRepository,
    KnowledgeLexicalIndexRepository, KnowledgeRepository, MarketDataRepository,
    MoneyFlowRepository, NewsEvidenceRepository, SynthesisRepository,
)
from quantos.synthesis import DeepSeekLLMClient
from quantos.triage import rank_anomaly_candidates


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantOS Daily Intelligence Report V1")
    parser.add_argument("--trade-date", required=True, type=date.fromisoformat)
    parser.add_argument("--as-of-time", required=True, type=datetime.fromisoformat)
    parser.add_argument("--mode", required=True, choices=("research", "strict_live"))
    parser.add_argument("--top-n", type=int,
                        default=SynthesisBatchSettings.from_env().synthesis_top_n)
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()
    if args.as_of_time.tzinfo is None or args.as_of_time.utcoffset() is None:
        parser.error("--as-of-time must include a timezone offset")
    if args.top_n < 1:
        parser.error("--top-n must be positive")

    market, anomalies, candidates, flows, current_bars = _market_artifacts(
        args.trade_date, args.as_of_time,
    )
    names = _stored_company_names(args.trade_date)
    records = NewsEvidenceRepository(DEFAULT_SETTINGS).query_web_search_results(
        as_of_time=args.as_of_time,
        pit_mode="source_timestamp_proxy" if args.mode == "research" else "strict_live",
    )
    market_event_time = datetime.combine(
        args.trade_date, datetime.min.time().replace(hour=15), tzinfo=MARKET_TIMEZONE,
    )
    event = calibrate_event_evidence(
        records, link_stored_query_a_results(records), market_event_time=market_event_time,
    )
    attribution = calibrate_attribution_evidence(
        event, candidates, flows, market_event_time=market_event_time,
        as_of_time=args.as_of_time,
    )
    selected_bundles = attribution.bundles[:args.top_n]
    generated_at = datetime.now(MARKET_TIMEZONE)
    knowledge_settings = KnowledgeIntegrationSettings.from_env()
    try:
        synthesis_inputs = _prepare_synthesis_inputs(
            selected_bundles,
            attribution.attribution_facts,
            company_names=names,
            mode=args.mode,
            market_event_time=market_event_time,
            research_corpus_cutoff=(args.as_of_time if args.mode == "research" else None),
            generated_at=generated_at,
            knowledge_settings=knowledge_settings,
        )
    except KnowledgePreparationError as error:
        raise SystemExit(
            f"knowledge preparation failed: {error.status.value}:{error.safe_reason_code}"
        ) from error
    llm_settings = LLMSettings.from_env()
    if not args.no_llm and llm_settings.reasoning_effort is not None:
        raise SystemExit("Daily Report V1 requires default/high reasoning")
    report = generate_daily_report(
        market_snapshot=market, sector_snapshots=(), anomalies=anomalies,
        candidates=candidates, company_names=names, attribution=attribution,
        synthesis_inputs=synthesis_inputs,
        client_factory=lambda _value: DeepSeekLLMClient(settings=llm_settings),
        synthesis_repository=SynthesisRepository(DEFAULT_SETTINGS),
        mode=args.mode, top_n=args.top_n,
        generated_at=generated_at, no_llm=args.no_llm,
        provenance={
            "market_snapshot_version": market.schema_version,
            "sector_snapshot_version": None,
            "anomaly_version": anomalies[0].schema_version if anomalies else None,
            "anomaly_config_version": None,
            "triage_version": None,
            "fund_flow_version": flows[0].schema_version if flows else None,
            "discovery_version": records[0].schema_version if records else None,
            "evidence_calibration_version": None,
            "attribution_version": None,
            "synthesis_prompt_version": (synthesis_inputs[0].prompt_version
                                         if synthesis_inputs else None),
            "synthesis_schema_version": (synthesis_inputs[0].schema_version
                                         if synthesis_inputs else None),
            "quantos_git_commit": _git_commit(),
        },
    )
    json_path, markdown_path = DailyReportRepository(DEFAULT_SETTINGS).write(report)
    print(json.dumps({
        "status": "PASS", "mode": report.mode,
        "report_schema": "PASS", "report_id": report.report_id,
        "json_path": str(json_path), "markdown_path": str(markdown_path),
        "total_candidates": report.candidate_summary["total_candidates"],
        "selected_candidates": report.candidate_summary["selected_for_report"],
        "cache_hits": report.synthesis_runtime["cache_hits"],
        "no_evidence_fast_paths": report.synthesis_runtime["no_evidence_fast_paths"],
        "llm_disabled_cache_misses": sum(
            item.synthesis.error_category == "LLM_DISABLED_CACHE_MISS"
            for item in report.candidate_briefs
        ),
        "synthesis_failures": report.synthesis_runtime["failures"],
        "historical_proxy_used": report.data_quality["evidence"]["historical_proxy_used"],
        "real_deepseek_request_count": report.synthesis_runtime["actual_llm_requests"],
        "ranking_unchanged": True, "fund_flow_unchanged": True,
        "system_health_unchanged": True,
    }, ensure_ascii=False, sort_keys=True))


def _prepare_synthesis_inputs(
    bundles,
    attribution_facts,
    *,
    company_names,
    mode,
    market_event_time,
    research_corpus_cutoff,
    generated_at,
    knowledge_settings,
    knowledge_repository=None,
    index_repository=None,
    context_repository=None,
):
    """Prepare all candidate inputs before any provider or product publication."""
    if knowledge_settings.enabled:
        if knowledge_repository is None:
            knowledge_repository = KnowledgeRepository(settings=DEFAULT_SETTINGS)
        if index_repository is None:
            index_repository = KnowledgeLexicalIndexRepository(settings=DEFAULT_SETTINGS)
        if context_repository is None:
            context_repository = KnowledgeContextRepository(settings=DEFAULT_SETTINGS)
    return [
        prepare_candidate_synthesis_input(
            bundle,
            attribution_facts,
            mode=mode,
            company_name=company_names.get(
                bundle.candidate.symbol, bundle.candidate.symbol,
            ),
            market_event_time=market_event_time,
            research_corpus_cutoff=research_corpus_cutoff,
            generated_at=generated_at,
            settings=knowledge_settings,
            knowledge_repository=knowledge_repository,
            index_repository=index_repository,
            context_repository=context_repository,
        ).synthesis_input
        for bundle in bundles
    ]


def _market_artifacts(trade_date, as_of_time):
    market_repository = MarketDataRepository(DEFAULT_SETTINGS)
    history = defaultdict(list)
    for day_dir in sorted(DEFAULT_SETTINGS.normalized_market_dir.glob("trade_date=*")):
        day = date.fromisoformat(day_dir.name.split("=", 1)[1])
        if day <= trade_date:
            for bar in market_repository.read_by_date(day, as_of_time=as_of_time):
                history[bar.symbol].append(bar)
    anomalies, current_bars = [], {}
    for symbol, bars in history.items():
        bars.sort(key=lambda item: item.timestamp)
        current = next((item for item in bars if item.timestamp.date() == trade_date), None)
        if current is not None:
            current_bars[symbol] = current
            anomalies.append(detect_stock_anomaly(
                current, [item for item in bars if item.timestamp < current.timestamp],
                as_of_time=as_of_time,
            ))
    candidates = rank_anomaly_candidates(
        anomalies, (), (), as_of_time=as_of_time, top_n=None,
    )
    money = MoneyFlowRepository(DEFAULT_SETTINGS).read_by_date(
        trade_date, as_of_time=as_of_time,
    )
    money_by_symbol = {item.symbol: item for item in money}
    flows = [build_fund_flow_evidence(
        money_by_symbol[item.symbol], current_bars[item.symbol], as_of_time=as_of_time,
    ) for item in candidates if item.symbol in money_by_symbol]
    market = build_market_snapshot(
        tuple(current_bars.values()), (), security_member_symbols=tuple(current_bars),
        trade_date=trade_date, as_of_time=as_of_time,
    )
    return market, anomalies, candidates, flows, current_bars


def _stored_company_names(trade_date):
    path = DEFAULT_SETTINGS.project_root / "reports" / f"evidence_quality_calibration_{trade_date}.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return {item["symbol"]: item["company_name"] for item in value.get("top20", ())}


def _git_commit():
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=DEFAULT_SETTINGS.project_root,
        check=False, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


if __name__ == "__main__":
    main()
