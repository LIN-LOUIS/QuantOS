"""Offline V1 evaluation over frozen QuantOS product and runtime contracts.

The harness is deliberately outside the production decision path.  It calls
the existing deterministic layers and records their results; it does not
change ranking, PIT, retrieval, synthesis, product, manifest, or scheduler
semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
import csv
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time as runtime_time
from typing import Mapping, Sequence
from unittest.mock import patch

from quantos.calibration import (
    calibrate_attribution_evidence,
    calibrate_event_evidence,
    link_stored_query_a_results,
)
from quantos.config import (
    MARKET_TIMEZONE,
    KnowledgeIntegrationSettings,
    Settings,
)
from quantos.knowledge_integration import (
    KnowledgePreparationResult,
    KnowledgePreparationStatus,
    prepare_candidate_synthesis_input,
)
from quantos.knowledge_retrieval import build_knowledge_lexical_index
from quantos.orchestration import (
    collect_readiness,
    execute_run,
    load_local_operational_artifacts,
    local_artifact_handlers,
)
from quantos.reporting import generate_daily_report
from quantos.schemas import (
    AnomalyCandidate,
    AnomalyStatus,
    FundFlowEvidence,
    KnowledgeDocumentInput,
    KnowledgeProvenance,
    KnowledgeSourceType,
    KnowledgeTemporalClass,
    MarketSnapshot,
    StockAnomaly,
    WebSearchResult,
    canonicalize_knowledge_document,
)
from quantos.schemas.run import (
    ArtifactReference,
    KnowledgeOperationalStatus,
    RunContext,
    RunType,
)
from quantos.serialization import canonical_json_bytes
from quantos.storage import (
    DailyReportRepository,
    KnowledgeContextRepository,
    KnowledgeLexicalIndexRepository,
    KnowledgeRepository,
    RunRepository,
    SynthesisRepository,
    TimeSliceRepository,
)
from quantos.synthesis import LLMGeneration
from quantos.time_slices import assemble_time_slice


EVALUATION_SCHEMA_VERSION = "quantos-v1-evaluation-v1"
EVALUATION_POLICY_VERSION = "v1-offline-strict:v1"
EVALUATION_FIXTURE_VERSION = "strict-ready-empty:v1"
CORE_FREEZE_TAG = "v0.1.0-core"
CORE_FREEZE_COMMIT = "46038d95d384a9e4a8ad04d7ffbcecf25e25e20e"
UNIVERSE = "shse_szse_a_share"


class EvaluationDataSource(str, Enum):
    REAL_HISTORICAL = "REAL_HISTORICAL"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"


@dataclass(frozen=True, slots=True)
class CandidateObservation:
    preparation: KnowledgePreparationResult
    evidence_count: int
    attribution_count: int


@dataclass(frozen=True, slots=True)
class DayObservation:
    trade_date: date
    data_source: EvaluationDataSource
    report: object
    daily_ref: ArtifactReference
    post_close_manifest: object
    post_close_manifest_ref: ArtifactReference
    post_close_time_slice_id: str
    candidates: tuple[CandidateObservation, ...]
    duration_ms: int
    pre_open_manifest: object | None = None
    pre_open_manifest_ref: ArtifactReference | None = None
    pre_open_time_slice_id: str | None = None
    pre_open_prior_daily_ref: ArtifactReference | None = None
    pre_open_retrieval_calls: int = 0
    pre_open_context_assembly_calls: int = 0
    pre_open_synthesis_calls: int = 0


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    summary: Mapping[str, object]
    runs: tuple[Mapping[str, object], ...]
    days: tuple[Mapping[str, object], ...]
    candidates: tuple[Mapping[str, object], ...]
    failure_cases: tuple[Mapping[str, object], ...]


class _FixtureFactory:
    """Local deterministic synthesis provider; never owns a network transport."""

    def __init__(self, failed_symbols: Sequence[str] = ()) -> None:
        self.calls = 0
        self.failed_symbols = frozenset(failed_symbols)

    def __call__(self, value):
        parent = self

        class Client:
            def generate_structured(self, **_kwargs):
                parent.calls += 1
                if value.symbol in parent.failed_symbols:
                    raise RuntimeError("synthetic evaluation failure")
                evidence = value.attribution_evidence
                refs = tuple(
                    f"K{item.retrieval_rank}"
                    for item in value.knowledge_context.historical_items
                )
                explanations = []
                used = []
                if evidence:
                    used = [evidence[0].evidence_id]
                    explanations = [{
                        "statement": "该事前证据与行情可能相关。",
                        "supporting_evidence_ids": used,
                        "limitations": ["不能证明因果。"],
                        "causal_status": "associated_not_proven",
                    }]
                payload = {
                    "symbol": value.symbol,
                    "mode": value.mode,
                    "evidence_summary": "存在严格 PIT 可见的事前证据。" if evidence else "",
                    "possible_explanations": explanations,
                    "contradicting_signals": [],
                    "post_event_notes": [],
                    "insufficient_evidence": not bool(explanations),
                    "limitations": ["离线确定性评估 fixture。"],
                    "used_evidence_ids": used,
                    "knowledge_background": (
                        [{"statement": "公司具有本地 canonical 背景资料。",
                          "knowledge_refs": list(refs)}]
                        if refs else []
                    ),
                    "retrospective_knowledge_context": [],
                }
                return LLMGeneration(
                    payload,
                    "fixture-local",
                    "deterministic-v1",
                    10,
                    5,
                    15,
                )

        return Client()


def run_synthetic_strict_evaluation(
    output_dir: Path | str,
    *,
    trading_days: int = 10,
    candidate_count: int = 2,
) -> EvaluationResult:
    """Run the frozen V1 chain against explicitly synthetic local fixtures."""
    if type(trading_days) is not int or trading_days < 1:
        raise ValueError("evaluation requires at least one trading day")
    if type(candidate_count) is not int or not 1 <= candidate_count <= 2:
        raise ValueError("synthetic evaluation supports one or two candidates")
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError("evaluation output session already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    artifact_root = Path(tempfile.mkdtemp(prefix="quantos-evaluation-artifacts-"))
    observations: list[DayObservation] = []
    previous = None
    try:
        for day in _trading_dates(date(2026, 8, 24), trading_days):
            built = _build_strict_day(
                artifact_root / day.isoformat(), day, candidate_count=candidate_count,
            )
            pre_open = None
            counters = {"retrieval": 0, "assembly": 0, "synthesis": 0}
            if previous is not None:
                with _instrument_pre_open_work() as counters:
                    pre_open = _build_pre_open(
                        artifact_root / day.isoformat(), day, previous,
                    )
            observations.append(DayObservation(
                trade_date=day,
                data_source=EvaluationDataSource.SYNTHETIC_FIXTURE,
                report=built["report"],
                daily_ref=built["daily_ref"],
                post_close_manifest=built["manifest"],
                post_close_manifest_ref=built["manifest_ref"],
                post_close_time_slice_id=built["time_slice_id"],
                candidates=built["candidates"],
                duration_ms=built["duration_ms"],
                pre_open_manifest=pre_open["manifest"] if pre_open else None,
                pre_open_manifest_ref=pre_open["manifest_ref"] if pre_open else None,
                pre_open_time_slice_id=pre_open["time_slice_id"] if pre_open else None,
                pre_open_prior_daily_ref=previous["daily_ref"] if pre_open else None,
                pre_open_retrieval_calls=counters["retrieval"],
                pre_open_context_assembly_calls=counters["assembly"],
                pre_open_synthesis_calls=counters["synthesis"],
            ))
            previous = built

        determinism_mismatches = 0
        with tempfile.TemporaryDirectory(prefix="quantos-evaluation-replay-") as temporary:
            for observation in observations[:2]:
                replay = _build_strict_day(
                    Path(temporary) / observation.trade_date.isoformat(),
                    observation.trade_date,
                    candidate_count=candidate_count,
                )
                original = _identity_vector(observation)[:-1]
                replay_vector = _identity_vector_from_build(replay)[:-1]
                determinism_mismatches += sum(
                    a != b for a, b in zip(original, replay_vector)
                )
                relocated_state_id = _relocated_state_id(
                    observation,
                    Path(temporary) / "relocated" / observation.trade_date.isoformat(),
                )
                determinism_mismatches += (
                    observation.post_close_manifest.knowledge_state.state_id
                    != relocated_state_id
                )

        result = evaluate_observations(
            observations,
            determinism_mismatch_count=determinism_mismatches,
        )
        write_evaluation_artifacts(result, output)
        return result
    finally:
        shutil.rmtree(artifact_root, ignore_errors=True)


def evaluate_observations(
    observations: Sequence[DayObservation],
    *,
    determinism_mismatch_count: int = 0,
) -> EvaluationResult:
    """Validate and aggregate run/day/candidate observations without core writes."""
    if not observations:
        raise ValueError("at least one evaluation day is required")
    ordered = tuple(sorted(observations, key=lambda item: item.trade_date))
    if len({item.trade_date for item in ordered}) != len(ordered):
        raise ValueError("evaluation trade dates must be unique")
    sources = {item.data_source.value for item in ordered}
    if len(sources) != 1:
        raise ValueError("real and fixture observations cannot share one headline result")

    runs: list[Mapping[str, object]] = []
    days: list[Mapping[str, object]] = []
    candidates: list[Mapping[str, object]] = []
    failures: list[Mapping[str, object]] = []
    operational = {status.value: 0 for status in KnowledgeOperationalStatus}
    pit_violations = 0
    causal_failures = 0
    provider_calls = 0
    cache_hits = 0
    total_input_tokens = 0
    total_cached_input_tokens = 0
    total_output_tokens = 0
    total_reasoning_tokens = 0
    total_tokens = 0

    for day_index, observed in enumerate(ordered):
        report = observed.report
        manifest = observed.post_close_manifest
        if (
            report.trade_date != observed.trade_date
            or manifest.run_context.target_trade_date != observed.trade_date
            or manifest.run_context.run_type is not RunType.POST_CLOSE
            or manifest.summary.report_id != report.report_id
        ):
            raise ValueError("evaluation day product/run relationship is invalid")
        state = manifest.knowledge_state
        if state is None:
            raise ValueError("evaluation requires an observed Knowledge state")
        operational[state.status.value] += 1
        provider_calls += int(report.synthesis_runtime["actual_llm_requests"])
        cache_hits += int(report.synthesis_runtime["cache_hits"])
        total_input_tokens += int(report.synthesis_runtime["input_tokens"])
        total_cached_input_tokens += int(report.synthesis_runtime["cached_input_tokens"])
        total_output_tokens += int(report.synthesis_runtime["output_tokens"])
        total_reasoning_tokens += int(report.synthesis_runtime["reasoning_tokens"])
        total_tokens += int(report.synthesis_runtime["total_tokens"])

        day_ready = 0
        day_empty = 0
        day_evidence = 0
        day_attribution = 0
        day_synthesis = 0
        by_symbol = {item.preparation.synthesis_input.symbol: item for item in observed.candidates}
        for brief in report.candidate_briefs:
            candidate = by_symbol[brief.symbol]
            value = candidate.preparation.synthesis_input
            context = candidate.preparation.knowledge_context
            candidate_pit = 0
            if context is not None:
                candidate_pit += sum(
                    item.available_at > value.candidate.market_event_time
                    for item in context.historical_items
                )
                candidate_pit += context.retrospective_item_count
            candidate_pit += sum(
                not item.strict_pit
                or item.available_at > value.candidate.market_event_time
                for item in value.attribution_evidence
            )
            pit_violations += candidate_pit
            status = candidate.preparation.status.value
            day_ready += status == KnowledgePreparationStatus.READY.value
            day_empty += status == KnowledgePreparationStatus.EMPTY.value
            day_evidence += candidate.evidence_count > 0
            day_attribution += candidate.attribution_count > 0
            synthesis_ok = brief.synthesis.status in {
                "GENERATED", "CACHE_HIT", "NO_EVIDENCE_FAST_PATH",
            }
            day_synthesis += synthesis_ok
            causal_status = "PASS"
            if brief.synthesis.validation_error_code in {
                "FORBIDDEN_CAUSAL_LANGUAGE", "INVALID_CAUSAL_STATUS",
            }:
                causal_status = "FAIL"
                causal_failures += 1
            row = {
                "trade_date": observed.trade_date.isoformat(),
                "data_source": observed.data_source.value,
                "symbol": brief.symbol,
                "candidate_rank": brief.rank,
                "signal_type": brief.signal_types[0],
                "market_event_time": value.candidate.market_event_time.isoformat(),
                "evidence_count": candidate.evidence_count,
                "attribution_count": candidate.attribution_count,
                "knowledge_preparation_status": status,
                "supplied_context_id": brief.synthesis.supplied_context_id,
                "k_ref_count": len(set(brief.synthesis.supplied_knowledge_refs)),
                "historical_knowledge_count": (
                    context.historical_item_count if context else 0
                ),
                "retrospective_knowledge_count": (
                    context.retrospective_item_count if context else 0
                ),
                "synthesis_status": brief.synthesis.status,
                "provider_invocation_count": (
                    1 if brief.synthesis.status == "GENERATED" else 0
                ),
                "input_tokens": None,
                "total_tokens": None,
                "synthesis_duration_ms": None,
                "causal_validation_status": causal_status,
                "pit_violation_count": candidate_pit,
                "final_inclusion_status": "INCLUDED",
            }
            candidates.append(row)
            if not synthesis_ok or candidate_pit or causal_status != "PASS":
                failure_stage = (
                    "PIT_VALIDATION" if candidate_pit else
                    "CAUSAL_VALIDATION" if causal_status != "PASS" else
                    "SYNTHESIS"
                )
                failures.append({
                    "trade_date": row["trade_date"],
                    "run_type": "POST_CLOSE",
                    "symbol": brief.symbol,
                    "candidate_rank": brief.rank,
                    "failure_stage": failure_stage,
                    "status": (
                        "PIT_VIOLATION" if candidate_pit else
                        brief.synthesis.validation_error_code or "SYNTHESIS_FAILED"
                    ),
                    "safe_reason_code": brief.synthesis.validation_error_code,
                    "report_id": report.report_id,
                    "context_id": brief.synthesis.supplied_context_id,
                    "daily_artifact_id": observed.daily_ref.artifact_id,
                })

        pre_open_success = (
            observed.pre_open_manifest is not None
            and observed.pre_open_manifest.is_success
            and observed.pre_open_prior_daily_ref is not None
            and observed.pre_open_manifest.knowledge_state.product_ref
            == observed.pre_open_prior_daily_ref
            and not any((
                observed.pre_open_retrieval_calls,
                observed.pre_open_context_assembly_calls,
                observed.pre_open_synthesis_calls,
            ))
        )
        pre_open_eligible = day_index > 0
        if not pre_open_eligible and observed.pre_open_manifest is not None:
            raise ValueError("first evaluation day cannot have a prior-Daily replay")
        if pre_open_eligible and not pre_open_success:
            pre_open_state = (
                observed.pre_open_manifest.knowledge_state
                if observed.pre_open_manifest is not None else None
            )
            failures.append({
                "trade_date": observed.trade_date.isoformat(),
                "run_type": RunType.PRE_OPEN.value,
                "symbol": None,
                "candidate_rank": None,
                "failure_stage": "PRE_OPEN_REUSE",
                "status": (
                    pre_open_state.status.value if pre_open_state is not None
                    else "PRE_OPEN_REUSE_MISSING"
                ),
                "safe_reason_code": (
                    pre_open_state.reason_code if pre_open_state is not None
                    else "PRE_OPEN_REUSE_MISSING"
                ),
                "report_id": report.report_id,
                "context_id": None,
                "daily_artifact_id": observed.daily_ref.artifact_id,
            })
        days.append({
            "trade_date": observed.trade_date.isoformat(),
            "data_source": observed.data_source.value,
            "candidate_count": len(report.candidate_briefs),
            "successful_candidate_count": day_synthesis,
            "evidence_covered_candidates": day_evidence,
            "attribution_ready_candidates": day_attribution,
            "knowledge_ready_candidates": day_ready,
            "knowledge_empty_candidates": day_empty,
            "synthesis_success_candidates": day_synthesis,
            "report_publication_status": "PASS",
            "daily_report_id": report.report_id,
            "daily_schema_version": report.schema_version,
            "post_close_success": manifest.is_success,
            "pre_open_reuse_status": (
                "NOT_APPLICABLE" if not pre_open_eligible
                else "PASS" if pre_open_success else "FAIL"
            ),
            "duration_ms": observed.duration_ms,
        })
        if observed.pre_open_manifest is not None:
            operational[
                observed.pre_open_manifest.knowledge_state.status.value
            ] += 1
            runs.append(_run_row(
                observed,
                observed.pre_open_manifest,
                observed.pre_open_manifest_ref,
            ))
        runs.append(_run_row(observed, manifest, observed.post_close_manifest_ref))

    candidate_count = len(candidates)
    day_count = len(days)
    pre_open_days = [row for row in days if row["pre_open_reuse_status"] != "NOT_APPLICABLE"]
    summary_identity = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "policy_version": EVALUATION_POLICY_VERSION,
        "core_freeze_commit": CORE_FREEZE_COMMIT,
        "core_freeze_tag": CORE_FREEZE_TAG,
        "data_source": next(iter(sources)),
        "mode": "strict_live",
        "run_types": ("POST_CLOSE", "PRE_OPEN"),
        "candidate_count": candidate_count,
        "fixture_version": EVALUATION_FIXTURE_VERSION,
        "trade_dates": tuple(row["trade_date"] for row in days),
    }
    summary = {
        **summary_identity,
        "evaluation_id": hashlib.sha256(canonical_json_bytes(summary_identity)).hexdigest(),
        "trading_days_evaluated": day_count,
        "run_count": len(runs),
        "candidate_count": candidate_count,
        "post_close_success_rate": _ratio(sum(row["post_close_success"] for row in days), day_count),
        "pre_open_replay_success_rate": _ratio(
            sum(row["pre_open_reuse_status"] == "PASS" for row in pre_open_days),
            len(pre_open_days),
        ),
        "evidence_coverage_rate": _ratio(
            sum(row["evidence_count"] > 0 for row in candidates), candidate_count,
        ),
        "attribution_coverage_rate": _ratio(
            sum(row["attribution_count"] > 0 for row in candidates), candidate_count,
        ),
        "knowledge_ready_rate": _ratio(
            sum(row["knowledge_preparation_status"] == "READY" for row in candidates),
            candidate_count,
        ),
        "knowledge_empty_rate": _ratio(
            sum(row["knowledge_preparation_status"] == "EMPTY" for row in candidates),
            candidate_count,
        ),
        "average_k_refs_per_candidate": _average(
            row["k_ref_count"] for row in candidates
        ),
        "average_evidence_refs_per_candidate": _average(
            row["evidence_count"] for row in candidates
        ),
        "average_attribution_refs_per_candidate": _average(
            row["attribution_count"] for row in candidates
        ),
        "evidence_to_attribution_dropoff_rate": _ratio(
            sum(row["evidence_count"] for row in candidates)
            - sum(row["attribution_count"] for row in candidates),
            sum(row["evidence_count"] for row in candidates),
        ),
        "synthesis_success_rate": _ratio(
            sum(row["synthesis_status"] != "FAIL" for row in candidates), candidate_count,
        ),
        "provider_invocation_count": provider_calls,
        "cache_hit_count": cache_hits,
        "total_input_tokens": total_input_tokens,
        "total_cached_input_tokens": total_cached_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_reasoning_tokens": total_reasoning_tokens,
        "total_tokens": total_tokens,
        "pit_violation_count": pit_violations,
        "causal_validation_failure_count": causal_failures,
        "synthesis_validation_failure_count": sum(
            row["synthesis_status"] == "FAIL" for row in candidates
        ),
        "knowledge_only_causal_rejection_count": sum(
            case["status"] == "FORBIDDEN_CAUSAL_LANGUAGE" for case in failures
        ),
        "unknown_reference_rejection_count": sum(
            case["status"] in {"UNKNOWN_EVIDENCE_ID", "UNKNOWN_KNOWLEDGE_REFERENCE"}
            for case in failures
        ),
        "daily_v1_count": sum(row["daily_schema_version"] == "daily-intelligence-v1"
                              for row in days),
        "daily_v2_count": sum(row["daily_schema_version"] == "daily-intelligence-v2"
                              for row in days),
        "pre_open_extra_retrieval_count": sum(
            item.pre_open_retrieval_calls for item in ordered
        ),
        "pre_open_extra_context_assembly_count": sum(
            item.pre_open_context_assembly_calls for item in ordered
        ),
        "pre_open_extra_synthesis_count": sum(
            item.pre_open_synthesis_calls for item in ordered
        ),
        "operational_failure_distribution": operational,
        "determinism_mismatch_count": determinism_mismatch_count,
        "average_runtime_ms": _average(row["duration_ms"] for row in days),
        "failure_case_count": len(failures),
        "knowledge_candidate_status_distribution": {
            status: sum(
                row["knowledge_preparation_status"] == status for row in candidates
            )
            for status in ("NOT_CONFIGURED", "EMPTY", "READY", "UNAVAILABLE",
                           "STALE", "CORRUPT", "FAILED_INTEGRITY", "FAILED")
        },
        "synthesis_candidate_status_distribution": {
            status: sum(row["synthesis_status"] == status for row in candidates)
            for status in ("GENERATED", "CACHE_HIT", "NO_EVIDENCE_FAST_PATH", "FAIL")
        },
        "real_deepseek_request_count": 0,
        "provider_network_request_count": 0,
        "embedding_provider_request_count": 0,
    }
    if sum(operational.values()) != len(runs):
        raise ValueError("operational status buckets do not cover every run")
    if sum(summary["knowledge_candidate_status_distribution"].values()) != candidate_count:
        raise ValueError("Knowledge status buckets do not cover every candidate")
    if sum(summary["synthesis_candidate_status_distribution"].values()) != candidate_count:
        raise ValueError("synthesis status buckets do not cover every candidate")
    if any(row["attribution_count"] > row["evidence_count"] for row in candidates):
        raise ValueError("attribution evidence cannot exceed selected Event Evidence")
    return EvaluationResult(summary, tuple(runs), tuple(days), tuple(candidates), tuple(failures))


def write_evaluation_artifacts(result: EvaluationResult, output_dir: Path | str) -> None:
    output = Path(output_dir).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    final_names = (
        "evaluation_summary.json", "failure_cases.json", "evaluation_runs.csv",
        "evaluation_days.csv", "evaluation_candidates.csv", "evaluation_report.md",
    )
    if output.exists():
        raise FileExistsError("evaluation result session is immutable")
    staging = Path(tempfile.mkdtemp(
        prefix=f".{output.name}.publish-", dir=output.parent,
    ))
    try:
        _write_json(staging / "evaluation_summary.json", result.summary)
        _write_json(staging / "failure_cases.json", {
            "schema_version": EVALUATION_SCHEMA_VERSION,
            "failure_cases": result.failure_cases,
        })
        _write_csv(staging / "evaluation_runs.csv", result.runs)
        _write_csv(staging / "evaluation_days.csv", result.days)
        _write_csv(staging / "evaluation_candidates.csv", result.candidates)
        (staging / "evaluation_report.md").write_text(
            _render_report(result.summary), encoding="utf-8",
        )
        _validate_staged_evaluation(staging, result)
        staging.rename(output)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@contextmanager
def _instrument_pre_open_work():
    """Count forbidden PRE_OPEN work at the actual imported call sites."""
    import quantos.knowledge_integration as integration
    import quantos.synthesis_runtime as synthesis_runtime

    counters = {"retrieval": 0, "assembly": 0, "synthesis": 0}
    original_retrieval = integration.retrieve_knowledge
    original_assembly = integration.assemble_knowledge_context
    original_synthesis = synthesis_runtime.synthesize_evidence

    def retrieval(*args, **kwargs):
        counters["retrieval"] += 1
        return original_retrieval(*args, **kwargs)

    def assembly(*args, **kwargs):
        counters["assembly"] += 1
        return original_assembly(*args, **kwargs)

    def synthesis(*args, **kwargs):
        counters["synthesis"] += 1
        return original_synthesis(*args, **kwargs)

    with (
        patch.object(integration, "retrieve_knowledge", retrieval),
        patch.object(integration, "assemble_knowledge_context", assembly),
        patch.object(synthesis_runtime, "synthesize_evidence", synthesis),
    ):
        yield counters


def _validate_staged_evaluation(
    staging: Path, result: EvaluationResult,
) -> None:
    """Validate the complete result set before publishing any result file."""
    summary = json.loads((staging / "evaluation_summary.json").read_text("utf-8"))
    failures = json.loads((staging / "failure_cases.json").read_text("utf-8"))
    expected_summary = json.loads(canonical_json_bytes(result.summary))
    if summary != expected_summary:
        raise ValueError("staged evaluation summary mismatch")
    if failures != {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "failure_cases": list(result.failure_cases),
    }:
        raise ValueError("staged evaluation failure cases mismatch")
    for name, expected in (
        ("evaluation_runs.csv", len(result.runs)),
        ("evaluation_days.csv", len(result.days)),
        ("evaluation_candidates.csv", len(result.candidates)),
    ):
        with (staging / name).open(encoding="utf-8", newline="") as stream:
            if len(tuple(csv.DictReader(stream))) != expected:
                raise ValueError("staged evaluation CSV row-count mismatch")
    report = (staging / "evaluation_report.md").read_text("utf-8")
    if (
        str(result.summary["evaluation_id"]) not in report
        or "NOT REAL-HISTORICAL PERFORMANCE" not in report
    ):
        raise ValueError("staged evaluation report contract mismatch")


def _build_strict_day(
    root: Path, trade_date: date, *, candidate_count: int = 2,
    failed_symbols: Sequence[str] = (),
) -> Mapping[str, object]:
    begun = runtime_time.monotonic()
    event = datetime.combine(trade_date, time(15), MARKET_TIMEZONE)
    generated = datetime.combine(trade_date, time(18), MARKET_TIMEZONE)
    symbols = ("601330.SH", "601331.SH")[:candidate_count]
    names = {
        symbol: ("评估公司一" if index == 0 else "评估公司二")
        for index, symbol in enumerate(symbols)
    }
    candidates = tuple(
        AnomalyCandidate(
            trade_date, generated, symbol, None, None, None,
            Decimal("10.25") - index, Decimal("4.2"), Decimal("3"), Decimal("2"),
            True, True, True, 3, "price_volume_amount", None, None, None,
            "unavailable", index + 1, index + 1, 20, generated,
        )
        for index, symbol in enumerate(symbols)
    )
    anomalies = tuple(
        StockAnomaly(
            trade_date, generated, candidate.symbol, candidate.return_pct,
            candidate.return_zscore, True, 100 + index, Decimal("50"),
            candidate.volume_ratio, True, Decimal("1000") + index,
            Decimal("500"), candidate.amount_ratio, True, 20,
            AnomalyStatus.OK, generated,
        )
        for index, candidate in enumerate(candidates)
    )
    flows = tuple(
        FundFlowEvidence(
            trade_date, generated, symbol, Decimal("-100"), Decimal("-0.1"),
            Decimal("20"), Decimal("-40"), Decimal("-20"), Decimal("-0.02"),
            Decimal("10"), Decimal("10"), "outflow", generated,
        )
        for symbol in symbols
    )
    evidence = tuple(
        WebSearchResult(
            f"fixture-{trade_date}-{index}", names[symbol], symbol,
            f"{names[symbol]}公告", "离线事前证据", f"fixture://{trade_date}/{symbol}",
            "fixture.local", event - timedelta(hours=1), "provider_reported",
            event - timedelta(minutes=30), event - timedelta(minutes=30),
            "fixture-local", f"record-{trade_date}-{index}",
            pit_mode="strict_live",
        )
        for index, symbol in enumerate(symbols)
    )
    event_selection = calibrate_event_evidence(
        evidence,
        link_stored_query_a_results(evidence),
        market_event_time=event,
    )
    attribution = calibrate_attribution_evidence(
        event_selection,
        candidates,
        flows,
        market_event_time=event,
        as_of_time=generated,
    )
    knowledge = KnowledgeRepository(root / "knowledge")
    knowledge.documents_root.mkdir(parents=True, exist_ok=True)
    document = canonicalize_knowledge_document(KnowledgeDocumentInput(
        document_family_id=f"fixture:{trade_date}:601330",
        document_version=1,
        source="fixture-local",
        source_type=KnowledgeSourceType.REFERENCE_DOCUMENT,
        title="评估公司一背景资料",
        content="评估公司一 601330.SH 本地 canonical 公司背景资料",
        temporal_class=KnowledgeTemporalClass.TIMELESS,
        published_at=event - timedelta(days=2),
        available_at=event - timedelta(days=1),
        retrieved_at=event - timedelta(days=1),
        effective_from=None,
        effective_to=None,
        entity_refs=(symbols[0],),
        provenance=KnowledgeProvenance(
            source_identifier=f"fixture:{trade_date}:601330",
            ingestion_adapter="evaluation-fixture",
            ingestion_adapter_version="v1",
            origin_reference=f"fixture://knowledge/{trade_date}/601330",
        ),
    ))
    knowledge.store(document)
    index = build_knowledge_lexical_index(knowledge, built_at=generated)
    indexes = KnowledgeLexicalIndexRepository(root / "indexes")
    indexes.store(index)
    contexts = KnowledgeContextRepository(root / "contexts")
    knowledge_settings = KnowledgeIntegrationSettings(
        enabled=True,
        lexical_index_version=index.lexical_index_version,
        retrieval_limit=10,
        context_max_items=10,
        context_max_chars=10_000,
    )
    preparations = tuple(
        prepare_candidate_synthesis_input(
            bundle,
            attribution.attribution_facts,
            mode="strict_live",
            company_name=names[bundle.candidate.symbol],
            market_event_time=event,
            research_corpus_cutoff=None,
            generated_at=generated,
            settings=knowledge_settings,
            knowledge_repository=knowledge,
            index_repository=indexes,
            context_repository=contexts,
        )
        for bundle in attribution.bundles
    )
    factory = _FixtureFactory(failed_symbols)
    settings = Settings.from_project_root(root / "product")
    market = MarketSnapshot(
        trade_date, generated, UNIVERSE, candidate_count, candidate_count,
        Decimal("1"), candidate_count, 0, 0, Decimal("1"),
        100 * candidate_count, Decimal("1000") * candidate_count,
        0, 0, 0, 0, (), (), 0, 0, (),
        generated,
    )
    report = generate_daily_report(
        market_snapshot=market,
        sector_snapshots=(),
        anomalies=anomalies,
        candidates=candidates,
        company_names=names,
        attribution=attribution,
        synthesis_inputs=tuple(item.synthesis_input for item in preparations),
        client_factory=factory,
        synthesis_repository=SynthesisRepository(settings),
        mode="strict_live",
        top_n=candidate_count,
        generated_at=generated,
        provenance={
            "quantos_git_commit": CORE_FREEZE_COMMIT,
            "synthesis_prompt_version": preparations[0].synthesis_input.prompt_version,
            "synthesis_schema_version": preparations[0].synthesis_input.schema_version,
        },
        model_provider="fixture-local",
        model_name="deterministic-v1",
    )
    json_path, _ = DailyReportRepository(settings).write(report)
    daily_ref = ArtifactReference(
        hashlib.sha256(json_path.read_bytes()).hexdigest(),
        str(json_path.resolve()),
        report.schema_version,
    )
    cutoff = generated + timedelta(minutes=1)
    artifacts, state = load_local_operational_artifacts(
        settings,
        target_trade_date=trade_date,
        as_of_time=cutoff,
        run_type=RunType.POST_CLOSE,
        mode="strict_live",
        top_n=candidate_count,
        universe_name=UNIVERSE,
        daily_report_ref=daily_ref,
    )
    readiness = collect_readiness(
        target_trade_date=trade_date,
        as_of_time=cutoff,
        run_type=RunType.POST_CLOSE,
        mode="strict_live",
        artifacts=artifacts,
        knowledge_state=state,
    )
    context = RunContext(
        trade_date, readiness.market_basis_trade_date, cutoff, RunType.POST_CLOSE,
        "strict_live", UNIVERSE, candidate_count, False, cutoff,
    )
    manifest = execute_run(
        context,
        readiness,
        handlers=local_artifact_handlers(readiness),
        clock=lambda: cutoff,
        git_commit=CORE_FREEZE_COMMIT,
    )
    manifest_path = RunRepository(settings).write(manifest)
    manifest_ref = ArtifactReference(
        context.run_id, str(manifest_path.resolve()), manifest.schema_version,
    )
    time_slice = assemble_time_slice(
        manifest,
        manifest_ref=manifest_ref,
        daily_report=report,
        daily_report_ref=daily_ref,
        generated_at=cutoff,
    )
    TimeSliceRepository(settings).write(time_slice)
    candidate_observations = tuple(
        CandidateObservation(
            preparation,
            len({
                item.result_id
                for item in attribution.event_selection.event_evidence
                if item.query_symbol == bundle.candidate.symbol
            }),
            len({item.result_id for item in bundle.strict_attribution_evidence}),
        )
        for preparation, bundle in zip(preparations, attribution.bundles)
    )
    return {
        "report": report,
        "daily_ref": daily_ref,
        "manifest": manifest,
        "manifest_ref": manifest_ref,
        "time_slice_id": time_slice.report_id,
        "candidates": candidate_observations,
        "duration_ms": max(0, round((runtime_time.monotonic() - begun) * 1000)),
        "document_ids": (document.document_id,),
        "chunk_ids": tuple(entry.chunk.chunk_id for entry in index.entries),
        "index_id": index.lexical_index_version,
        "request_ids": tuple(item.retrieval_request_id for item in preparations),
        "context_ids": tuple(item.context_id for item in preparations),
        "state_id": state.state_id,
    }


def _build_pre_open(root: Path, target_date: date, previous: Mapping[str, object]):
    report = previous["report"]
    daily_ref = previous["daily_ref"]
    settings = Settings.from_project_root(root / "pre-open")
    cutoff = datetime.combine(target_date, time(9), MARKET_TIMEZONE)
    top_n = report.candidate_summary["requested_top_n"]
    artifacts, state = load_local_operational_artifacts(
        settings,
        target_trade_date=target_date,
        market_basis_trade_date=report.trade_date,
        as_of_time=cutoff,
        run_type=RunType.PRE_OPEN,
        mode="strict_live",
        top_n=top_n,
        universe_name=UNIVERSE,
        daily_report_ref=daily_ref,
    )
    readiness = collect_readiness(
        target_trade_date=target_date,
        as_of_time=cutoff,
        run_type=RunType.PRE_OPEN,
        mode="strict_live",
        artifacts=artifacts,
        knowledge_state=state,
    )
    context = RunContext(
        target_date, report.trade_date, cutoff, RunType.PRE_OPEN,
        "strict_live", UNIVERSE, top_n, False, cutoff,
    )
    manifest = execute_run(
        context,
        readiness,
        handlers=local_artifact_handlers(readiness),
        clock=lambda: cutoff,
        git_commit=CORE_FREEZE_COMMIT,
    )
    manifest_path = RunRepository(settings).write(manifest)
    manifest_ref = ArtifactReference(
        context.run_id, str(manifest_path.resolve()), manifest.schema_version,
    )
    time_slice = assemble_time_slice(
        manifest,
        manifest_ref=manifest_ref,
        daily_report=report,
        daily_report_ref=daily_ref,
        generated_at=cutoff,
    )
    TimeSliceRepository(settings).write(time_slice)
    return {
        "manifest": manifest,
        "manifest_ref": manifest_ref,
        "time_slice_id": time_slice.report_id,
    }


def _identity_vector(observation: DayObservation):
    preparations = tuple(item.preparation for item in observation.candidates)
    contexts = tuple(item.knowledge_context for item in preparations)
    return (
        tuple(
            context.historical_items[0].document_id
            for context in contexts if context and context.historical_items
        ),
        tuple(
            context.historical_items[0].chunk_id
            for context in contexts if context and context.historical_items
        ),
        preparations[0].retrieval_result.lexical_index_version,
        tuple(item.retrieval_request_id for item in preparations),
        tuple(item.context_id for item in preparations),
        observation.report.report_id,
        observation.post_close_manifest.knowledge_state.state_id,
    )


def _identity_vector_from_build(built: Mapping[str, object]):
    return (
        built["document_ids"],
        built["chunk_ids"],
        built["index_id"],
        built["request_ids"],
        built["context_ids"],
        built["report"].report_id,
        built["state_id"],
    )


def _relocated_state_id(observation: DayObservation, root: Path) -> str:
    """Re-observe identical bytes at another path; path is not state identity."""
    root.mkdir(parents=True, exist_ok=True)
    source = Path(observation.daily_ref.path)
    relocated_json = root / source.name
    relocated_markdown = relocated_json.with_suffix(".md")
    shutil.copyfile(source, relocated_json)
    shutil.copyfile(source.with_suffix(".md"), relocated_markdown)
    reference = ArtifactReference(
        observation.daily_ref.artifact_id,
        str(relocated_json.resolve()),
        observation.daily_ref.version,
    )
    manifest = observation.post_close_manifest
    context = manifest.run_context
    _, state = load_local_operational_artifacts(
        Settings.from_project_root(root / "observer"),
        target_trade_date=observation.trade_date,
        as_of_time=context.as_of_time,
        run_type=RunType.POST_CLOSE,
        mode=context.mode,
        top_n=context.top_n,
        universe_name=context.universe_name,
        daily_report_ref=reference,
    )
    return state.state_id


def _run_row(observed: DayObservation, manifest, manifest_ref):
    state = manifest.knowledge_state
    return {
        "run_id": manifest.run_context.run_id,
        "trade_date": manifest.run_context.target_trade_date.isoformat(),
        "run_type": manifest.run_context.run_type.value,
        "mode": manifest.run_context.mode,
        "is_success": manifest.is_success,
        "duration_ms": manifest.summary.wall_clock_duration_ms,
        "daily_report_id": manifest.summary.report_id or state.source_report_id,
        "manifest_artifact_id": manifest_ref.artifact_id,
        "manifest_ref": Path(manifest_ref.path).name,
        "knowledge_operational_status": state.status.value,
        "failure_status": None if manifest.is_success else state.status.value,
        "safe_reason_code": state.reason_code,
    }


def _trading_dates(start: date, count: int) -> tuple[date, ...]:
    values = []
    current = start
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current += timedelta(days=1)
    return tuple(values)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _average(values) -> float:
    materialized = tuple(values)
    return round(sum(materialized) / len(materialized), 3) if materialized else 0.0


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=tuple(rows[0]),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _render_report(summary: Mapping[str, object]) -> str:
    operational = summary["operational_failure_distribution"]
    return "\n".join((
        "# QuantOS V1 Multi-day Offline Evaluation",
        "",
        "**SYNTHETIC FIXTURE — NOT REAL-HISTORICAL PERFORMANCE**",
        "",
        "This benchmark validates the evaluation harness, PIT plumbing, artifact chain,",
        "deterministic replay, and failure-safe product flow. It does not measure financial",
        "accuracy, alpha, investment return, or production latency.",
        "",
        f"- Evaluation ID: `{summary['evaluation_id']}`",
        f"- Data source: `{summary['data_source']}`",
        f"- Trading days: {summary['trading_days_evaluated']}",
        f"- Runs: {summary['run_count']}",
        f"- Candidates: {summary['candidate_count']}",
        f"- POST_CLOSE success: {summary['post_close_success_rate']:.2%}",
        f"- PRE_OPEN exact reuse: {summary['pre_open_replay_success_rate']:.2%}",
        f"- Evidence coverage: {summary['evidence_coverage_rate']:.2%}",
        f"- Attribution coverage: {summary['attribution_coverage_rate']:.2%}",
        f"- Candidate Knowledge READY: {summary['knowledge_ready_rate']:.2%}",
        f"- Candidate Knowledge EMPTY: {summary['knowledge_empty_rate']:.2%}",
        f"- Run-level operational READY: {operational['READY']}/{summary['run_count']}",
        "  (aggregate READY means at least one candidate is READY; other candidates may be EMPTY)",
        f"- Local fake-provider invocations: {summary['provider_invocation_count']}",
        f"- External provider network requests: {summary['provider_network_request_count']}",
        f"- PIT violations: {summary['pit_violation_count']}",
        f"- Determinism mismatches: {summary['determinism_mismatch_count']}",
        f"- Average local POST_CLOSE fixture-chain runtime: {summary['average_runtime_ms']} ms",
        "",
        "The 50/50 READY/EMPTY split is deliberately constructed by the fixture and is not",
        "a natural Knowledge hit rate. Runtime covers local candidate, Evidence, Attribution,",
        "retrieval/context, fake synthesis, storage, operational manifest, and POST_CLOSE",
        "TimeSlice work; it excludes PRE_OPEN replay and is not production latency.",
        "",
    ))
