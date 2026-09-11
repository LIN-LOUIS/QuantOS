"""Cached, gated, bounded batch runtime for validated evidence synthesis."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
from threading import Lock
import time
from typing import Callable, Sequence

from quantos.config import SynthesisBatchSettings
from quantos.schemas import EvidenceSynthesis, SynthesisInput
from quantos.storage import SynthesisRepository
from quantos.synthesis import (
    LLMClient, LLMUnavailableError, SynthesisValidationError, synthesize_evidence,
    validate_synthesis_input,
)


@dataclass(frozen=True, slots=True)
class SynthesisCacheKey:
    input_bundle_id: str
    prompt_version: str
    schema_version: str
    model_provider: str
    model_name: str
    reasoning_config: str
    knowledge_context_id: str | None = None
    supplied_knowledge_refs: tuple[str, ...] = ()

    @classmethod
    def for_input(
        cls, value: SynthesisInput, *, model_provider: str, model_name: str,
        reasoning_config: str,
    ) -> "SynthesisCacheKey":
        context = value.knowledge_context
        references = (
            tuple(f"K{item.retrieval_rank}" for item in context.selected_items)
            if context is not None else ()
        )
        return cls(
            value.input_bundle_id, value.prompt_version, value.schema_version,
            model_provider, model_name, reasoning_config,
            context.context_id if context is not None else None,
            references,
        )

    def as_dict(self) -> dict[str, str]:
        fields = {
            "input_bundle_id": self.input_bundle_id,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "reasoning_config": self.reasoning_config,
            "cache_identity": self.identity,
        }
        if self.knowledge_context_id is not None:
            fields["knowledge_context_id"] = self.knowledge_context_id
            fields["supplied_knowledge_refs"] = ",".join(self.supplied_knowledge_refs)
        return fields

    @property
    def identity(self) -> str:
        fields = {
            "input_bundle_id": self.input_bundle_id,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "reasoning_config": self.reasoning_config,
        }
        if self.knowledge_context_id is not None:
            fields["knowledge_context_id"] = self.knowledge_context_id
            fields["supplied_knowledge_refs"] = self.supplied_knowledge_refs
        canonical = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateSynthesisResult:
    symbol: str
    rank: int
    status: str
    output: EvidenceSynthesis | None = None
    safe_error_type: str | None = None
    validation_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SynthesisBatchReport:
    results: tuple[CandidateSynthesisResult, ...]
    candidate_count: int
    selected_candidate_count: int
    planned_llm_request_count: int
    actual_llm_request_count: int
    cache_hits: int
    cache_misses: int
    no_evidence_fast_path_count: int
    requests_avoided_by_cache: int
    requests_avoided_by_no_evidence: int
    total_requests_avoided: int
    cache_hit_ratio: float
    total_input_tokens: int
    total_cached_input_tokens: int
    total_output_tokens: int
    total_reasoning_tokens: int
    total_tokens: int
    total_llm_duration_ms: int
    wall_clock_duration_ms: int
    max_concurrency_observed: int
    duplicate_generation_prevented: int


@dataclass(frozen=True, slots=True)
class _Usage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    duration_ms: int | None = None


def synthesize_batch(
    inputs: Sequence[SynthesisInput], *,
    client_factory: Callable[[SynthesisInput], LLMClient],
    repository: SynthesisRepository,
    model_provider: str,
    model_name: str,
    reasoning_config: str = "default/high",
    settings: SynthesisBatchSettings | None = None,
) -> SynthesisBatchReport:
    """Synthesize only ranked report candidates with no retries."""

    config = settings or SynthesisBatchSettings.from_env()
    started = time.monotonic()
    selected_indexes = set(sorted(
        range(len(inputs)), key=lambda index: inputs[index].candidate.rank,
    )[:config.synthesis_top_n])
    results: list[CandidateSynthesisResult | None] = [None] * len(inputs)
    groups: dict[str, list[tuple[int, SynthesisInput, SynthesisCacheKey]]] = {}
    persistent_hits = 0
    misses = 0
    no_evidence = 0

    for index, value in enumerate(inputs):
        if index not in selected_indexes:
            results[index] = CandidateSynthesisResult(
                value.symbol, value.candidate.rank, "NOT_SELECTED",
            )
            continue
        try:
            validate_synthesis_input(value)
        except SynthesisValidationError as exc:
            results[index] = CandidateSynthesisResult(
                value.symbol,
                value.candidate.rank,
                "FAIL",
                safe_error_type=type(exc).__name__,
                validation_error_code=exc.validation_error_code,
            )
            continue
        has_selected_knowledge = (
            value.knowledge_context is not None
            and value.knowledge_context.selected_item_count > 0
        )
        if not value.attribution_evidence and not has_selected_knowledge:
            output = synthesize_evidence(value, client_factory(value))
            results[index] = CandidateSynthesisResult(
                value.symbol, value.candidate.rank, "NO_EVIDENCE_FAST_PATH", output,
            )
            no_evidence += 1
            continue
        key = SynthesisCacheKey.for_input(
            value, model_provider=model_provider, model_name=model_name,
            reasoning_config=reasoning_config,
        )
        cached = repository.find_valid(key.as_dict())
        if cached is not None:
            results[index] = CandidateSynthesisResult(
                value.symbol, value.candidate.rank, "CACHE_HIT", cached,
            )
            persistent_hits += 1
            continue
        misses += 1
        groups.setdefault(key.identity, []).append((index, value, key))

    active = 0
    maximum_active = 0
    counter_lock = Lock()

    def generate(group: list[tuple[int, SynthesisInput, SynthesisCacheKey]]):
        nonlocal active, maximum_active
        index, value, key = group[0]
        with counter_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            output = synthesize_evidence(value, client_factory(value))
            if (output.model_provider, output.model_name) != (
                key.model_provider, key.model_name,
            ):
                raise ValueError("generation model identity does not match cache key")
            repository.write(output, cache_key=key.as_dict())
            return group, output, None, _usage_from_output(output)
        except Exception as exc:  # each candidate is isolated; there is no retry
            return group, None, exc, _usage_from_error(exc)
        finally:
            with counter_lock:
                active -= 1

    completed = []
    with ThreadPoolExecutor(max_workers=config.max_concurrency) as executor:
        futures = [executor.submit(generate, group) for group in groups.values()]
        for future in as_completed(futures):
            completed.append(future.result())

    usages: list[_Usage] = []
    coalesced = sum(max(0, len(group) - 1) for group in groups.values())
    for group, output, error, usage in completed:
        usages.append(usage)
        for position, (index, value, _key) in enumerate(group):
            if output is not None:
                results[index] = CandidateSynthesisResult(
                    value.symbol, value.candidate.rank,
                    "PASS" if position == 0 else "CACHE_HIT", output,
                )
            else:
                results[index] = CandidateSynthesisResult(
                    value.symbol, value.candidate.rank, "FAIL",
                    safe_error_type=type(error).__name__,
                    validation_error_code=(
                        error.validation_error_code
                        if isinstance(error, SynthesisValidationError) else None
                    ),
                )

    finalized = tuple(item for item in results if item is not None)
    cache_hits = sum(item.status == "CACHE_HIT" for item in finalized)
    avoided_by_cache = persistent_hits + coalesced
    wall_clock_ms = round((time.monotonic() - started) * 1000)
    return SynthesisBatchReport(
        results=finalized,
        candidate_count=len(inputs),
        selected_candidate_count=len(selected_indexes),
        planned_llm_request_count=len(groups),
        actual_llm_request_count=len(groups),
        cache_hits=cache_hits,
        cache_misses=misses,
        no_evidence_fast_path_count=no_evidence,
        requests_avoided_by_cache=avoided_by_cache,
        requests_avoided_by_no_evidence=no_evidence,
        total_requests_avoided=avoided_by_cache + no_evidence,
        cache_hit_ratio=(persistent_hits / (persistent_hits + misses)
                         if persistent_hits + misses else 0.0),
        total_input_tokens=_sum(usages, "input_tokens"),
        total_cached_input_tokens=_sum(usages, "cached_input_tokens"),
        total_output_tokens=_sum(usages, "output_tokens"),
        total_reasoning_tokens=_sum(usages, "reasoning_tokens"),
        total_tokens=_sum(usages, "total_tokens"),
        total_llm_duration_ms=_sum(usages, "duration_ms"),
        wall_clock_duration_ms=wall_clock_ms,
        max_concurrency_observed=maximum_active,
        duplicate_generation_prevented=coalesced,
    )


def _usage_from_output(output: EvidenceSynthesis) -> _Usage:
    return _Usage(
        output.input_tokens, output.cached_input_tokens, output.output_tokens,
        output.reasoning_tokens, output.total_tokens, output.duration_ms,
    )


def _usage_from_error(error: Exception) -> _Usage:
    if isinstance(error, SynthesisValidationError):
        diagnostics = error.response_diagnostics
        return _Usage(
            error.input_tokens,
            diagnostics.cached_tokens if diagnostics else None,
            error.output_tokens,
            diagnostics.reasoning_tokens if diagnostics else None,
            error.total_tokens,
            diagnostics.request_duration_ms if diagnostics else None,
        )
    if isinstance(error, LLMUnavailableError):
        return _Usage(duration_ms=error.request_duration_ms)
    return _Usage()


def _sum(usages: Sequence[_Usage], field: str) -> int:
    return sum(value for usage in usages
               if (value := getattr(usage, field)) is not None)
