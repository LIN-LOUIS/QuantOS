"""Bounded Phase 3A smoke over stored evidence; never performs discovery."""

from __future__ import annotations

import json
import os
from datetime import datetime

from benchmark_web_evidence import _load_candidates
from quantos.calibration import calibrate_attribution_evidence, calibrate_event_evidence, link_stored_query_a_results
from quantos.config import DEFAULT_SETTINGS, LLMSettings, MARKET_TIMEZONE
from quantos.storage import NewsEvidenceRepository, SynthesisRepository
from quantos.synthesis import (
    DeepSeekLLMClient, LLMUnavailableError, SynthesisValidationError,
    build_synthesis_input, render_synthesis_report, synthesize_evidence,
)

EVENT = datetime(2026, 8, 31, 15, tzinfo=MARKET_TIMEZONE)
SYMBOL = "601330.SH"
COMPANY_NAME = "绿色动力"


def main() -> None:
    now = datetime.now(MARKET_TIMEZONE)
    candidates, flows = _load_candidates(now)
    source = NewsEvidenceRepository(DEFAULT_SETTINGS)
    proxy = source.query_web_search_results(as_of_time=now, pit_mode="source_timestamp_proxy")
    strict = source.query_web_search_results(as_of_time=now, pit_mode="strict_live")
    research_selection = _attribution(proxy, candidates, flows, now)
    strict_selection = _attribution(strict, candidates, flows, now)
    research_bundle = next(item for item in research_selection.bundles if item.candidate.symbol == SYMBOL)
    strict_bundle = next(item for item in strict_selection.bundles if item.candidate.symbol == SYMBOL)
    research_input = build_synthesis_input(
        research_bundle, research_selection.attribution_facts, mode="research",
        company_name=COMPANY_NAME, market_event_time=EVENT,
    )
    strict_input = build_synthesis_input(
        strict_bundle, strict_selection.attribution_facts, mode="strict_live",
        company_name=COMPANY_NAME, market_event_time=EVENT,
    )
    settings = LLMSettings.from_env()
    report = {
        "target_date": "2026-08-31", "symbol": SYMBOL,
        "deepseek_provider_capability": "NOT_RUN",
        "quantos_synthesis_e2e_status": "NOT_RUN", "actual_model_name": settings.model,
        "real_llm_request_count": 0,
        "research_smoke": "NOT_RUN",
        "strict_smoke": "NOT_RUN",
        "research_smoke_used_evidence_ids": [],
        "token_usage": None,
    }
    if settings.provider == "deepseek" and settings.model and os.environ.get("DEEPSEEK_API_KEY"):
        try:
            report["real_llm_request_count"] = 1
            client = DeepSeekLLMClient(settings=settings)
            research = synthesize_evidence(research_input, client)
            rendered = render_synthesis_report(research_input, research)
            SynthesisRepository(DEFAULT_SETTINGS).write(research)
            report.update({
                "deepseek_provider_capability": "PASS",
                "quantos_synthesis_e2e_status": "PASS", "research_smoke": "PASS",
                "research_smoke_used_evidence_ids": list(research.used_evidence_ids),
                "token_usage": {"input_tokens": research.input_tokens,
                                "output_tokens": research.output_tokens,
                                "total_tokens": research.total_tokens},
                "research_attribution_evidence_count": len(research_input.attribution_evidence),
                "possible_explanations_count": len(research.possible_explanations),
                "insufficient_evidence": research.insufficient_evidence,
                "renderer": "PASS",
                "research_disclaimer": "PASS" if "不代表 QuantOS 在当时已经实时获取这些信息" in rendered else "FAIL",
                "persistence": "PASS",
            })
            _add_diagnostic_fields(report, client.last_response_diagnostics)
        except SynthesisValidationError as exc:
            report.update({"deepseek_provider_capability": exc.provider_request_status or "UNKNOWN",
                           "quantos_synthesis_e2e_status": "PARTIAL",
                           "research_smoke": "VALIDATOR_REJECTED",
                           "safe_error_type": type(exc).__name__,
                           "validation_error_code": exc.validation_error_code,
                           "validation_field": exc.validation_field,
                           "offending_evidence_id": exc.offending_evidence_id,
                           "transport_status": "SUCCESS" if exc.provider_request_status == "PASS" else "UNKNOWN",
                           "token_usage": {"input_tokens": exc.input_tokens,
                                           "output_tokens": exc.output_tokens,
                                           "total_tokens": exc.total_tokens}})
            _add_diagnostic_fields(report, exc.response_diagnostics)
        except LLMUnavailableError as exc:
            report.update({"deepseek_provider_capability": "FAIL",
                           "quantos_synthesis_e2e_status": "FAIL",
                           "research_smoke": "PROVIDER_UNAVAILABLE",
                           "safe_error_type": type(exc).__name__,
                           "safe_error_category": exc.error_category,
                           "http_status": exc.status_code,
                           "request_duration_ms": exc.request_duration_ms,
                           "timeout_stage": exc.timeout_stage})
    if strict_input.attribution_evidence:
        report["real_llm_request_count"] += 1
        try:
            strict_output = synthesize_evidence(strict_input, DeepSeekLLMClient(settings=settings))
            report["strict_smoke"] = "PASS_LLM"
        except (SynthesisValidationError, LLMUnavailableError) as exc:
            report.update({"strict_smoke": "FAILED", "strict_safe_error_type": type(exc).__name__})
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
    else:
        strict_output = synthesize_evidence(strict_input, DeepSeekLLMClient(settings=settings))
        report["strict_smoke"] = "PASS_DETERMINISTIC_NO_EVIDENCE"
    SynthesisRepository(DEFAULT_SETTINGS).write(strict_output)
    report["strict_report"] = render_synthesis_report(strict_input, strict_output)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _attribution(records, candidates, flows, as_of_time):
    events = calibrate_event_evidence(records, link_stored_query_a_results(records), market_event_time=EVENT)
    return calibrate_attribution_evidence(
        events, candidates, flows, market_event_time=EVENT, as_of_time=as_of_time,
    )


def _add_diagnostic_fields(report, metadata):
    if metadata is None:
        return
    report.update({
        "request_duration_ms": metadata.request_duration_ms,
        "response_status": metadata.response_status,
        "full_synthesis_structured_output": metadata.synthesis_schema_validation,
        "json_decode": metadata.json_decode_status,
        "synthesis_schema_validation": metadata.synthesis_schema_validation,
    })


if __name__ == "__main__":
    main()
