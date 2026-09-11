"""Canonical deterministic scheduling contracts; no runtime or readiness state."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ._validation import require_aware, require_non_empty
from .run import RunType

SCHEDULE_SCHEMA_VERSION = "quantos-schedule-v1"
SCHEDULE_POLICY_VERSION = "quantos-schedule-policy-v1"


class ScheduleDecisionStatus(str, Enum):
    DUE = "DUE"
    NOT_DUE = "NOT_DUE"
    MISSED_WINDOW = "MISSED_WINDOW"
    NON_TRADING_DAY = "NON_TRADING_DAY"


@dataclass(frozen=True, slots=True)
class ScheduleSlot:
    slot_id: str
    run_type: RunType
    local_time: time
    timezone: str
    grace_period: timedelta
    enabled: bool = True

    def __post_init__(self):
        require_non_empty(self.slot_id, "slot_id")
        if not isinstance(self.run_type, RunType):
            raise ValueError("run_type must be an explicit RunType")
        if type(self.local_time) is not time or self.local_time.tzinfo is not None:
            raise ValueError("local_time must be a naive wall-clock time")
        _zone(self.timezone)
        if type(self.grace_period) is not timedelta or self.grace_period <= timedelta(0):
            raise ValueError("grace_period must be positive")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be boolean")


@dataclass(frozen=True, slots=True)
class SchedulePolicy:
    slots: tuple[ScheduleSlot, ...]
    timezone: str = "Asia/Shanghai"
    policy_version: str = SCHEDULE_POLICY_VERSION

    def __post_init__(self):
        _zone(self.timezone)
        if self.policy_version != SCHEDULE_POLICY_VERSION:
            raise ValueError("unsupported schedule policy")
        if type(self.slots) is not tuple or not self.slots:
            raise ValueError("schedule policy requires slots")
        identifiers = [slot.slot_id for slot in self.slots]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("duplicate slot_id")
        if any(slot.timezone != self.timezone for slot in self.slots):
            raise ValueError("all slots must use the policy timezone")
        windows = sorted(((_seconds(slot.local_time), _seconds(slot.local_time)
                           + slot.grace_period.total_seconds(), slot) for slot in self.slots),
                         key=lambda item: (item[0], item[1], item[2].slot_id))
        for start, end, slot in windows:
            if end > 24 * 60 * 60:
                raise ValueError("schedule window cannot cross local midnight")
            if slot.run_type == RunType.PRE_OPEN:
                if start >= _seconds(MARKET_OPEN_TIME) or end > _seconds(MARKET_OPEN_TIME):
                    raise ValueError("PRE_OPEN window must end by market open")
            elif slot.run_type == RunType.INTRADAY:
                if not any(_seconds(session_start) <= start and end <= _seconds(session_end)
                           for session_start, session_end in MARKET_SESSIONS):
                    raise ValueError("INTRADAY window must stay inside one market session")
            elif slot.run_type == RunType.POST_CLOSE:
                if start <= _seconds(MARKET_CLOSE_TIME):
                    raise ValueError("POST_CLOSE slot must be after market close")
        for (_, previous_end, _), (current_start, _, _) in zip(windows, windows[1:]):
            if current_start < previous_end:
                raise ValueError("schedule windows cannot overlap")


@dataclass(frozen=True, slots=True)
class ScheduledRunIntent:
    slot_id: str
    run_type: RunType
    scheduled_for: datetime
    evaluated_at: datetime
    target_trade_date: date
    market_basis_trade_date: date
    timezone: str

    def __post_init__(self):
        require_non_empty(self.slot_id, "slot_id")
        require_aware(self.scheduled_for, "scheduled_for")
        require_aware(self.evaluated_at, "evaluated_at")
        zone = _zone(self.timezone)
        if not isinstance(self.run_type, RunType):
            raise ValueError("run_type must be an explicit RunType")
        if type(self.target_trade_date) is not date or type(self.market_basis_trade_date) is not date:
            raise ValueError("intent trade dates must be dates")
        if self.scheduled_for.astimezone(zone).date() != self.target_trade_date:
            raise ValueError("scheduled_for must belong to target_trade_date")
        if self.evaluated_at.astimezone(zone).date() != self.target_trade_date:
            raise ValueError("evaluated_at must belong to target_trade_date")
        if self.evaluated_at < self.scheduled_for:
            raise ValueError("intent cannot be evaluated before scheduled_for")
        if self.run_type == RunType.POST_CLOSE:
            if self.market_basis_trade_date != self.target_trade_date:
                raise ValueError("POST_CLOSE basis must equal target trade date")
        elif self.market_basis_trade_date >= self.target_trade_date:
            raise ValueError("PRE_OPEN and INTRADAY basis must precede target trade date")


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    slot_id: str
    run_type: RunType
    status: ScheduleDecisionStatus
    scheduled_for: datetime
    window_end: datetime
    evaluated_at: datetime
    intent: ScheduledRunIntent | None = None

    def __post_init__(self):
        require_non_empty(self.slot_id, "slot_id")
        for field_name in ("scheduled_for", "window_end", "evaluated_at"):
            require_aware(getattr(self, field_name), field_name)
        if not isinstance(self.run_type, RunType) or not isinstance(self.status, ScheduleDecisionStatus):
            raise ValueError("explicit schedule decision enums required")
        if self.window_end <= self.scheduled_for:
            raise ValueError("invalid due window")
        expected = (ScheduleDecisionStatus.NOT_DUE if self.evaluated_at < self.scheduled_for
                    else ScheduleDecisionStatus.DUE if self.evaluated_at < self.window_end
                    else ScheduleDecisionStatus.MISSED_WINDOW)
        if self.status != ScheduleDecisionStatus.NON_TRADING_DAY and self.status != expected:
            raise ValueError("decision status disagrees with due window")
        if (self.status == ScheduleDecisionStatus.DUE) != (self.intent is not None):
            raise ValueError("only DUE decisions contain an intent")
        if self.intent is not None:
            if (self.intent.slot_id, self.intent.run_type, self.intent.scheduled_for,
                self.intent.evaluated_at) != (
                self.slot_id, self.run_type, self.scheduled_for, self.evaluated_at,
            ):
                raise ValueError("decision and intent disagree")


@dataclass(frozen=True, slots=True)
class ScheduleEvaluation:
    evaluated_at: datetime
    market_timezone: str
    local_trade_date: date
    decisions: tuple[ScheduleDecision, ...]
    schema_version: str = SCHEDULE_SCHEMA_VERSION
    policy_version: str = SCHEDULE_POLICY_VERSION

    def __post_init__(self):
        require_aware(self.evaluated_at, "evaluated_at")
        zone = _zone(self.market_timezone)
        if self.evaluated_at.astimezone(zone).date() != self.local_trade_date:
            raise ValueError("local_trade_date disagrees with evaluated_at")
        if self.schema_version != SCHEDULE_SCHEMA_VERSION or self.policy_version != SCHEDULE_POLICY_VERSION:
            raise ValueError("unsupported schedule schema or policy")
        identifiers = [decision.slot_id for decision in self.decisions]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("duplicate decisions")
        if any(decision.evaluated_at != self.evaluated_at for decision in self.decisions):
            raise ValueError("decision evaluated_at disagrees with evaluation")
        non_trading = [decision.status == ScheduleDecisionStatus.NON_TRADING_DAY
                       for decision in self.decisions]
        if any(non_trading) and not all(non_trading):
            raise ValueError("trading-day status must be consistent across slots")

    @property
    def intents(self) -> tuple[ScheduledRunIntent, ...]:
        return tuple(decision.intent for decision in self.decisions if decision.intent is not None)


MARKET_OPEN_TIME = time(9, 30)
MORNING_CLOSE_TIME = time(11, 30)
AFTERNOON_OPEN_TIME = time(13, 0)
MARKET_CLOSE_TIME = time(15, 0)
MARKET_SESSIONS = (
    (MARKET_OPEN_TIME, MORNING_CLOSE_TIME),
    (AFTERNOON_OPEN_TIME, MARKET_CLOSE_TIME),
)


def _seconds(value: time) -> float:
    return value.hour * 3600 + value.minute * 60 + value.second + value.microsecond / 1_000_000


def _zone(name: str) -> ZoneInfo:
    require_non_empty(name, "timezone")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone") from exc
