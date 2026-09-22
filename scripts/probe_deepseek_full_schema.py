"""One-request full-schema diagnostic; no business validation or persistence."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from benchmark_web_evidence import _load_candidates
from smoke_llm_synthesis import COMPANY_NAME, EVENT, SYMBOL, _attribution
from quantos.config import DEFAULT_SETTINGS, LLMSettings, MARKET_TIMEZONE
from quantos.storage import NewsEvidenceRepository
from quantos.synthesis import (
    DeepSeekLLMClient, LLMUnavailableError, SynthesisValidationError,
    build_synthesis_input, build_synthesis_prompt, evidence_synthesis_json_schema,
)


def main() -> None:
    now = datetime.now(MARKET_TIMEZONE)
    candidates, flows = _load_candidates(now)
    records = NewsEvidenceRepository(DEFAULT_SETTINGS).query_web_search_results(
        as_of_time=now, pit_mode="source_timestamp_proxy",
    )
    selection = _attribution(records, candidates, flows, now)
    bundle = next(item for item in selection.bundles if item.candidate.symbol == SYMBOL)
    value = build_synthesis_input(
        bundle, selection.attribution_facts, mode="research",
        company_name=COMPANY_NAME, market_event_time=EVENT,
    )
    system, payload = build_synthesis_prompt(value)
    report = {
        "real_generation_request_count": 1,
        "research_evidence_input_count": len(value.attribution_evidence),
        "business_validator_status": "NOT_RUN",
        "full_synthesis_structured_output": "FAIL",
    }
    try:
        generation = DeepSeekLLMClient(settings=LLMSettings.from_env()).generate_structured(
            system_prompt=system, input_payload=payload,
            output_schema=evidence_synthesis_json_schema(),
        )
        report.update({"deepseek_provider_capability": "PASS",
                       "full_synthesis_structured_output": "PASS"})
        _add_metadata(report, generation.response_diagnostics)
    except SynthesisValidationError as exc:
        report.update({"deepseek_provider_capability": exc.provider_request_status or "UNKNOWN",
                       "validation_error_code": exc.validation_error_code})
        _add_metadata(report, exc.response_diagnostics)
    except LLMUnavailableError as exc:
        report.update({"deepseek_provider_capability": "FAIL",
                       "provider_error_category": exc.error_category,
                       "http_status": exc.status_code})
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def _add_metadata(report, metadata) -> None:
    if metadata is None:
        return
    report.update({
        "response_status": metadata.response_status,
        "response_error_present": metadata.response_error_present,
        "incomplete_reason": metadata.incomplete_reason,
        "response_model": metadata.response_model,
        "output_item_types": list(metadata.output_item_types),
        "output_item_statuses": list(metadata.output_item_statuses),
        "message_item_count": metadata.message_item_count,
        "reasoning_item_count": metadata.reasoning_item_count,
        "message_content_types": list(metadata.message_content_types),
        "output_text_part_count": metadata.output_text_part_count,
        "output_text_length": metadata.output_text_length,
        "output_text_sha256": metadata.output_text_sha256,
        "first_non_whitespace_type": metadata.first_non_whitespace_type,
        "last_non_whitespace_type": metadata.last_non_whitespace_type,
        "starts_with_code_fence": metadata.starts_with_code_fence,
        "ends_with_code_fence": metadata.ends_with_code_fence,
        "schema_response_error_code": metadata.schema_response_error_code,
        "json_decode_status": metadata.json_decode_status,
        "json_error_class": metadata.json_error_class,
        "json_error_position": metadata.json_error_position,
        "json_top_level_type": metadata.json_top_level_type,
        "json_top_level_keys": list(metadata.json_top_level_keys),
        "missing_expected_top_level_keys": list(metadata.missing_expected_top_level_keys),
        "extra_top_level_keys": list(metadata.extra_top_level_keys),
        "synthesis_schema_validation": metadata.synthesis_schema_validation,
        "schema_errors": [{"location": loc, "type": kind}
                          for loc, kind in metadata.schema_errors],
        "input_tokens": metadata.input_tokens,
        "output_tokens": metadata.output_tokens,
        "total_tokens": metadata.total_tokens,
        "cached_tokens": metadata.cached_tokens,
        "reasoning_tokens": metadata.reasoning_tokens,
    })


if __name__ == "__main__":
    main()
