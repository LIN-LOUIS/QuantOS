from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantos.collectors import RawMarketRecord
from quantos.normalizers import MarketNormalizer
from quantos.observability import (
    EventType,
    HealthPipelineRunner,
    ResultStatus,
    RunHealth,
)
from quantos.schemas import JobContext, JobType, MarketBar

SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_context() -> JobContext:
    return JobContext(
        run_id="health-run",
        job_type=JobType.MARKET_INGESTION,
        trade_date=date(2024, 1, 5),
        as_of_time=datetime(2024, 1, 5, 15, 0, tzinfo=SHANGHAI),
    )


def make_raw(*, zero_activity: bool = False) -> RawMarketRecord:
    return RawMarketRecord(
        provider="eastmoney",
        symbol="600519.SH",
        provider_record_id="600519.SH:1d:2024-01-02T00:00:00+08:00",
        received_at=datetime(2024, 1, 5, 16, 0, tzinfo=SHANGHAI),
        fields={
            "timestamp": "2024-01-02",
            "frequency": "1d",
            "open": "1715.00",
            "high": "1718.19",
            "low": "1678.10",
            "close": "1685.01",
            "volume": "0" if zero_activity else "32156",
            "amount": "0" if zero_activity else "5440082548.00",
        },
    )


class FakeCollector:
    def __init__(self, records=None, error: Exception | None = None) -> None:
        self.records = [make_raw()] if records is None else records
        self.error = error

    def fetch_bars(self, **kwargs):
        if self.error:
            raise self.error
        return list(self.records)


class FailingNormalizer:
    def normalize_many(self, records):
        raise ValueError("invalid provider price")


class FakeRepository:
    def __init__(
        self,
        *,
        raw_error: Exception | None = None,
        market_error: Exception | None = None,
        query_error: Exception | None = None,
        query_override: list[MarketBar] | None = None,
    ) -> None:
        self.raw_error = raw_error
        self.market_error = market_error
        self.query_error = query_error
        self.query_override = query_override
        self.bars: list[MarketBar] = []

    def write_raw(self, records):
        if self.raw_error:
            raise self.raw_error
        return len(records)

    def write_bars(self, bars):
        if self.market_error:
            raise self.market_error
        self.bars.extend(bars)
        return len(bars)

    def read_by_symbol(self, symbol, *, as_of_time, start_date, end_date):
        if self.query_error:
            raise self.query_error
        if self.query_override is not None:
            return list(self.query_override)
        return [
            bar
            for bar in self.bars
            if bar.symbol == symbol
            and start_date <= bar.timestamp.date() <= end_date
            and bar.available_at <= as_of_time
        ]


class StepClock:
    def __init__(self, start: float = 10.0, step: float = 0.001) -> None:
        self.value = start
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


def make_runner(
    *,
    collector=None,
    normalizer=None,
    repository=None,
    monotonic_clock=None,
    event_sinks=(),
) -> HealthPipelineRunner:
    return HealthPipelineRunner(
        collector=collector or FakeCollector(),
        normalizer=normalizer or MarketNormalizer(),
        repository=repository or FakeRepository(),
        wall_clock=lambda: datetime(2024, 1, 5, 16, 0, tzinfo=SHANGHAI),
        monotonic_clock=monotonic_clock or StepClock(),
        event_sinks=event_sinks,
    )


def run(runner: HealthPipelineRunner, context: JobContext | None = None):
    return runner.run(
        job_context=context or make_context(),
        symbols=["600519.SH"],
        frequency="1d",
        start_time=datetime(2024, 1, 2, 0, 0, tzinfo=SHANGHAI),
        end_time=datetime(2024, 1, 5, 15, 0, tzinfo=SHANGHAI),
    )


def test_all_modules_succeed() -> None:
    report = run(make_runner())

    assert report.status is RunHealth.HEALTHY
    assert [module.status for module in report.modules] == [ResultStatus.SUCCESS] * 5
    assert [(module.input_count, module.output_count) for module in report.modules] == [
        (1, 1),
        (1, 1),
        (1, 1),
        (1, 1),
        (1, 1),
    ]


def test_market_warning_degrades_run_without_system_failure() -> None:
    report = run(make_runner(collector=FakeCollector([make_raw(zero_activity=True)])))

    assert report.status is RunHealth.DEGRADED
    assert report.modules[1].status is ResultStatus.WARNING
    assert report.modules[4].status is ResultStatus.WARNING
    assert all(module.status is not ResultStatus.FAILED for module in report.modules)


def test_collector_failure_skips_all_dependencies() -> None:
    report = run(make_runner(collector=FakeCollector(error=OSError("offline"))))

    assert report.status is RunHealth.UNHEALTHY
    assert report.modules[0].status is ResultStatus.FAILED
    assert all(module.status is ResultStatus.SKIPPED for module in report.modules[1:])
    assert all(module.dependency_failures == ("Collector",) for module in report.modules[1:])


def test_normalizer_failure_skips_storage_and_query() -> None:
    report = run(make_runner(normalizer=FailingNormalizer()))

    assert report.modules[1].status is ResultStatus.FAILED
    assert all(module.status is ResultStatus.SKIPPED for module in report.modules[2:])


@pytest.mark.parametrize(
    ("repository", "failed_index", "skipped_indices"),
    [
        (FakeRepository(raw_error=OSError("raw write failed")), 2, (3, 4)),
        (FakeRepository(market_error=OSError("bar write failed")), 3, (4,)),
        (FakeRepository(query_error=OSError("query failed")), 4, ()),
    ],
)
def test_storage_and_query_failures_are_reported(
    repository, failed_index: int, skipped_indices: tuple[int, ...]
) -> None:
    report = run(make_runner(repository=repository))

    assert report.status is RunHealth.UNHEALTHY
    assert report.modules[failed_index].status is ResultStatus.FAILED
    assert all(report.modules[index].status is ResultStatus.SKIPPED for index in skipped_indices)


def test_pit_leak_is_failed_validation() -> None:
    original = MarketNormalizer().normalize(make_raw())
    leaked = MarketBar(
        symbol=original.symbol,
        timestamp=original.timestamp,
        frequency=original.frequency,
        open=original.open,
        high=original.high,
        low=original.low,
        close=original.close,
        volume=original.volume,
        amount=original.amount,
        source=original.source,
        source_record_id="leaked-future-revision",
        collected_at=original.collected_at,
        available_at=datetime(2024, 1, 6, 15, 0, tzinfo=SHANGHAI),
    )
    report = run(make_runner(repository=FakeRepository(query_override=[leaked])))

    pit = report.modules[4]
    assert pit.status is ResultStatus.FAILED
    assert report.status is RunHealth.UNHEALTHY
    assert any(
        result.rule_name == "market_bar.point_in_time"
        and result.status is ResultStatus.FAILED
        for result in pit.validations
    )


def test_empty_market_data_is_degraded_and_never_skipped() -> None:
    report = run(make_runner(collector=FakeCollector(records=[])))

    assert report.status is RunHealth.DEGRADED
    assert all(module.status is not ResultStatus.SKIPPED for module in report.modules)
    assert report.modules[0].output_count == 0
    assert report.modules[4].output_count == 0


def test_runner_uses_monotonic_duration_and_aware_wall_clock() -> None:
    report = run(make_runner(monotonic_clock=StepClock(step=0.002)))

    assert report.duration_ms > 0
    assert all(module.duration_ms > 0 for module in report.modules)
    assert report.started_at.utcoffset() is not None
    assert all(module.finished_at.utcoffset() is not None for module in report.modules)


def test_same_job_context_instance_flows_through_every_module() -> None:
    context = make_context()
    report = run(make_runner(), context)

    assert report.job_context is context
    assert all(module.job_context is context for module in report.modules)


def test_success_event_order_and_module_result_association() -> None:
    events = []
    report = run(make_runner(event_sinks=(events.append,)))

    assert [event.event_type for event in events] == [
        EventType.RUN_STARTED,
        EventType.MODULE_STARTED,
        EventType.MODULE_COMPLETED,
        EventType.MODULE_STARTED,
        EventType.MODULE_COMPLETED,
        EventType.MODULE_STARTED,
        EventType.MODULE_COMPLETED,
        EventType.MODULE_STARTED,
        EventType.MODULE_COMPLETED,
        EventType.MODULE_STARTED,
        EventType.MODULE_COMPLETED,
        EventType.RUN_COMPLETED,
    ]
    final_events = [event for event in events if event.module_result is not None]
    assert [event.module_result for event in final_events] == list(report.modules)
    assert events[-1].health_report is report


def test_failed_and_skipped_events_follow_collector_failure() -> None:
    events = []
    run(
        make_runner(
            collector=FakeCollector(error=OSError("offline")),
            event_sinks=(events.append,),
        )
    )

    assert [event.event_type for event in events] == [
        EventType.RUN_STARTED,
        EventType.MODULE_STARTED,
        EventType.MODULE_FAILED,
        EventType.MODULE_SKIPPED,
        EventType.MODULE_SKIPPED,
        EventType.MODULE_SKIPPED,
        EventType.MODULE_SKIPPED,
        EventType.RUN_COMPLETED,
    ]


def test_warning_emits_module_warning_event() -> None:
    events = []
    run(
        make_runner(
            collector=FakeCollector([make_raw(zero_activity=True)]),
            event_sinks=(events.append,),
        )
    )
    assert EventType.MODULE_WARNING in [event.event_type for event in events]


def test_failing_event_sink_does_not_break_pipeline_or_other_sinks() -> None:
    received = []

    def failing_sink(event) -> None:
        raise RuntimeError("monitor unavailable")

    report = run(make_runner(event_sinks=(failing_sink, received.append)))

    assert report.status is RunHealth.HEALTHY
    assert received[0].event_type is EventType.RUN_STARTED
    assert received[-1].event_type is EventType.RUN_COMPLETED
