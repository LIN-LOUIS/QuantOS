"""Synchronous, failure-isolated runtime events for pipeline observability."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from quantos.schemas import JobContext
from quantos.schemas._validation import require_aware

from .models import ModuleResult, ResultStatus, RunHealthReport


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    MODULE_STARTED = "module_started"
    MODULE_COMPLETED = "module_completed"
    MODULE_WARNING = "module_warning"
    MODULE_FAILED = "module_failed"
    MODULE_SKIPPED = "module_skipped"
    RUN_COMPLETED = "run_completed"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """A notification linked to the canonical run or module health record."""

    event_type: EventType
    job_context: JobContext
    emitted_at: datetime
    module_name: str | None = None
    module_result: ModuleResult | None = None
    health_report: RunHealthReport | None = None

    def __post_init__(self) -> None:
        require_aware(self.emitted_at, "emitted_at")
        if self.module_result is not None:
            if self.module_result.job_context is not self.job_context:
                raise ValueError("module event must share the same JobContext instance")
            if self.module_name != self.module_result.module_name:
                raise ValueError("module event name must match ModuleResult")
        if self.health_report is not None:
            if self.health_report.job_context is not self.job_context:
                raise ValueError("run event must share the same JobContext instance")


EventSink = Callable[[RuntimeEvent], None]


class ConsoleEventSink:
    """Render live module progress without replacing the final health report."""

    def __init__(self, emit: Callable[[str], None] = print) -> None:
        self._emit = emit

    def __call__(self, event: RuntimeEvent) -> None:
        if event.event_type is EventType.MODULE_STARTED:
            self._emit(
                f"[{event.emitted_at:%H:%M:%S}] "
                f"{event.module_name or '':<16} RUNNING"
            )
            return
        result = event.module_result
        if result is None:
            return
        label = {
            ResultStatus.SUCCESS: "PASS",
            ResultStatus.WARNING: "WARN",
            ResultStatus.FAILED: "FAIL",
            ResultStatus.SKIPPED: "SKIP",
        }[result.status]
        self._emit(
            f"[{event.emitted_at:%H:%M:%S}] {result.module_name:<16} "
            f"{label:<5} input={result.input_count} output={result.output_count} "
            f"{result.duration_ms:.1f}ms"
        )


class StructuredLoggingEventSink:
    """Emit safe structured fields through the Python standard logging API."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("quantos.observability.runtime")

    def __call__(self, event: RuntimeEvent) -> None:
        result = event.module_result
        report = event.health_report
        status = (
            result.status.value
            if result is not None
            else report.status.value
            if report is not None
            else "running"
        )
        level = (
            logging.ERROR
            if event.event_type is EventType.MODULE_FAILED
            else logging.WARNING
            if event.event_type is EventType.MODULE_WARNING
            else logging.INFO
        )
        self.logger.log(
            level,
            event.event_type.value,
            extra={
                "run_id": event.job_context.run_id,
                "module_name": event.module_name,
                "status": status,
                "duration_ms": result.duration_ms if result is not None else None,
                "input_count": result.input_count if result is not None else None,
                "output_count": result.output_count if result is not None else None,
            },
        )
