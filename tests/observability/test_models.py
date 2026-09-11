from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from quantos.observability import (
    ModuleResult,
    ResultStatus,
    RunHealth,
    RunHealthReport,
    ValidationCategory,
    ValidationResult,
)
from quantos.schemas import JobContext, JobType

SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_context(run_id: str = "run-1") -> JobContext:
    return JobContext(
        run_id=run_id,
        job_type=JobType.MARKET_INGESTION,
        trade_date=date(2026, 8, 26),
        as_of_time=datetime(2026, 8, 26, 15, 0, tzinfo=SHANGHAI),
    )


def make_module(
    status: ResultStatus = ResultStatus.SUCCESS,
    *,
    context: JobContext | None = None,
) -> ModuleResult:
    started = datetime(2026, 8, 26, 15, 1, tzinfo=SHANGHAI)
    return ModuleResult(
        module_name="Collector",
        job_context=context or make_context(),
        started_at=started,
        finished_at=started + timedelta(milliseconds=12),
        duration_ms=11.5,
        status=status,
        input_count=1,
        output_count=4,
        warnings=("empty response",) if status is ResultStatus.WARNING else (),
        errors=("transport failed",) if status is ResultStatus.FAILED else (),
        dependency_failures=("Collector",) if status is ResultStatus.SKIPPED else (),
    )


def test_module_result_uses_job_context_as_run_source_of_truth() -> None:
    context = make_context()
    result = make_module(context=context)

    assert result.run_id == context.run_id
    assert result.as_of_time is context.as_of_time


def test_module_result_requires_aware_wall_clock_times() -> None:
    result = make_module()
    with pytest.raises(ValueError, match="started_at"):
        ModuleResult(
            module_name=result.module_name,
            job_context=result.job_context,
            started_at=result.started_at.replace(tzinfo=None),
            finished_at=result.finished_at,
            duration_ms=result.duration_ms,
            status=result.status,
            input_count=result.input_count,
            output_count=result.output_count,
        )


@pytest.mark.parametrize("duration", [-1.0, float("inf"), float("nan")])
def test_module_result_rejects_invalid_monotonic_duration(duration: float) -> None:
    result = make_module()
    with pytest.raises(ValueError, match="duration_ms"):
        ModuleResult(
            module_name=result.module_name,
            job_context=result.job_context,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_ms=duration,
            status=result.status,
            input_count=result.input_count,
            output_count=result.output_count,
        )


def test_skipped_module_requires_failed_dependency() -> None:
    result = make_module()
    with pytest.raises(ValueError, match="failed dependency"):
        ModuleResult(
            module_name="Normalizer",
            job_context=result.job_context,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_ms=0,
            status=ResultStatus.SKIPPED,
            input_count=0,
            output_count=0,
        )


def test_market_observation_cannot_be_system_failure() -> None:
    with pytest.raises(ValueError, match="cannot fail system health"):
        ValidationResult(
            rule_name="market.extreme_move",
            category=ValidationCategory.MARKET_OBSERVATION,
            status=ResultStatus.FAILED,
            checked_count=1,
            failed_count=1,
        )


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((ResultStatus.SUCCESS,), RunHealth.HEALTHY),
        ((ResultStatus.SUCCESS, ResultStatus.WARNING), RunHealth.DEGRADED),
        ((ResultStatus.SUCCESS, ResultStatus.FAILED), RunHealth.UNHEALTHY),
        ((ResultStatus.FAILED, ResultStatus.SKIPPED), RunHealth.UNHEALTHY),
    ],
)
def test_report_aggregates_module_health(
    statuses: tuple[ResultStatus, ...], expected: RunHealth
) -> None:
    context = make_context()
    modules = tuple(make_module(status, context=context) for status in statuses)
    report = RunHealthReport.from_modules(
        job_context=context,
        started_at=modules[0].started_at,
        finished_at=modules[-1].finished_at,
        duration_ms=20,
        modules=modules,
    )

    assert report.status is expected


def test_report_rejects_module_from_another_job_context() -> None:
    context = make_context()
    with pytest.raises(ValueError, match="same JobContext"):
        RunHealthReport.from_modules(
            job_context=context,
            started_at=datetime(2026, 8, 26, 15, 0, tzinfo=SHANGHAI),
            finished_at=datetime(2026, 8, 26, 15, 1, tzinfo=SHANGHAI),
            duration_ms=1,
            modules=(make_module(context=make_context("run-2")),),
        )
