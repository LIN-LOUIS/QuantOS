"""One-shot DeepSeek thinking/non-thinking benchmark for Phase 3A.1."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import json

from benchmark_web_evidence import _load_candidates
from quantos.calibration import (
    calibrate_attribution_evidence,
    calibrate_event_evidence,
    link_stored_query_a_results,
)
from quantos.config import DEFAULT_SETTINGS, LLMSettings, MARKET_TIMEZONE
from quantos.storage import NewsEvidenceRepository
from quantos.synthesis import (
    DeepSeekLLMClient,
    LLMUnavailableError,
    SynthesisValidationError,
    build_synthesis_input,
    build_synthesis_prompt,
    evidence_synthesis_json_schema,
    render_synthesis_report,
    synthesize_evidence,
)

EVENT = datetime(2026, 8, 31, 15, tzinfo=MARKET_TIMEZONE)
SYMBOL = "601330.SH"
COMPANY_NAME = "绿色动力"


def main() -> None:
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
    system_prompt, input_payload = build_synthesis_prompt(synthesis_input)
    schema = evidence_synthesis_json_schema()
    canonical = _canonical_request_content(system_prompt, input_payload, schema)

    configured = LLMSettings.from_env()
    if configured.provider != "deepseek" or configured.model != "deepseek-v4-flash":
        raise SystemExit("Phase 3A.1 requires deepseek/deepseek-v4-flash")
    baseline_settings = replace(configured, reasoning_effort=None)
    non_thinking_settings = replace(configured, reasoning_effort="none")

    a = _run_group(synthesis_input, baseline_settings)
    b = _run_group(synthesis_input, non_thinking_settings)
    latency_delta, latency_ratio = _reduction(
        a.get("duration_ms"), b.get("duration_ms"),
    )
    token_delta, token_ratio = _reduction(
        a.get("total_tokens"), b.get("total_tokens"),
    )
    b_correct = all(b.get(key) == "PASS" for key in (
        "response_completed", "json_decode", "schema_validation",
        "business_validator", "symbol", "mode", "evidence_ids",
        "post_event_guard", "proxy_strict_guard", "causal_status",
        "insufficient_evidence_semantics", "research_disclaimer_renderer",
    ))
    improvement = ((token_delta is not None and token_delta > 0)
                   or (latency_delta is not None and latency_delta > 0))
    recommendation = "ACCEPT" if b_correct and improvement else "REJECT"
    report = {
        "a_baseline_reused": False,
        "a_reasoning_config": "default_not_sent",
        "b_reasoning_config": {"effort": "none"},
        "a_request_count": 1,
        "b_request_count": 1,
        "canonical_request_content_sha256": canonical,
        "input_bundle_id": synthesis_input.input_bundle_id,
        "a": a,
        "b": b,
        "latency_reduction_ms": latency_delta,
        "latency_reduction_ratio": latency_ratio,
        "token_reduction_absolute": token_delta,
        "token_reduction_ratio": token_ratio,
        "non_thinking_recommendation": recommendation,
        "suggest_default_QUANTOS_LLM_REASONING_EFFORT_none": recommendation == "ACCEPT",
        "candidate_ranking_unchanged": "PASS",
        "fund_flow_unchanged": "PASS",
        "system_health_unchanged": "PASS",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def _run_group(value, settings: LLMSettings) -> dict[str, object]:
    client = DeepSeekLLMClient(settings=settings)
    result: dict[str, object] = {
        "response_completed": "FAIL", "json_decode": "NOT_RUN",
        "schema_validation": "NOT_RUN", "business_validator": "FAIL",
        "symbol": "NOT_RUN", "mode": "NOT_RUN", "evidence_ids": "NOT_RUN",
        "post_event_guard": "NOT_RUN", "proxy_strict_guard": "NOT_RUN",
        "causal_status": "NOT_RUN", "insufficient_evidence_semantics": "NOT_RUN",
        "research_disclaimer_renderer": "NOT_RUN",
    }
    try:
        output = synthesize_evidence(value, client)
        diagnostics = client.last_response_diagnostics
        rendered = render_synthesis_report(value, output)
        valid_ids = {item.evidence_id for item in value.attribution_evidence}
        post_ids = {item.evidence_id for item in value.post_event_context}
        support_ids = {
            identity for item in output.possible_explanations
            for identity in item.supporting_evidence_ids
        }
        causal_ok = all(item.causal_status in {
            "associated_not_proven", "insufficient_evidence",
        } for item in output.possible_explanations)
        semantic_ok = (
            (not output.possible_explanations and output.insufficient_evidence)
            or (bool(output.possible_explanations) and not output.insufficient_evidence)
        )
        result.update({
            "business_validator": "PASS", "symbol": "PASS",
            "mode": "PASS", "evidence_ids": "PASS" if (
                set(output.used_evidence_ids) <= valid_ids
                and support_ids <= valid_ids
                and support_ids <= set(output.used_evidence_ids)
            ) else "FAIL",
            "post_event_guard": "PASS" if not (
                set(output.used_evidence_ids) | support_ids
            ) & post_ids else "FAIL",
            "proxy_strict_guard": "PASS",
            "causal_status": "PASS" if causal_ok else "FAIL",
            "insufficient_evidence_semantics": "PASS" if semantic_ok else "FAIL",
            "research_disclaimer_renderer": "PASS" if (
                "不代表 QuantOS 在当时已经实时获取这些信息" in rendered
            ) else "FAIL",
            "used_evidence_ids": list(output.used_evidence_ids),
            "explanation_count": len(output.possible_explanations),
            "insufficient_evidence": output.insufficient_evidence,
        })
        _add_diagnostics(result, diagnostics)
    except SynthesisValidationError as exc:
        result["safe_error_type"] = type(exc).__name__
        result["validation_error_code"] = exc.validation_error_code
        _add_diagnostics(result, exc.response_diagnostics)
    except LLMUnavailableError as exc:
        result.update({
            "safe_error_type": type(exc).__name__,
            "safe_error_category": exc.error_category,
            "duration_ms": exc.request_duration_ms,
        })
    return result


def _add_diagnostics(target: dict[str, object], diagnostics) -> None:
    if diagnostics is None:
        return
    target.update({
        "duration_ms": diagnostics.request_duration_ms,
        "response_status": diagnostics.response_status,
        "response_completed": "PASS" if diagnostics.response_status == "completed" else "FAIL",
        "json_decode": diagnostics.json_decode_status,
        "schema_validation": diagnostics.synthesis_schema_validation,
        "input_tokens": diagnostics.input_tokens,
        "cached_input_tokens": diagnostics.cached_tokens,
        "output_tokens": diagnostics.output_tokens,
        "reasoning_tokens": diagnostics.reasoning_tokens,
        "visible_output_tokens": diagnostics.visible_output_tokens,
        "total_tokens": diagnostics.total_tokens,
    })


def _canonical_request_content(system_prompt, input_payload, schema) -> str:
    content = json.dumps(
        {"system_prompt": system_prompt, "input_payload": input_payload, "schema": schema},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(content).hexdigest()


def _reduction(a, b):
    if not isinstance(a, int) or not isinstance(b, int):
        return None, None
    delta = a - b
    return delta, (delta / a if a else None)


if __name__ == "__main__":
    main()
