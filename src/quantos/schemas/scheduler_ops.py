"""Closed schemas for one-shot operational scheduler invocation."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ._validation import require_aware, require_non_empty
from .schedule import ScheduleDecisionStatus

LOCAL_CALENDAR_SCHEMA_VERSION = "quantos-local-trading-calendar-v1"
SCHEDULER_ONCE_SCHEMA_VERSION = "quantos-scheduler-once-v1"
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class SchedulerOnceOverallStatus(str, Enum):
    DRY_RUN = "DRY_RUN"
    SUCCESS = "SUCCESS"
    NO_WORK = "NO_WORK"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    EXECUTION_FAILED = "EXECUTION_FAILED"


class SchedulerOpsEventType(str, Enum):
    SCHEDULER_ONCE_STARTED = "scheduler_once_started"
    CALENDAR_LOADED = "calendar_loaded"
    SCHEDULE_EVALUATED = "schedule_evaluated"
    RUNTIME_ACTION = "runtime_action"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_FINISHED = "execution_finished"
    SCHEDULER_ONCE_NO_WORK = "scheduler_once_no_work"
    SCHEDULER_ONCE_COMPLETED = "scheduler_once_completed"
    SCHEDULER_ONCE_FAILED = "scheduler_once_failed"


@dataclass(frozen=True, slots=True)
class LocalTradingCalendarArtifact:
    schema_version: str
    market: str
    timezone: str
    coverage_start: date
    coverage_end: date
    trading_dates: tuple[date, ...]
    generated_at: datetime

    def __post_init__(self):
        if self.schema_version != LOCAL_CALENDAR_SCHEMA_VERSION:
            raise ValueError("unsupported local calendar schema")
        require_non_empty(self.market, "market")
        if self.timezone != "Asia/Shanghai":
            raise ValueError("V1 calendar timezone must be Asia/Shanghai")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown calendar timezone") from exc
        if type(self.coverage_start) is not date or type(self.coverage_end) is not date:
            raise ValueError("calendar coverage must use dates")
        if self.coverage_start > self.coverage_end:
            raise ValueError("calendar coverage is reversed")
        if type(self.trading_dates) is not tuple:
            raise ValueError("trading_dates must be a tuple")
        if not self.trading_dates:
            raise ValueError("calendar requires at least one trading date")
        if any(type(value) is not date for value in self.trading_dates):
            raise ValueError("trading_dates must contain dates")
        if tuple(sorted(set(self.trading_dates))) != self.trading_dates:
            raise ValueError("trading_dates must be unique and sorted")
        if any(value < self.coverage_start or value > self.coverage_end
               for value in self.trading_dates):
            raise ValueError("trading date outside calendar coverage")
        require_aware(self.generated_at, "generated_at")


@dataclass(frozen=True, slots=True)
class SchedulerOpsEvent:
    event_type: SchedulerOpsEventType
    emitted_at: datetime
    slot_id: str | None = None
    schedule_instance_id: str | None = None
    run_id: str | None = None
    attempt_number: int | None = None
    status: str | None = None
    safe_error_code: str | None = None

    def __post_init__(self):
        if not isinstance(self.event_type, SchedulerOpsEventType):
            raise ValueError("explicit scheduler event type required")
        require_aware(self.emitted_at, "emitted_at")
        if self.slot_id is not None:
            require_non_empty(self.slot_id, "slot_id")
        for name in ("schedule_instance_id", "run_id"):
            value = getattr(self, name)
            if value is not None:
                require_non_empty(value, name)
        if self.attempt_number is not None and (
            type(self.attempt_number) is not int or self.attempt_number < 1
        ):
            raise ValueError("attempt_number must be positive")
        for value in (self.status, self.safe_error_code):
            if value is not None and not _SAFE_CODE.fullmatch(value):
                raise ValueError("unsafe operational status code")


@dataclass(frozen=True, slots=True)
class SchedulerSlotResult:
    slot_id: str
    schedule_decision: ScheduleDecisionStatus
    runtime_action: str
    schedule_instance_id: str | None = None
    run_id: str | None = None
    attempt_number: int | None = None
    safe_status_code: str | None = None

    def __post_init__(self):
        require_non_empty(self.slot_id, "slot_id")
        if not isinstance(self.schedule_decision, ScheduleDecisionStatus):
            raise ValueError("explicit schedule decision required")
        if not _SAFE_CODE.fullmatch(self.runtime_action):
            raise ValueError("unsafe runtime action")
        if self.safe_status_code is not None and not _SAFE_CODE.fullmatch(self.safe_status_code):
            raise ValueError("unsafe operational status code")
        if self.attempt_number is not None and (
            type(self.attempt_number) is not int or self.attempt_number < 1
        ):
            raise ValueError("attempt_number must be positive")
        if self.run_id is not None and self.schedule_instance_id is None:
            raise ValueError("run_id requires schedule instance correlation")


@dataclass(frozen=True, slots=True)
class SchedulerOnceResult:
    schema_version: str
    evaluated_at: datetime
    mode: str
    calendar_market: str
    calendar_timezone: str
    calendar_coverage_start: date
    calendar_coverage_end: date
    dry_run: bool
    slot_results: tuple[SchedulerSlotResult, ...]
    overall_status: SchedulerOnceOverallStatus
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    events: tuple[SchedulerOpsEvent, ...]

    def __post_init__(self):
        if self.schema_version != SCHEDULER_ONCE_SCHEMA_VERSION:
            raise ValueError("unsupported scheduler once schema")
        for name in ("evaluated_at", "started_at", "finished_at"):
            require_aware(getattr(self, name), name)
        if self.finished_at < self.started_at:
            raise ValueError("scheduler invocation finished before it started")
        if self.mode not in {"research", "strict_live"}:
            raise ValueError("explicit mode required")
        if type(self.dry_run) is not bool:
            raise ValueError("dry_run must be boolean")
        if type(self.slot_results) is not tuple or type(self.events) is not tuple:
            raise ValueError("canonical operational collections must be tuples")
        if type(self.duration_ms) is not int or self.duration_ms < 0:
            raise ValueError("duration_ms must be a non-negative integer")
        if not isinstance(self.overall_status, SchedulerOnceOverallStatus):
            raise ValueError("explicit overall status required")
        if self.dry_run != (self.overall_status == SchedulerOnceOverallStatus.DRY_RUN):
            raise ValueError("dry-run status mismatch")
        if len({value.slot_id for value in self.slot_results}) != len(self.slot_results):
            raise ValueError("duplicate operational slot result")
