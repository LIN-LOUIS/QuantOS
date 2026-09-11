"""Small deterministic orchestration for historical market-bar backfills."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, Sequence

from quantos.collectors import RawMarketRecord
from quantos.normalizers import MarketNormalizationError, MarketNormalizer
from quantos.schemas import MarketBar
from quantos.schemas._validation import require_aware


class HistoricalMarketProvider(Protocol):
    def fetch_trade_dates(
        self, *, start_date: date, end_date: date, as_of_time: datetime
    ) -> list[date]: ...

    def fetch_market_bars_by_trade_date(
        self, *, trade_date: date, as_of_time: datetime
    ) -> list[RawMarketRecord]: ...


class HistoricalMarketRepository(Protocol):
    def write_raw(self, records: Sequence[RawMarketRecord]) -> int: ...
    def write_daily_bars(self, bars: Sequence[MarketBar]) -> int: ...


@dataclass(frozen=True, slots=True)
class BackfillDayResult:
    trade_date: date
    provider_record_count: int
    normalized_count: int
    stored_count: int
    invalid_count: int
    duplicate_count: int
    status: str
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class BackfillReport:
    start_date: date
    end_date: date
    trading_dates: tuple[date, ...]
    days: tuple[BackfillDayResult, ...]

    @property
    def raw_records(self) -> int:
        return sum(day.provider_record_count for day in self.days)

    @property
    def normalized_records(self) -> int:
        return sum(day.normalized_count for day in self.days)

    @property
    def stored_records(self) -> int:
        return sum(day.stored_count for day in self.days)

    @property
    def invalid_records(self) -> int:
        return sum(day.invalid_count for day in self.days)

    @property
    def duplicate_records(self) -> int:
        return sum(day.duplicate_count for day in self.days)


def backfill_market_bars(
    provider: HistoricalMarketProvider,
    repository: HistoricalMarketRepository,
    *,
    start_date: date,
    end_date: date,
    as_of_time: datetime,
    normalizer: MarketNormalizer | None = None,
) -> BackfillReport:
    """Fetch, normalize, and idempotently persist each completed trade date."""

    require_aware(as_of_time, "as_of_time")
    if start_date > end_date:
        raise ValueError("start_date cannot be after end_date")
    converter = normalizer or MarketNormalizer()
    trading_dates = tuple(
        provider.fetch_trade_dates(
            start_date=start_date,
            end_date=end_date,
            as_of_time=as_of_time,
        )
    )
    results: list[BackfillDayResult] = []
    for trade_date in trading_dates:
        try:
            records = provider.fetch_market_bars_by_trade_date(
                trade_date=trade_date,
                as_of_time=as_of_time,
            )
            repository.write_raw(records)
            bars: list[MarketBar] = []
            invalid = 0
            for record in records:
                try:
                    bars.append(converter.normalize(record))
                except MarketNormalizationError:
                    invalid += 1
            stored = repository.write_daily_bars(bars)
            results.append(
                BackfillDayResult(
                    trade_date=trade_date,
                    provider_record_count=len(records),
                    normalized_count=len(bars),
                    stored_count=stored,
                    invalid_count=invalid,
                    duplicate_count=len(bars) - stored,
                    status="success" if records else "empty",
                )
            )
        except Exception as exc:
            results.append(
                BackfillDayResult(
                    trade_date=trade_date,
                    provider_record_count=0,
                    normalized_count=0,
                    stored_count=0,
                    invalid_count=0,
                    duplicate_count=0,
                    status="failed",
                    error_type=type(exc).__name__,
                )
            )
    return BackfillReport(
        start_date=start_date,
        end_date=end_date,
        trading_dates=trading_dates,
        days=tuple(results),
    )
