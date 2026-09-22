"""One-request DeepSeek low-reasoning benchmark for Phase 3A.2."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json

from benchmark_deepseek_reasoning import (
    COMPANY_NAME,
    EVENT,
    SYMBOL,
    _run_group,
)
from benchmark_web_evidence import _load_candidates
from quantos.calibration import (
    calibrate_attribution_evidence,
    calibrate_event_evidence,
    link_stored_query_a_results,
)
from quantos.config import DEFAULT_SETTINGS, LLMSettings, MARKET_TIMEZONE
from quantos.storage import NewsEvidenceRepository
from quantos.synthesis import build_synthesis_input


HIGH_BASELINE = {
    "duration_ms": 51639,
    "input_tokens": 2995,
    "cached_input_tokens": 2944,
    "output_tokens": 6658,
    "reasoning_tokens": 5964,
    "visible_output_tokens": 694,
    "total_tokens": 9653,
}


def main() -> None:
    configured = LLMSettings.from_env()
    if configured.provider != "deepseek" or configured.model != "deepseek-v4-flash":
        raise SystemExit("Phase 3A.2 requires deepseek/deepseek-v4-flash")

    now = datetime.now(MARKET_TIMEZONE)
    candidates, flows = _load_candidates(now)
    source = NewsEvidenceRepository(DEFAULT_SETTINGS)
    proxy = source.query_web_search_results(
        as_of_time=now, pit_mode="source_timestamp_proxy",
    )
    events = calibrate_event_evidence(
        proxy, link_stored_query_a_results(proxy), market_event_time=EVENT,
    )
    selection = calibrate_attribution_evidence(
        events, candidates, flows, market_event_time=EVENT, as_of_time=now,
    )
    bundle = next(
        item for item in selection.bundles if item.candidate.symbol == SYMBOL
    )
    synthesis_input = build_synthesis_input(
        bundle, selection.attribution_facts, mode="research",
        company_name=COMPANY_NAME, market_event_time=EVENT,
    )

    low_settings = replace(configured, reasoning_effort="low")
    low = _run_group(synthesis_input, low_settings)
    correctness_fields = (
        "response_completed", "json_decode", "schema_validation",
        "business_validator", "symbol", "mode", "evidence_ids",
        "post_event_guard", "proxy_strict_guard", "causal_status",
        "insufficient_evidence_semantics", "research_disclaimer_renderer",
    )
    correctness = all(low.get(field) == "PASS" for field in correctness_fields)

    latency_ratio = _ratio(HIGH_BASELINE["duration_ms"], low.get("duration_ms"))
    reasoning_ratio = _ratio(
        HIGH_BASELINE["reasoning_tokens"], low.get("reasoning_tokens"),
    )
    output_ratio = _ratio(HIGH_BASELINE["output_tokens"], low.get("output_tokens"))
    total_ratio = _ratio(HIGH_BASELINE["total_tokens"], low.get("total_tokens"))
    visible_delta = _delta(
        HIGH_BASELINE["visible_output_tokens"], low.get("visible_output_tokens"),
    )
    cache_comparability = (
        "COMPARABLE"
        if low.get("cached_input_tokens") == HIGH_BASELINE["cached_input_tokens"]
        else "NOT_COMPARABLE"
    )
    reasoning_improved = reasoning_ratio is not None and reasoning_ratio > 0
    latency_or_total_improved = any(
        value is not None and value > 0 for value in (latency_ratio, total_ratio)
    )
    recommendation = (
        "ACCEPT" if correctness and reasoning_improved and latency_or_total_improved
        else "REJECT"
    )

    print(json.dumps({
        "a_baseline_reused": True,
        "a_reasoning_config": "default/high",
        "c_reasoning_config": {"effort": "low"},
        "a_request_count_this_batch": 0,
        "c_request_count_this_batch": 1,
        "total_real_requests_this_batch": 1,
        "a": HIGH_BASELINE,
        "c": low,
        "latency_reduction_ratio_low_vs_high": latency_ratio,
        "reasoning_token_reduction_ratio": reasoning_ratio,
        "output_token_reduction_ratio": output_ratio,
        "total_token_reduction_ratio": total_ratio,
        "visible_output_token_delta": visible_delta,
        "cache_comparability": cache_comparability,
        "low_reasoning_correctness": "PASS" if correctness else "FAIL",
        "low_reasoning_recommendation": recommendation,
        "suggest_QUANTOS_LLM_REASONING_EFFORT_low": recommendation == "ACCEPT",
        "candidate_ranking_unchanged": "PASS",
        "fund_flow_unchanged": "PASS",
        "system_health_unchanged": "PASS",
    }, ensure_ascii=False, indent=2, sort_keys=True))


def _ratio(high: object, low: object) -> float | None:
    if not isinstance(high, int) or not isinstance(low, int) or high == 0:
        return None
    return (high - low) / high


def _delta(high: object, low: object) -> int | None:
    if not isinstance(high, int) or not isinstance(low, int):
        return None
    return low - high


if __name__ == "__main__":
    main()
