"""Pure Phase 3B assembly and deterministic rendering of canonical artifacts."""

from __future__ import annotations

from dataclasses import asdict, fields
from datetime import date, datetime
from decimal import Decimal
import hashlib
from typing import Any, Mapping, Sequence

from quantos.calibration import AttributionEvidenceSelection
from quantos.schemas import (
    AnomalyCandidate, AnomalyStatus, CandidateBrief, DailyIntelligenceReport,
    EvidenceStats, KnowledgeBackgroundStatement, MarketSnapshot, SectorSnapshot,
    StockAnomaly, SynthesisBrief,
)
from quantos.schemas.report import (
    KNOWLEDGE_REPORT_SCHEMA_VERSION, REPORT_SCHEMA_VERSION,
)
from quantos.serialization import canonical_identity_json_bytes, canonical_json_bytes
from quantos.synthesis_runtime import (
    CandidateSynthesisResult, SynthesisBatchReport, SynthesisCacheKey,
    synthesize_batch,
)
from quantos.synthesis import synthesize_evidence, validate_synthesis_input
from quantos.storage.synthesis import SynthesisRepository
from quantos.config import SynthesisBatchSettings

RESEARCH_DISCLAIMER = (
    "历史研究模式说明：\n"
    "本报告可能使用 source timestamp proxy 进行历史证据研究。\n"
    "该模式用于回溯分析，不代表相关信息在目标市场时点真实可获得。\n"
    "严格时点判断请使用 strict_live 模式。"
)
STRICT_DISCLAIMER = (
    "严格实时模式说明：本报告只使用目标市场时点可知的 strict PIT 证据；"
    "严格时点下暂无可归因证据时，不调用 LLM。"
)
PROVENANCE_FIELDS = (
    "market_snapshot_version", "sector_snapshot_version", "anomaly_version",
    "anomaly_config_version", "triage_version", "fund_flow_version",
    "discovery_version", "evidence_calibration_version", "attribution_version",
    "synthesis_prompt_version", "synthesis_schema_version",
    "report_schema_version", "quantos_git_commit",
)


def generate_daily_report(
    *, market_snapshot: MarketSnapshot,
    sector_snapshots: Sequence[SectorSnapshot],
    anomalies: Sequence[StockAnomaly],
    candidates: Sequence[AnomalyCandidate],
    company_names: Mapping[str, str],
    attribution: AttributionEvidenceSelection,
    synthesis_inputs: Sequence[Any],
    client_factory: Any,
    synthesis_repository: SynthesisRepository,
    mode: str,
    top_n: int,
    generated_at: datetime,
    no_llm: bool = False,
    provider_failures: Sequence[str] = (),
    provenance: Mapping[str, str | None] | None = None,
    model_provider: str = "deepseek",
    model_name: str = "deepseek-v4-flash",
    reasoning_config: str = "default/high",
    max_concurrency: int = 2,
) -> DailyIntelligenceReport:
    """Run the existing synthesis runtime, then assemble the report only."""

    runtime = (_no_llm_batch(
        synthesis_inputs, synthesis_repository, model_provider=model_provider,
        model_name=model_name, reasoning_config=reasoning_config,
    ) if no_llm else synthesize_batch(
        synthesis_inputs, client_factory=client_factory,
        repository=synthesis_repository, model_provider=model_provider,
        model_name=model_name, reasoning_config=reasoning_config,
        settings=SynthesisBatchSettings(top_n, max_concurrency),
    ))
    input_map = {(item.symbol, item.candidate.rank): item for item in synthesis_inputs}
    return assemble_daily_report(
        market_snapshot=market_snapshot, sector_snapshots=sector_snapshots,
        anomalies=anomalies, candidates=candidates, company_names=company_names,
        attribution=attribution, synthesis_inputs=input_map,
        synthesis_report=runtime, mode=mode, top_n=top_n,
        generated_at=generated_at, provider_failures=provider_failures,
        provenance=provenance, model_provider=model_provider,
        model_name=model_name, reasoning_config=reasoning_config,
    )


def assemble_daily_report(
    *, market_snapshot: MarketSnapshot,
    sector_snapshots: Sequence[SectorSnapshot],
    anomalies: Sequence[StockAnomaly],
    candidates: Sequence[AnomalyCandidate],
    company_names: Mapping[str, str],
    attribution: AttributionEvidenceSelection,
    synthesis_inputs: Mapping[tuple[str, int], Any],
    synthesis_report: SynthesisBatchReport,
    mode: str,
    top_n: int,
    generated_at: datetime,
    provider_failures: Sequence[str] = (),
    provenance: Mapping[str, str | None] | None = None,
    model_provider: str = "deepseek",
    model_name: str = "deepseek-v4-flash",
    reasoning_config: str = "default/high",
) -> DailyIntelligenceReport:
    """Combine existing facts without recomputing any upstream conclusion."""

    if mode not in {"research", "strict_live"} or top_n < 1:
        raise ValueError("report mode or top_n is invalid")
    _validate_inputs(market_snapshot, sector_snapshots, anomalies, candidates, attribution, mode)
    selected = list(candidates[:top_n])
    if [item.rank for item in candidates] != list(range(1, len(candidates) + 1)):
        raise ValueError("candidate ranking must be supplied in canonical rank order")

    bundle_map = {item.candidate.symbol: item for item in attribution.bundles}
    result_map = {(item.symbol, item.rank): item for item in synthesis_report.results}
    anomaly_map = {item.symbol: item for item in anomalies}
    candidate_briefs = tuple(
        _candidate_brief(
            candidate, company_names.get(candidate.symbol, candidate.symbol),
            anomaly_map.get(candidate.symbol), bundle_map.get(candidate.symbol),
            attribution, result_map.get((candidate.symbol, candidate.rank)),
            synthesis_inputs.get((candidate.symbol, candidate.rank)), mode,
            model_provider, model_name, reasoning_config,
        )
        for candidate in selected
    )
    sector_by_code = {item.sector_code: item for item in sector_snapshots}
    complete_sectors = sum(item.coverage_ratio == Decimal("1") for item in sector_snapshots)
    incomplete_sectors = len(sector_snapshots) - complete_sectors
    market_overview = {
        "security_member_count": market_snapshot.security_member_count,
        "valid_bar_count": market_snapshot.valid_bar_count,
        "coverage_ratio": market_snapshot.coverage_ratio,
        "advancer_count": market_snapshot.advancer_count,
        "decliner_count": market_snapshot.decliner_count,
        "flat_count": market_snapshot.flat_count,
        "total_volume": market_snapshot.total_volume,
        "total_amount": market_snapshot.total_amount,
    }
    sector_overview = {
        "classification_system": "证监会行业分类",
        "sector_count": len(sector_snapshots),
        "complete_sector_count": complete_sectors,
        "incomplete_sector_count": incomplete_sectors,
        "top_sectors": tuple(_sector_brief(sector_by_code.get(code), code)
                             for code in market_snapshot.top_sector_codes),
        "bottom_sectors": tuple(_sector_brief(sector_by_code.get(code), code)
                                for code in market_snapshot.bottom_sector_codes),
    }
    tier_distribution = {str(tier): sum(item.priority_tier == tier for item in candidates)
                         for tier in range(1, 6)}
    anomaly_overview = {
        "stocks_scanned": len(anomalies),
        "stocks_analyzed": sum(item.status == AnomalyStatus.OK for item in anomalies),
        "insufficient_history": sum(
            item.status == AnomalyStatus.INSUFFICIENT_HISTORY for item in anomalies
        ),
        "price_anomaly_count": sum(item.price_anomaly for item in anomalies),
        "volume_anomaly_count": sum(item.volume_anomaly for item in anomalies),
        "amount_anomaly_count": sum(item.amount_anomaly for item in anomalies),
        "candidate_count": len(candidates),
        "tier_distribution": tier_distribution,
    }
    candidate_summary = {
        "total_candidates": len(candidates),
        "selected_for_report": len(selected),
        "not_selected": len(candidates) - len(selected),
        "requested_top_n": top_n,
    }
    discovery = attribution.event_selection.discovery_evidence
    event_evidence = attribution.event_selection.event_evidence
    research_count = sum(len(item.research_attribution_evidence) for item in attribution.bundles)
    strict_count = sum(len(item.strict_attribution_evidence) for item in attribution.bundles)
    post_count = sum(len(item.post_event_context) for item in attribution.bundles)
    providers = tuple({
        "provider": provider,
        "record_count": sum(item.provider == provider for item in discovery),
        "candidate_coverage": len({item.query_symbol for item in discovery
                                   if item.provider == provider}),
    } for provider in sorted({item.provider for item in discovery}))
    evidence_overview = {
        "discovery_evidence_count": len(discovery),
        "event_evidence_count": len(event_evidence),
        "research_attribution_count": research_count,
        "strict_attribution_count": strict_count,
        "post_event_context_count": post_count,
        "unknown_timestamp_count": sum(item.published_at is None for item in discovery),
        "providers": providers,
    }
    eligible_records = [
        record for bundle in attribution.bundles
        for record in (bundle.research_attribution_evidence if mode == "research"
                       else bundle.strict_attribution_evidence)
    ]
    historical_proxy_used = bool(
        mode == "research"
        and any(item.pit_mode == "source_timestamp_proxy" for item in eligible_records)
    )
    covered_fund_flow = sum(
        bundle_map.get(item.symbol) is not None
        and bundle_map[item.symbol].fund_flow_evidence is not None
        for item in selected
    )
    data_quality = {
        "market": {"coverage": market_snapshot.coverage_ratio,
                   "missing_bars": market_snapshot.missing_bar_count},
        "sector": {"complete_sectors": complete_sectors,
                   "incomplete_sectors": incomplete_sectors},
        "fund_flow": {
            "covered_candidates": covered_fund_flow,
            "target_candidates": len(selected),
            "coverage": (Decimal(covered_fund_flow) / Decimal(len(selected))
                         if selected else None),
        },
        "evidence": {
            "mode": mode, "provider_failures": tuple(provider_failures),
            "strict_pit_only": mode == "strict_live",
            "historical_proxy_used": historical_proxy_used,
        },
    }
    runtime = {
        "selected_candidates": len(selected),
        "cache_hits": synthesis_report.cache_hits,
        "cache_misses": synthesis_report.cache_misses,
        "generated": sum(item.status == "PASS" for item in synthesis_report.results),
        "no_evidence_fast_paths": synthesis_report.no_evidence_fast_path_count,
        "failures": sum(item.status == "FAIL" for item in synthesis_report.results),
        "planned_llm_requests": synthesis_report.planned_llm_request_count,
        "actual_llm_requests": synthesis_report.actual_llm_request_count,
        "requests_avoided_by_cache": synthesis_report.requests_avoided_by_cache,
        "requests_avoided_by_no_evidence": synthesis_report.requests_avoided_by_no_evidence,
        "total_requests_avoided": synthesis_report.total_requests_avoided,
        "input_tokens": synthesis_report.total_input_tokens,
        "cached_input_tokens": synthesis_report.total_cached_input_tokens,
        "output_tokens": synthesis_report.total_output_tokens,
        "reasoning_tokens": synthesis_report.total_reasoning_tokens,
        "total_tokens": synthesis_report.total_tokens,
        "wall_clock_duration_ms": synthesis_report.wall_clock_duration_ms,
    }
    provenance_values = {field: None for field in PROVENANCE_FIELDS}
    provenance_values.update(provenance or {})
    knowledge_enabled = any(
        value is not None and value.knowledge_context is not None
        for value in synthesis_inputs.values()
    )
    report_schema_version = (
        KNOWLEDGE_REPORT_SCHEMA_VERSION if knowledge_enabled else REPORT_SCHEMA_VERSION
    )
    provenance_values["report_schema_version"] = report_schema_version
    if knowledge_enabled:
        identity = _knowledge_report_identity_value(
            trade_date=market_snapshot.trade_date,
            as_of_time=market_snapshot.as_of_time,
            mode=mode,
            universe_name=market_snapshot.universe,
            market_overview=market_overview,
            sector_overview=sector_overview,
            anomaly_overview=anomaly_overview,
            candidate_summary=candidate_summary,
            candidate_briefs=candidate_briefs,
            evidence_overview=evidence_overview,
            data_quality=data_quality,
            provenance=provenance_values,
        )
    else:
        identity = {
            "trade_date": market_snapshot.trade_date,
            "as_of_time": market_snapshot.as_of_time,
            "mode": mode,
            "universe_name": market_snapshot.universe,
            "top_n": top_n,
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "candidate_ranking": tuple((item.rank, item.symbol) for item in candidates),
            "triage_version": provenance_values["triage_version"],
            "attribution_version": provenance_values["attribution_version"],
            "synthesis_prompt_version": provenance_values["synthesis_prompt_version"],
            "market_overview": market_overview,
            "sector_overview": sector_overview,
            "anomaly_overview": anomaly_overview,
            "candidate_summary": candidate_summary,
            "candidate_briefs": tuple(
                _candidate_identity_value(item, include_synthesis=False)
                for item in candidate_briefs
            ),
            "evidence_overview": evidence_overview,
            "data_quality": data_quality,
        }
    report_id = hashlib.sha256(canonical_identity_json_bytes(identity)).hexdigest()
    return DailyIntelligenceReport(
        report_schema_version, report_id, market_snapshot.trade_date,
        market_snapshot.as_of_time, generated_at, mode, market_snapshot.universe,
        market_overview, sector_overview, anomaly_overview, candidate_summary,
        candidate_briefs, evidence_overview, data_quality, runtime, provenance_values,
    )


def _candidate_brief(candidate, name, anomaly, bundle, attribution, runtime_result,
                     synthesis_input, mode, provider, model, reasoning):
    discovery_count = sum(item.query_symbol == candidate.symbol
                          for item in attribution.event_selection.discovery_evidence)
    event_count = sum(item.query_symbol == candidate.symbol
                      for item in attribution.event_selection.event_evidence)
    research_count = len(bundle.research_attribution_evidence) if bundle else 0
    strict_count = len(bundle.strict_attribution_evidence) if bundle else 0
    post_count = len(bundle.post_event_context) if bundle else 0
    stats = EvidenceStats(
        discovery_count, event_count, research_count, strict_count, post_count,
        research_count if mode == "research" else strict_count,
    )
    market_facts = {
        "return_pct": candidate.return_pct,
        "return_zscore": candidate.return_zscore,
        "volume_ratio": candidate.volume_ratio,
        "amount_ratio": candidate.amount_ratio,
        "excess_return_pct": candidate.excess_return_pct,
    }
    if anomaly is not None:
        market_facts.update({"volume": anomaly.volume, "amount": anomaly.amount})
    sector_context = None if candidate.sector_name is None else {
        "available": True, "sector_name": candidate.sector_name,
        "classification_system": "证监会行业分类",
        "sector_metric": candidate.sector_return_pct, "sector_rank": None,
    }
    flow = bundle.fund_flow_evidence if bundle else None
    fund_flow = ({"available": False, "net_mf_amount": None,
                  "large_plus_extra_net_amount": None, "flow_direction": "unknown"}
                 if flow is None else
                 {"available": True, "net_mf_amount": flow.net_mf_amount,
                  "large_plus_extra_net_amount": flow.large_plus_extra_net_amount,
                  "flow_direction": flow.flow_direction})
    synthesis = _synthesis_brief(
        runtime_result, synthesis_input, provider, model, reasoning,
    )
    return CandidateBrief(
        candidate.rank, candidate.symbol, name, candidate.priority_tier,
        (candidate.signal_type,), market_facts, sector_context, fund_flow, stats, synthesis,
    )


def _synthesis_brief(result, synthesis_input, provider, model, reasoning):
    if result is None or result.status in {"FAIL", "NOT_SELECTED"}:
        context = synthesis_input.knowledge_context if synthesis_input else None
        return SynthesisBrief(
            "FAIL", None, None,
            synthesis_input.input_bundle_id if synthesis_input else None,
            _synthesis_cache_identity(
                synthesis_input, provider, model, reasoning,
            ),
            None, (), (), (), (), (),
            ("LLM_DISABLED_CACHE_MISS" if result and
             result.safe_error_type == "LLM_DISABLED_CACHE_MISS" else
             "SYNTHESIS_RESULT_MISSING" if result is None else None),
            result.validation_error_code if result else None,
            supplied_context_id=context.context_id if context else None,
            supplied_knowledge_refs=_context_knowledge_refs(context),
        )
    output = result.output
    if output is None:
        context = synthesis_input.knowledge_context if synthesis_input else None
        return SynthesisBrief(
            "FAIL", None, None,
            synthesis_input.input_bundle_id if synthesis_input else None,
            _synthesis_cache_identity(
                synthesis_input, provider, model, reasoning,
            ),
            None, (), (), (), (), (),
            "SYNTHESIS_OUTPUT_MISSING", None,
            supplied_context_id=context.context_id if context else None,
            supplied_knowledge_refs=_context_knowledge_refs(context),
        )
    status = {"PASS": "GENERATED", "CACHE_HIT": "CACHE_HIT",
              "NO_EVIDENCE_FAST_PATH": "NO_EVIDENCE_FAST_PATH"}[result.status]
    cache_identity = _synthesis_cache_identity(
        synthesis_input, provider, model, reasoning,
    )
    explanations = tuple({
        "statement": item.statement,
        "supporting_evidence_ids": item.supporting_evidence_ids,
        "limitations": item.limitations,
        "causal_status": item.causal_status,
    } for item in output.possible_explanations)
    return SynthesisBrief(
        status, output.model_provider, output.model_name, output.input_bundle_id,
        cache_identity, output.insufficient_evidence, output.used_evidence_ids,
        explanations, output.contradicting_signals, output.post_event_notes,
        output.limitations, None, None,
        output.knowledge_background, output.retrospective_knowledge_context,
        output.supplied_context_id, output.supplied_knowledge_refs,
    )


def _context_knowledge_refs(context):
    if context is None:
        return ()
    return tuple(f"K{item.retrieval_rank}" for item in context.selected_items)


def _synthesis_cache_identity(synthesis_input, provider, model, reasoning):
    if synthesis_input is None or not (
        synthesis_input.attribution_evidence
        or synthesis_input.knowledge_context is not None
    ):
        return None
    return SynthesisCacheKey.for_input(
        synthesis_input,
        model_provider=provider,
        model_name=model,
        reasoning_config=reasoning,
    ).identity


def _candidate_identity_value(item, *, include_synthesis):
    value = {
        "rank": item.rank, "symbol": item.symbol, "name": item.name,
        "tier": item.tier, "signal_types": item.signal_types,
        "market_facts": item.market_facts,
        "sector_context": item.sector_context,
        "fund_flow": item.fund_flow,
        "evidence_stats": asdict(item.evidence_stats),
    }
    if include_synthesis:
        synthesis = asdict(item.synthesis)
        if synthesis["status"] in {"GENERATED", "CACHE_HIT"}:
            synthesis["status"] = "SUCCESS"
        value["synthesis"] = synthesis
    return value


def _knowledge_report_identity_value(
    *, trade_date, as_of_time, mode, universe_name, market_overview,
    sector_overview, anomaly_overview, candidate_summary, candidate_briefs,
    evidence_overview, data_quality, provenance,
):
    return {
        "report_schema_version": KNOWLEDGE_REPORT_SCHEMA_VERSION,
        "trade_date": trade_date,
        "as_of_time": as_of_time,
        "mode": mode,
        "universe_name": universe_name,
        "market_overview": market_overview,
        "sector_overview": sector_overview,
        "anomaly_overview": anomaly_overview,
        "candidate_summary": candidate_summary,
        "candidate_briefs": tuple(
            _candidate_identity_value(item, include_synthesis=True)
            for item in candidate_briefs
        ),
        "evidence_overview": evidence_overview,
        "data_quality": data_quality,
        "provenance": provenance,
    }


def validate_daily_report_identity(report: DailyIntelligenceReport) -> None:
    """Fail closed for Knowledge-aware products without rewriting legacy IDs."""
    if report.schema_version != KNOWLEDGE_REPORT_SCHEMA_VERSION:
        return
    identity = _knowledge_report_identity_value(
        trade_date=report.trade_date,
        as_of_time=report.as_of_time,
        mode=report.mode,
        universe_name=report.universe_name,
        market_overview=report.market_overview,
        sector_overview=report.sector_overview,
        anomaly_overview=report.anomaly_overview,
        candidate_summary=report.candidate_summary,
        candidate_briefs=report.candidate_briefs,
        evidence_overview=report.evidence_overview,
        data_quality=report.data_quality,
        provenance=report.provenance,
    )
    expected = hashlib.sha256(canonical_identity_json_bytes(identity)).hexdigest()
    if report.report_id != expected:
        raise ValueError("Knowledge-aware daily report identity mismatch")


def _no_llm_batch(inputs, repository, *, model_provider, model_name, reasoning_config):
    results = []
    cache_hits = misses = fast_paths = 0
    for value in inputs:
        validate_synthesis_input(value)
        has_selected_knowledge = (
            value.knowledge_context is not None
            and value.knowledge_context.selected_item_count > 0
        )
        if not value.attribution_evidence and not has_selected_knowledge:
            output = synthesize_evidence(value, _NoProviderClient())
            results.append(CandidateSynthesisResult(
                value.symbol, value.candidate.rank, "NO_EVIDENCE_FAST_PATH", output,
            ))
            fast_paths += 1
            continue
        key = SynthesisCacheKey.for_input(
            value, model_provider=model_provider, model_name=model_name,
            reasoning_config=reasoning_config,
        )
        cached = repository.find_valid(key.as_dict())
        if cached is not None:
            results.append(CandidateSynthesisResult(
                value.symbol, value.candidate.rank, "CACHE_HIT", cached,
            ))
            cache_hits += 1
        else:
            results.append(CandidateSynthesisResult(
                value.symbol, value.candidate.rank, "FAIL",
                safe_error_type="LLM_DISABLED_CACHE_MISS",
            ))
            misses += 1
    considered = cache_hits + misses
    return SynthesisBatchReport(
        results=tuple(results), candidate_count=len(inputs),
        selected_candidate_count=len(inputs), planned_llm_request_count=misses,
        actual_llm_request_count=0, cache_hits=cache_hits, cache_misses=misses,
        no_evidence_fast_path_count=fast_paths,
        requests_avoided_by_cache=cache_hits,
        requests_avoided_by_no_evidence=fast_paths,
        total_requests_avoided=cache_hits + fast_paths,
        cache_hit_ratio=cache_hits / considered if considered else 0.0,
        total_input_tokens=0, total_cached_input_tokens=0,
        total_output_tokens=0, total_reasoning_tokens=0, total_tokens=0,
        total_llm_duration_ms=0, wall_clock_duration_ms=0,
        max_concurrency_observed=0, duplicate_generation_prevented=0,
    )


class _NoProviderClient:
    def generate_structured(self, **_kwargs):
        raise AssertionError("no-evidence report path attempted provider generation")


def _sector_brief(snapshot, code):
    if snapshot is None:
        return {"sector_code": code, "sector_name": "unavailable", "member_count": 0,
                "valid_bar_count": 0, "coverage_ratio": None,
                "ranking_metric_name": "equal_weight_return_pct",
                "ranking_metric_value": None}
    return {"sector_code": snapshot.sector_code, "sector_name": snapshot.sector_name,
            "member_count": snapshot.member_count, "valid_bar_count": snapshot.valid_bar_count,
            "coverage_ratio": snapshot.coverage_ratio,
            "ranking_metric_name": "equal_weight_return_pct",
            "ranking_metric_value": snapshot.equal_weight_return_pct}


def _validate_inputs(market, sectors, anomalies, candidates, attribution, mode):
    cutoff = market.as_of_time
    items = [market, *sectors, *anomalies, *candidates]
    if any(item.trade_date != market.trade_date for item in items):
        raise ValueError("all report artifacts must share trade_date")
    if any(item.available_at > cutoff for item in items):
        raise ValueError("report input is not PIT-visible")
    bundles = {item.candidate.symbol: item for item in attribution.bundles}
    if mode == "strict_live":
        for candidate in candidates:
            bundle = bundles.get(candidate.symbol)
            if bundle and any(
                not item.strict_pit or item.pit_mode != "strict_live"
                for item in bundle.strict_attribution_evidence
            ):
                raise ValueError("strict report contains proxy attribution evidence")


def report_to_dict(report: DailyIntelligenceReport) -> dict[str, Any]:
    value = asdict(report)
    if report.schema_version == REPORT_SCHEMA_VERSION:
        for candidate in value["candidate_briefs"]:
            for field in (
                "historical_knowledge_background",
                "retrospective_research_context",
                "supplied_context_id",
                "supplied_knowledge_refs",
            ):
                candidate["synthesis"].pop(field, None)
    return value


def report_from_dict(value: Mapping[str, Any]) -> DailyIntelligenceReport:
    _require_exact_fields(
        value,
        {field.name for field in fields(DailyIntelligenceReport)},
        "daily report",
    )
    knowledge_aware = value["schema_version"] == KNOWLEDGE_REPORT_SCHEMA_VERSION
    briefs = []
    for item in value["candidate_briefs"]:
        _require_exact_fields(
            item,
            {field.name for field in fields(CandidateBrief)},
            "candidate brief",
        )
        _require_exact_fields(
            item["evidence_stats"],
            {field.name for field in fields(EvidenceStats)},
            "evidence stats",
        )
        stats = EvidenceStats(**item["evidence_stats"])
        synthesis = synthesis_brief_from_dict(
            item["synthesis"], knowledge_aware=knowledge_aware,
        )
        briefs.append(CandidateBrief(
            rank=item["rank"], symbol=item["symbol"], name=item["name"], tier=item["tier"],
            signal_types=tuple(item["signal_types"]), market_facts=item["market_facts"],
            sector_context=item["sector_context"], fund_flow=item["fund_flow"],
            evidence_stats=stats, synthesis=synthesis,
        ))
    report = DailyIntelligenceReport(
        schema_version=value["schema_version"], report_id=value["report_id"],
        trade_date=date.fromisoformat(value["trade_date"]),
        as_of_time=datetime.fromisoformat(value["as_of_time"]),
        generated_at=datetime.fromisoformat(value["generated_at"]),
        mode=value["mode"], universe_name=value["universe_name"],
        market_overview=value["market_overview"], sector_overview=value["sector_overview"],
        anomaly_overview=value["anomaly_overview"], candidate_summary=value["candidate_summary"],
        candidate_briefs=tuple(briefs), evidence_overview=value["evidence_overview"],
        data_quality=value["data_quality"], synthesis_runtime=value["synthesis_runtime"],
        provenance=value["provenance"],
    )
    validate_daily_report_identity(report)
    return report


def synthesis_brief_from_dict(
    value: Mapping[str, Any], *, knowledge_aware: bool | None = None,
) -> SynthesisBrief:
    """Load both legacy and Knowledge-aware product synthesis surfaces."""
    all_fields = {field.name for field in fields(SynthesisBrief)}
    knowledge_fields = {
        "historical_knowledge_background",
        "retrospective_research_context",
        "supplied_context_id",
        "supplied_knowledge_refs",
    }
    legacy_fields = all_fields - knowledge_fields
    if knowledge_aware is None:
        if set(value) == legacy_fields:
            knowledge_aware = False
        elif set(value) == all_fields:
            knowledge_aware = True
        else:
            raise ValueError("synthesis brief fields disagree with schema version")
    _require_exact_fields(
        value, all_fields if knowledge_aware else legacy_fields, "synthesis brief",
    )

    def knowledge_statement(item: Mapping[str, Any]) -> KnowledgeBackgroundStatement:
        _require_exact_fields(
            item, {"statement", "knowledge_refs"}, "Knowledge background statement",
        )
        return KnowledgeBackgroundStatement(
            statement=item["statement"], knowledge_refs=tuple(item["knowledge_refs"]),
        )

    historical = tuple(
        knowledge_statement(item)
        for item in value.get("historical_knowledge_background", ())
    )
    retrospective = tuple(
        knowledge_statement(item)
        for item in value.get("retrospective_research_context", ())
    )
    return SynthesisBrief(
        **{
            **value,
            "used_evidence_ids": tuple(value["used_evidence_ids"]),
            "possible_explanations": tuple(value["possible_explanations"]),
            "contradicting_signals": tuple(value["contradicting_signals"]),
            "post_event_notes": tuple(value["post_event_notes"]),
            "limitations": tuple(value["limitations"]),
            "historical_knowledge_background": historical,
            "retrospective_research_context": retrospective,
            "supplied_context_id": value.get("supplied_context_id"),
            "supplied_knowledge_refs": tuple(value.get("supplied_knowledge_refs", ())),
        }
    )


def _require_exact_fields(
    value: Mapping[str, Any], expected: set[str], label: str,
) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{label} fields disagree with schema version")


def render_daily_report(report: DailyIntelligenceReport | Mapping[str, Any]) -> str:
    data = report_to_dict(report) if isinstance(report, DailyIntelligenceReport) else dict(report)
    lines = ["# QuantOS Daily Intelligence", "", "## 报告信息",
             f"- 交易日：{data['trade_date']}", f"- 截止时间：{data['as_of_time']}",
             f"- 模式：{data['mode']}", f"- Report ID：{data['report_id']}", "",
             "## 模式说明", RESEARCH_DISCLAIMER if data["mode"] == "research" else STRICT_DISCLAIMER,
             "", "## 一、市场概览"]
    market = data["market_overview"]
    lines.extend([f"- 样本证券：{market['security_member_count']}",
                  f"- 有效行情：{market['valid_bar_count']}",
                  f"- 覆盖率：{_percent(market['coverage_ratio'])}",
                  f"- 上涨/下跌/平盘：{market['advancer_count']}/{market['decliner_count']}/{market['flat_count']}",
                  f"- 成交量：{_display_shares(market['total_volume'])}",
                  f"- 成交额：{_display_yuan(market['total_amount'])}", "", "## 二、行业表现"])
    sector = data["sector_overview"]
    lines.append(f"- 分类体系：{sector['classification_system']}")
    lines.append(f"- 行业数：{sector['sector_count']}（完整 {sector['complete_sector_count']}，不完整 {sector['incomplete_sector_count']}）")
    lines.append("- 领先行业：" + _sector_list(sector["top_sectors"]))
    lines.append("- 落后行业：" + _sector_list(sector["bottom_sectors"]))
    anomaly = data["anomaly_overview"]
    lines.extend(["", "## 三、异常扫描",
                  f"- 扫描/可分析：{anomaly['stocks_scanned']}/{anomaly['stocks_analyzed']}",
                  f"- 历史不足：{anomaly['insufficient_history']}",
                  f"- 价格/成交量/成交额异常：{anomaly['price_anomaly_count']}/{anomaly['volume_anomaly_count']}/{anomaly['amount_anomaly_count']}",
                  f"- 候选数：{anomaly['candidate_count']}", "", "## 四、重点候选"])
    for brief in data["candidate_briefs"]:
        lines.extend(_render_candidate(brief))
    evidence = data["evidence_overview"]
    lines.extend(["", "## 五、证据概览",
                  f"- Discovery/Event：{evidence['discovery_evidence_count']}/{evidence['event_evidence_count']}",
                  f"- Research/Strict attribution：{evidence['research_attribution_count']}/{evidence['strict_attribution_count']}",
                  f"- Post-event context：{evidence['post_event_context_count']}", "",
                  "## 六、数据质量与限制",
                  f"- 历史代理证据使用：{str(data['data_quality']['evidence']['historical_proxy_used']).lower()}",
                  "- Data Quality 仅描述数据覆盖，不修改 System Health。", "",
                  "## 七、运行统计"])
    runtime = data["synthesis_runtime"]
    lines.extend([f"- Cache hit / generated / fast path / failure：{runtime['cache_hits']}/{runtime['generated']}/{runtime['no_evidence_fast_paths']}/{runtime['failures']}",
                  f"- Planned / actual LLM requests：{runtime['planned_llm_requests']}/{runtime['actual_llm_requests']}",
                  f"- Total tokens：{runtime['total_tokens']}", "", "## 八、方法与版本"])
    for key in PROVENANCE_FIELDS:
        lines.append(f"- {key}：{data['provenance'].get(key)}")
    return "\n".join(lines) + "\n"


def _render_candidate(brief):
    synthesis = brief["synthesis"]
    lines = ["", f"### {brief['rank']}. {brief['symbol']} {brief['name']}",
             "", "#### 异常信号", ", ".join(brief["signal_types"]),
             "", "#### 行情事实"]
    lines.extend(f"- {key}: {value}" for key, value in brief["market_facts"].items())
    lines.extend(["", "#### 行业上下文",
                  "unavailable" if brief["sector_context"] is None else
                  f"{brief['sector_context']['sector_name']}（证监会行业分类）",
                  "", "#### 资金流证据"])
    flow = brief["fund_flow"]
    lines.append("unavailable" if not flow["available"] else
                 f"net_mf_amount={flow['net_mf_amount']}，large_plus_extra_net_amount={flow['large_plus_extra_net_amount']}，flow_direction={flow['flow_direction']}")
    stats = brief["evidence_stats"]
    lines.extend(["", "#### 事件证据",
                  f"discovery={stats['discovery_count']}，event={stats['event_evidence_count']}，eligible={stats['eligible_attribution_count']}",
                  "", "#### Evidence Synthesis"])
    if synthesis["status"] == "FAIL":
        code = synthesis.get("validation_error_code") or synthesis.get("error_category") or "UNKNOWN"
        lines.append(f"证据综合生成失败：{code}")
    elif synthesis["status"] == "NO_EVIDENCE_FAST_PATH":
        lines.append("严格时点下暂无可归因证据。")
    else:
        for item in synthesis["possible_explanations"]:
            lines.append(f"- {item['statement']}")
        for item in synthesis["contradicting_signals"]:
            lines.append(f"- 反向信号：{item}")
        for item in synthesis["post_event_notes"]:
            lines.append(f"- 事后说明：{item}")
    if synthesis.get("supplied_context_id") is not None:
        lines.extend(["", "#### Historical Knowledge Background"])
        historical = synthesis.get("historical_knowledge_background", ())
        lines.extend(
            f"- {item['statement']} [Knowledge refs: {', '.join(item['knowledge_refs'])}]"
            for item in historical
        )
        if not historical:
            lines.append("- 无已选历史 Knowledge 内容")
        lines.append(f"- Supplied context ID: {synthesis['supplied_context_id']}")
        lines.append(
            "- Supplied Knowledge refs: "
            + (", ".join(synthesis.get("supplied_knowledge_refs", ())) or "none")
        )
        if synthesis.get("retrospective_research_context"):
            lines.extend(["", "#### Retrospective Research Context"])
            lines.extend(
                f"- {item['statement']} [Knowledge refs: {', '.join(item['knowledge_refs'])}]"
                for item in synthesis["retrospective_research_context"]
            )
    lines.extend(["", "#### 限制与说明"])
    lines.extend(f"- {item}" for item in synthesis["limitations"])
    if not synthesis["limitations"]:
        lines.append("- 无额外说明")
    return lines


def _percent(value):
    return "unavailable" if value is None else f"{Decimal(str(value)) * 100:.2f}%"


def _display_shares(value):
    number = Decimal(str(value))
    return f"{number / Decimal('100000000'):.2f} 亿股" if abs(number) >= Decimal("100000000") else f"{number / Decimal('10000'):.2f} 万股"


def _display_yuan(value):
    number = Decimal(str(value))
    return f"{number / Decimal('100000000'):.2f} 亿元" if abs(number) >= Decimal("100000000") else f"{number / Decimal('10000'):.2f} 万元"


def _sector_list(values):
    return "无" if not values else "、".join(
        f"{item['sector_name']}({item['ranking_metric_value']})" for item in values
    )
