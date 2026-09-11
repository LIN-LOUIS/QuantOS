import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from quantos.observability import (
    ConsoleEventSink,
    EventType,
    ModuleResult,
    ResultStatus,
    RuntimeEvent,
    StructuredLoggingEventSink,
)
from quantos.schemas import JobContext, JobType

SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_context() -> JobContext:
    return JobContext(
        run_id="event-run",
        job_type=JobType.MARKET_INGESTION,
        trade_date=date(2026, 8, 27),
        as_of_time=datetime(2026, 8, 27, 15, 0, tzinfo=SHANGHAI),
    )


def make_result(
    context: JobContext, status: ResultStatus = ResultStatus.SUCCESS
) -> ModuleResult:
    started = datetime(2026, 8, 27, 15, 1, tzinfo=SHANGHAI)
    return ModuleResult(
        module_name="Collector",
        job_context=context,
        started_at=started,
        finished_at=started + timedelta(milliseconds=4),
        duration_ms=4,
        status=status,
        input_count=1,
        output_count=4,
        errors=("token=secret-value",) if status is ResultStatus.FAILED else (),
    )


def test_console_sink_renders_started_and_completed_events() -> None:
    context = make_context()
    result = make_result(context)
    lines = []
    sink = ConsoleEventSink(lines.append)

    sink(
        RuntimeEvent(
            event_type=EventType.MODULE_STARTED,
            job_context=context,
            emitted_at=result.started_at,
            module_name="Collector",
        )
    )
    sink(
        RuntimeEvent(
            event_type=EventType.MODULE_COMPLETED,
            job_context=context,
            emitted_at=result.finished_at,
            module_name="Collector",
            module_result=result,
        )
    )

    assert "Collector        RUNNING" in lines[0]
    assert "Collector        PASS" in lines[1]
    assert "input=1 output=4 4.0ms" in lines[1]


def test_structured_logging_has_required_fields_without_sensitive_error(
    caplog,
) -> None:
    context = make_context()
    result = make_result(context, ResultStatus.FAILED)
    event = RuntimeEvent(
        event_type=EventType.MODULE_FAILED,
        job_context=context,
        emitted_at=result.finished_at,
        module_name="Collector",
        module_result=result,
    )

    with caplog.at_level(logging.INFO, logger="quantos.events.test"):
        StructuredLoggingEventSink(logging.getLogger("quantos.events.test"))(event)

    record = caplog.records[-1]
    assert record.message == "module_failed"
    assert record.run_id == "event-run"
    assert record.module_name == "Collector"
    assert record.status == "failed"
    assert record.duration_ms == 4
    assert record.input_count == 1
    assert record.output_count == 4
    assert "secret-value" not in caplog.text
