"""Small orchestration for repeatable market-wide moneyflow backfills."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, Sequence

from quantos.schemas import MoneyFlowRecord
from quantos.schemas._validation import require_aware


class MoneyFlowProvider(Protocol):
    def fetch_by_trade_date(self, *, trade_date: date, as_of_time: datetime) -> list[MoneyFlowRecord]: ...


class MoneyFlowStorage(Protocol):
    def write_records(self, records: Sequence[MoneyFlowRecord]) -> int: ...


@dataclass(frozen=True, slots=True)
class MoneyFlowBackfillDay:
    trade_date: date
    provider_record_count: int
    stored_count: int
    duplicate_count: int
    status: str
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class MoneyFlowBackfillReport:
    trading_dates: tuple[date, ...]
    days: tuple[MoneyFlowBackfillDay, ...]

    @property
    def raw_records(self) -> int:
        return sum(item.provider_record_count for item in self.days)

    @property
    def stored_records(self) -> int:
        return sum(item.stored_count for item in self.days)

    @property
    def duplicate_records(self) -> int:
        return sum(item.duplicate_count for item in self.days)


def backfill_moneyflows(
    provider: MoneyFlowProvider,
    repository: MoneyFlowStorage,
    *,
    trading_dates: Sequence[date],
    as_of_time: datetime,
) -> MoneyFlowBackfillReport:
    require_aware(as_of_time, "as_of_time")
    dates = tuple(sorted(set(trading_dates)))
    results: list[MoneyFlowBackfillDay] = []
    for trade_date in dates:
        try:
            records = provider.fetch_by_trade_date(trade_date=trade_date, as_of_time=as_of_time)
            stored = repository.write_records(records)
            results.append(MoneyFlowBackfillDay(
                trade_date=trade_date,
                provider_record_count=len(records),
                stored_count=stored,
                duplicate_count=len(records) - stored,
                status="success" if records else "empty",
            ))
        except Exception as exc:
            results.append(MoneyFlowBackfillDay(
                trade_date=trade_date, provider_record_count=0, stored_count=0,
                duplicate_count=0, status="failed", error_type=type(exc).__name__,
            ))
    return MoneyFlowBackfillReport(dates, tuple(results))
