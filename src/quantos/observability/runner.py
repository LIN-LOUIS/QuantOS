"""Orchestration and health accounting for the Phase 1A market-data pipeline."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import date, datetime

from quantos.collectors import MarketDataProvider, RawMarketRecord
from quantos.normalizers import MarketNormalizer
from quantos.schemas import JobContext, MarketBar
from quantos.storage import MarketDataRepository

from .events import EventSink, EventType, RuntimeEvent
from .models import (
    ModuleResult,
    ResultStatus,
    RunHealthReport,
    ValidationCategory,
    ValidationResult,
)
from .validation import validate_market_bars


WallClock = Callable[[], datetime]
MonotonicClock = Callable[[], float]


class HealthPipelineRunner:
    """Run existing Phase 1A components without changing their business logic."""

    _MODULES = (
        "Collector",
        "Normalizer",
        "Raw Storage",
        "Market Storage",
        "PIT Query",
    )

    def __init__(
        self,
        *,
        collector: MarketDataProvider,
        normalizer: MarketNormalizer,
        repository: MarketDataRepository,
        wall_clock: WallClock,
        monotonic_clock: MonotonicClock = time.perf_counter,
        event_sinks: Sequence[EventSink] = (),
    ) -> None:
        self.collector = collector
        self.normalizer = normalizer
        self.repository = repository
        self.wall_clock = wall_clock
        self.monotonic_clock = monotonic_clock
        self.event_sinks = tuple(event_sinks)

    def run(
        self,
        *,
        job_context: JobContext,
        symbols: Sequence[str],
        frequency: str,
        start_time: datetime,
        end_time: datetime,
    ) -> RunHealthReport:
        report_started_at = self.wall_clock()
        report_started_tick = self.monotonic_clock()
        modules: list[ModuleResult] = []
        self._emit(
            RuntimeEvent(
                event_type=EventType.RUN_STARTED,
                job_context=job_context,
                emitted_at=report_started_at,
            )
        )

        collector_result, raw_records = self._collect(
            job_context, symbols, frequency, start_time, end_time
        )
        modules.append(collector_result)
        if collector_result.status is ResultStatus.FAILED:
            modules.extend(
                self._skipped_modules(
                    job_context,
                    self._MODULES[1:],
                    failed_dependency="Collector",
                )
            )
            return self._report(
                job_context, report_started_at, report_started_tick, modules
            )

        normalizer_result, bars = self._normalize(job_context, raw_records)
        modules.append(normalizer_result)
        if normalizer_result.status is ResultStatus.FAILED:
            modules.extend(
                self._skipped_modules(
                    job_context,
                    self._MODULES[2:],
                    failed_dependency="Normalizer",
                )
            )
            return self._report(
                job_context, report_started_at, report_started_tick, modules
            )

        raw_result = self._write_raw(job_context, raw_records)
        modules.append(raw_result)
        if raw_result.status is ResultStatus.FAILED:
            modules.extend(
                self._skipped_modules(
                    job_context,
                    self._MODULES[3:],
                    failed_dependency="Raw Storage",
                )
            )
            return self._report(
                job_context, report_started_at, report_started_tick, modules
            )

        market_result = self._write_market(job_context, bars)
        modules.append(market_result)
        if market_result.status is ResultStatus.FAILED:
            modules.extend(
                self._skipped_modules(
                    job_context,
                    self._MODULES[4:],
                    failed_dependency="Market Storage",
                )
            )
            return self._report(
                job_context, report_started_at, report_started_tick, modules
            )

        modules.append(
            self._query_pit(
                job_context,
                symbols=symbols,
                bars=bars,
                start_date=start_time.date(),
                end_date=end_time.date(),
            )
        )
        return self._report(job_context, report_started_at, report_started_tick, modules)

    def _collect(
        self,
        context: JobContext,
        symbols: Sequence[str],
        frequency: str,
        start_time: datetime,
        end_time: datetime,
    ) -> tuple[ModuleResult, list[RawMarketRecord]]:
        started_at, started_tick = self.wall_clock(), self.monotonic_clock()
        self._emit_started("Collector", context, started_at)
        try:
            records = self.collector.fetch_bars(
                symbols=symbols,
                frequency=frequency,
                start_time=start_time,
                end_time=end_time,
                as_of_time=context.as_of_time,
            )
        except Exception as exc:
            return (
                self._failed(
                    "Collector", context, started_at, started_tick, len(symbols), exc
                ),
                [],
            )
        warnings = (
            ("no raw market records observed; the requested period may be closed",)
            if not records
            else ()
        )
        return (
            self._completed(
                "Collector",
                context,
                started_at,
                started_tick,
                input_count=len(symbols),
                output_count=len(records),
                status=ResultStatus.WARNING if warnings else ResultStatus.SUCCESS,
                warnings=warnings,
                metrics={"frequency": frequency, "symbol_count": len(symbols)},
            ),
            records,
        )

    def _normalize(
        self, context: JobContext, records: list[RawMarketRecord]
    ) -> tuple[ModuleResult, list[MarketBar]]:
        started_at, started_tick = self.wall_clock(), self.monotonic_clock()
        self._emit_started("Normalizer", context, started_at)
        try:
            bars = self.normalizer.normalize_many(records)
            validations = validate_market_bars(bars, as_of_time=context.as_of_time)
        except Exception as exc:
            return (
                self._failed(
                    "Normalizer", context, started_at, started_tick, len(records), exc
                ),
                [],
            )
        status, warnings, errors = _validation_outcome(validations)
        return (
            self._completed(
                "Normalizer",
                context,
                started_at,
                started_tick,
                input_count=len(records),
                output_count=len(bars),
                status=status,
                warnings=warnings,
                errors=errors,
                validations=validations,
            ),
            bars,
        )

    def _write_raw(
        self, context: JobContext, records: list[RawMarketRecord]
    ) -> ModuleResult:
        started_at, started_tick = self.wall_clock(), self.monotonic_clock()
        self._emit_started("Raw Storage", context, started_at)
        try:
            written = self.repository.write_raw(records)
        except Exception as exc:
            return self._failed(
                "Raw Storage", context, started_at, started_tick, len(records), exc
            )
        return self._completed(
            "Raw Storage",
            context,
            started_at,
            started_tick,
            input_count=len(records),
            output_count=written,
            status=ResultStatus.SUCCESS,
            metrics={"duplicate_count": len(records) - written},
        )

    def _write_market(
        self, context: JobContext, bars: list[MarketBar]
    ) -> ModuleResult:
        started_at, started_tick = self.wall_clock(), self.monotonic_clock()
        self._emit_started("Market Storage", context, started_at)
        try:
            written = self.repository.write_bars(bars)
        except Exception as exc:
            return self._failed(
                "Market Storage", context, started_at, started_tick, len(bars), exc
            )
        return self._completed(
            "Market Storage",
            context,
            started_at,
            started_tick,
            input_count=len(bars),
            output_count=written,
            status=ResultStatus.SUCCESS,
            metrics={"duplicate_count": len(bars) - written},
        )

    def _query_pit(
        self,
        context: JobContext,
        *,
        symbols: Sequence[str],
        bars: list[MarketBar],
        start_date: date,
        end_date: date,
    ) -> ModuleResult:
        started_at, started_tick = self.wall_clock(), self.monotonic_clock()
        self._emit_started("PIT Query", context, started_at)
        try:
            queried: list[MarketBar] = []
            for symbol in symbols:
                queried.extend(
                    self.repository.read_by_symbol(
                        symbol,
                        as_of_time=context.as_of_time,
                        start_date=start_date,
                        end_date=end_date,
                    )
                )
            validations = list(
                validate_market_bars(queried, as_of_time=context.as_of_time)
            )
            expected_ids = {
                bar.source_record_id
                for bar in bars
                if bar.available_at <= context.as_of_time
            }
            actual_ids = {bar.source_record_id for bar in queried}
            mismatch_count = len(expected_ids.symmetric_difference(actual_ids))
            checked_count = max(len(expected_ids), len(actual_ids), mismatch_count)
            validations.append(
                ValidationResult(
                    rule_name="pit_query.expected_visibility",
                    category=ValidationCategory.SYSTEM_INTEGRITY,
                    status=(
                        ResultStatus.FAILED
                        if mismatch_count
                        else ResultStatus.SUCCESS
                    ),
                    checked_count=checked_count,
                    failed_count=mismatch_count,
                    message=(
                        "PIT query result did not match expected visible identities"
                        if mismatch_count
                        else None
                    ),
                    metrics={
                        "expected_count": len(expected_ids),
                        "actual_count": len(actual_ids),
                    },
                )
            )
        except Exception as exc:
            return self._failed(
                "PIT Query", context, started_at, started_tick, len(bars), exc
            )
        validation_tuple = tuple(validations)
        status, warnings, errors = _validation_outcome(validation_tuple)
        return self._completed(
            "PIT Query",
            context,
            started_at,
            started_tick,
            input_count=len(bars),
            output_count=len(queried),
            status=status,
            warnings=warnings,
            errors=errors,
            validations=validation_tuple,
            metrics={"excluded_count": len(bars) - len(queried)},
        )

    def _failed(
        self,
        name: str,
        context: JobContext,
        started_at: datetime,
        started_tick: float,
        input_count: int,
        exc: Exception,
    ) -> ModuleResult:
        return self._completed(
            name,
            context,
            started_at,
            started_tick,
            input_count=input_count,
            output_count=0,
            status=ResultStatus.FAILED,
            errors=(f"{type(exc).__name__}: {exc}",),
        )

    def _completed(
        self,
        name: str,
        context: JobContext,
        started_at: datetime,
        started_tick: float,
        *,
        input_count: int,
        output_count: int,
        status: ResultStatus,
        metrics: dict[str, object] | None = None,
        warnings: tuple[str, ...] = (),
        errors: tuple[str, ...] = (),
        validations: tuple[ValidationResult, ...] = (),
    ) -> ModuleResult:
        duration_ms = (self.monotonic_clock() - started_tick) * 1000
        result = ModuleResult(
            module_name=name,
            job_context=context,
            started_at=started_at,
            finished_at=self.wall_clock(),
            duration_ms=duration_ms,
            status=status,
            input_count=input_count,
            output_count=output_count,
            metrics=metrics or {},
            warnings=warnings,
            errors=errors,
            validations=validations,
        )
        self._emit_final(result)
        return result

    def _skipped_modules(
        self,
        context: JobContext,
        names: Sequence[str],
        *,
        failed_dependency: str,
    ) -> list[ModuleResult]:
        results = []
        for name in names:
            timestamp = self.wall_clock()
            result = ModuleResult(
                module_name=name,
                job_context=context,
                started_at=timestamp,
                finished_at=timestamp,
                duration_ms=0,
                status=ResultStatus.SKIPPED,
                input_count=0,
                output_count=0,
                dependency_failures=(failed_dependency,),
            )
            results.append(result)
            self._emit_final(result)
        return results

    def _report(
        self,
        context: JobContext,
        started_at: datetime,
        started_tick: float,
        modules: list[ModuleResult],
    ) -> RunHealthReport:
        duration_ms = (self.monotonic_clock() - started_tick) * 1000
        report = RunHealthReport.from_modules(
            job_context=context,
            started_at=started_at,
            finished_at=self.wall_clock(),
            duration_ms=duration_ms,
            modules=tuple(modules),
        )
        self._emit(
            RuntimeEvent(
                event_type=EventType.RUN_COMPLETED,
                job_context=context,
                emitted_at=report.finished_at,
                health_report=report,
            )
        )
        return report

    def _emit_started(
        self, module_name: str, context: JobContext, emitted_at: datetime
    ) -> None:
        self._emit(
            RuntimeEvent(
                event_type=EventType.MODULE_STARTED,
                job_context=context,
                emitted_at=emitted_at,
                module_name=module_name,
            )
        )

    def _emit_final(self, result: ModuleResult) -> None:
        event_type = {
            ResultStatus.SUCCESS: EventType.MODULE_COMPLETED,
            ResultStatus.WARNING: EventType.MODULE_WARNING,
            ResultStatus.FAILED: EventType.MODULE_FAILED,
            ResultStatus.SKIPPED: EventType.MODULE_SKIPPED,
        }[result.status]
        self._emit(
            RuntimeEvent(
                event_type=event_type,
                job_context=result.job_context,
                emitted_at=result.finished_at,
                module_name=result.module_name,
                module_result=result,
            )
        )

    def _emit(self, event: RuntimeEvent) -> None:
        for sink in self.event_sinks:
            try:
                sink(event)
            except Exception:
                # Observability must never change the financial pipeline result.
                continue


def _validation_outcome(
    validations: tuple[ValidationResult, ...],
) -> tuple[ResultStatus, tuple[str, ...], tuple[str, ...]]:
    errors = tuple(
        result.message or f"validation failed: {result.rule_name}"
        for result in validations
        if result.category is ValidationCategory.SYSTEM_INTEGRITY
        and result.status is ResultStatus.FAILED
    )
    warnings = tuple(
        result.message or f"validation warning: {result.rule_name}"
        for result in validations
        if result.status is ResultStatus.WARNING
    )
    if errors:
        return ResultStatus.FAILED, warnings, errors
    if warnings:
        return ResultStatus.WARNING, warnings, errors
    return ResultStatus.SUCCESS, warnings, errors
