"""Pure schedule evaluation over an injected local trading calendar."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Protocol
from zoneinfo import ZoneInfo

from quantos.config import MARKET_TIMEZONE
from quantos.schemas._validation import require_aware
from quantos.schemas.run import RunContext, RunType
from quantos.schemas.schedule import (
    ScheduleDecision, ScheduleDecisionStatus, ScheduleEvaluation, SchedulePolicy,
    ScheduleSlot, ScheduledRunIntent,
)


class TradingCalendar(Protocol):
    """Local trading-day facts supplied to the pure evaluator."""

    def is_trading_day(self, value: date) -> bool: ...

    def previous_trading_day(self, value: date) -> date: ...


@dataclass(frozen=True, slots=True)
class LocalTradingCalendar:
    """Minimal deterministic adapter over explicit local/fixture trade dates."""

    trading_dates: tuple[date, ...]

    def __post_init__(self):
        if type(self.trading_dates) is not tuple or not self.trading_dates:
            raise ValueError("local trading calendar requires dates")
        if any(type(value) is not date for value in self.trading_dates):
            raise ValueError("trading calendar values must be dates")
        if tuple(sorted(set(self.trading_dates))) != self.trading_dates:
            raise ValueError("trading dates must be unique and sorted")

    @classmethod
    def from_dates(cls, values: Iterable[date]) -> "LocalTradingCalendar":
        return cls(tuple(sorted(set(values))))

    def is_trading_day(self, value: date) -> bool:
        return value in self.trading_dates

    def previous_trading_day(self, value: date) -> date:
        prior = [candidate for candidate in self.trading_dates if candidate < value]
        if not prior:
            raise ValueError("local calendar has no previous trading day")
        return prior[-1]


DEFAULT_SCHEDULE_POLICY = SchedulePolicy((
    ScheduleSlot("PRE_OPEN_0900", RunType.PRE_OPEN, datetime.min.time().replace(hour=9),
                 MARKET_TIMEZONE.key, timedelta(minutes=15)),
    ScheduleSlot("INTRADAY_1030", RunType.INTRADAY, datetime.min.time().replace(hour=10, minute=30),
                 MARKET_TIMEZONE.key, timedelta(minutes=15)),
    ScheduleSlot("POST_CLOSE_1600", RunType.POST_CLOSE, datetime.min.time().replace(hour=16),
                 MARKET_TIMEZONE.key, timedelta(minutes=15)),
    ScheduleSlot("POST_CLOSE_2000", RunType.POST_CLOSE, datetime.min.time().replace(hour=20),
                 MARKET_TIMEZONE.key, timedelta(minutes=15)),
))


def evaluate_slot(*, evaluated_at: datetime, calendar: TradingCalendar,
                  slot: ScheduleSlot) -> ScheduleDecision:
    """Evaluate one enabled slot using the exact half-open due interval."""
    require_aware(evaluated_at, "evaluated_at")
    zone = ZoneInfo(slot.timezone)
    local = evaluated_at.astimezone(zone)
    target = local.date()
    scheduled = datetime.combine(target, slot.local_time, zone)
    window_end = scheduled + slot.grace_period
    if not calendar.is_trading_day(target):
        status = ScheduleDecisionStatus.NON_TRADING_DAY
    elif local < scheduled:
        status = ScheduleDecisionStatus.NOT_DUE
    elif local < window_end:
        status = ScheduleDecisionStatus.DUE
    else:
        status = ScheduleDecisionStatus.MISSED_WINDOW
    intent = None
    if status == ScheduleDecisionStatus.DUE:
        basis = target if slot.run_type == RunType.POST_CLOSE else calendar.previous_trading_day(target)
        intent = ScheduledRunIntent(slot.slot_id, slot.run_type, scheduled, local, target, basis, slot.timezone)
    return ScheduleDecision(slot.slot_id, slot.run_type, status, scheduled, window_end, local, intent)


def evaluate_schedule(*, evaluated_at: datetime, calendar: TradingCalendar,
                      policy: SchedulePolicy = DEFAULT_SCHEDULE_POLICY) -> ScheduleEvaluation:
    """Pure evaluation; disabled slots are absent and no readiness is inspected."""
    require_aware(evaluated_at, "evaluated_at")
    zone = ZoneInfo(policy.timezone)
    local = evaluated_at.astimezone(zone)
    decisions = tuple(evaluate_slot(evaluated_at=local, calendar=calendar, slot=slot)
                      for slot in policy.slots if slot.enabled)
    return ScheduleEvaluation(local, policy.timezone, local.date(), decisions,
                              policy_version=policy.policy_version)


def run_context_from_intent(
    intent: ScheduledRunIntent, *, mode: str, universe_name: str, top_n: int,
    llm_allowed: bool, generated_at: datetime | None = None,
) -> RunContext:
    """Cross only the RunContext boundary; scheduled_for remains in the intent."""
    return RunContext(
        intent.target_trade_date,
        intent.market_basis_trade_date,
        intent.evaluated_at,
        intent.run_type,
        mode,
        universe_name,
        top_n,
        llm_allowed,
        generated_at or intent.evaluated_at,
    )
