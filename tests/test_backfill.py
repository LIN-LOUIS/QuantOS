from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from quantos.backfill import backfill_market_bars
from quantos.collectors import RawMarketRecord

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2024, 2, 1, 20, 0, tzinfo=SHANGHAI)


def raw(trade_date: date, symbol: str = "600519.SH", **overrides) -> RawMarketRecord:
    fields = {
        "timestamp": trade_date.isoformat(),
        "available_at": f"{trade_date.isoformat()}T18:00:00+08:00",
        "frequency": "1d",
        "open": "100",
        "high": "110",
        "low": "90",
        "close": "105",
        "prev_close": "100",
        "volume": "12300",
        "amount": "456000",
    }
    fields.update(overrides)
    return RawMarketRecord(
        provider="tushare",
        symbol=symbol,
        provider_record_id=f"tushare:{symbol}:1d:{trade_date}",
        received_at=datetime(2024, 2, 1, 10, 0, tzinfo=SHANGHAI),
        fields=fields,
    )


class FakeProvider:
    def __init__(self, dates, records_by_date=None, failed_date=None) -> None:
        self.dates = list(dates)
        self.records_by_date = records_by_date or {}
        self.failed_date = failed_date
        self.calendar_calls = []
        self.daily_calls = []

    def fetch_trade_dates(self, **kwargs):
        self.calendar_calls.append(kwargs)
        return list(self.dates)

    def fetch_market_bars_by_trade_date(self, *, trade_date, as_of_time):
        self.daily_calls.append(trade_date)
        if trade_date == self.failed_date:
            raise OSError("provider unavailable")
        return list(self.records_by_date.get(trade_date, []))


class InMemoryRepository:
    def __init__(self) -> None:
        self.raw_ids = set()
        self.bar_ids = set()

    def write_raw(self, records):
        new = [record for record in records if record.provider_record_id not in self.raw_ids]
        self.raw_ids.update(record.provider_record_id for record in new)
        return len(new)

    def write_daily_bars(self, bars):
        new = [bar for bar in bars if bar.source_record_id not in self.bar_ids]
        self.bar_ids.update(bar.source_record_id for bar in new)
        return len(new)


def test_backfill_iterates_provider_trading_dates_in_order() -> None:
    dates = [date(2024, 1, 2), date(2024, 1, 3)]
    provider = FakeProvider(dates, {item: [raw(item)] for item in dates})
    report = backfill_market_bars(
        provider,
        InMemoryRepository(),
        start_date=dates[0],
        end_date=dates[-1],
        as_of_time=AS_OF,
    )
    assert report.trading_dates == tuple(dates)
    assert provider.daily_calls == dates
    assert report.raw_records == 2
    assert report.normalized_records == 2
    assert report.stored_records == 2


def test_duplicate_backfill_is_idempotent() -> None:
    trade_date = date(2024, 1, 2)
    provider = FakeProvider([trade_date], {trade_date: [raw(trade_date)]})
    repository = InMemoryRepository()
    first = backfill_market_bars(
        provider,
        repository,
        start_date=trade_date,
        end_date=trade_date,
        as_of_time=AS_OF,
    )
    second = backfill_market_bars(
        provider,
        repository,
        start_date=trade_date,
        end_date=trade_date,
        as_of_time=AS_OF,
    )
    assert first.stored_records == 1
    assert second.stored_records == 0
    assert second.duplicate_records == 1


def test_empty_trading_day_is_data_quality_information() -> None:
    trade_date = date(2024, 1, 2)
    report = backfill_market_bars(
        FakeProvider([trade_date]),
        InMemoryRepository(),
        start_date=trade_date,
        end_date=trade_date,
        as_of_time=AS_OF,
    )
    assert report.days[0].status == "empty"
    assert report.days[0].error_type is None


def test_invalid_record_is_counted_without_losing_valid_records() -> None:
    trade_date = date(2024, 1, 2)
    invalid = raw(trade_date, symbol="000001.SZ")
    invalid_fields = dict(invalid.fields)
    del invalid_fields["amount"]
    invalid = RawMarketRecord(
        provider=invalid.provider,
        symbol=invalid.symbol,
        provider_record_id=invalid.provider_record_id,
        received_at=invalid.received_at,
        fields=invalid_fields,
    )
    report = backfill_market_bars(
        FakeProvider([trade_date], {trade_date: [raw(trade_date), invalid]}),
        InMemoryRepository(),
        start_date=trade_date,
        end_date=trade_date,
        as_of_time=AS_OF,
    )
    assert report.raw_records == 2
    assert report.normalized_records == 1
    assert report.invalid_records == 1
    assert report.stored_records == 1


def test_partial_provider_failure_is_reported_and_next_date_continues() -> None:
    failed = date(2024, 1, 2)
    succeeded = date(2024, 1, 3)
    report = backfill_market_bars(
        FakeProvider(
            [failed, succeeded],
            {succeeded: [raw(succeeded)]},
            failed_date=failed,
        ),
        InMemoryRepository(),
        start_date=failed,
        end_date=succeeded,
        as_of_time=AS_OF,
    )
    assert report.days[0].status == "failed"
    assert report.days[0].error_type == "OSError"
    assert report.days[1].status == "success"
    assert report.stored_records == 1


def test_backfill_preserves_canonical_units_and_historical_availability() -> None:
    trade_date = date(2024, 1, 2)
    repository = InMemoryRepository()
    captured = []

    def write_daily_bars(bars):
        captured.extend(bars)
        return InMemoryRepository.write_daily_bars(repository, bars)

    repository.write_daily_bars = write_daily_bars
    backfill_market_bars(
        FakeProvider([trade_date], {trade_date: [raw(trade_date)]}),
        repository,
        start_date=trade_date,
        end_date=trade_date,
        as_of_time=AS_OF,
    )
    assert captured[0].volume == 12300
    assert str(captured[0].amount) == "456000"
    assert captured[0].available_at == datetime(
        2024, 1, 2, 18, 0, tzinfo=SHANGHAI
    )
    assert captured[0].collected_at == datetime(
        2024, 2, 1, 10, 0, tzinfo=SHANGHAI
    )
