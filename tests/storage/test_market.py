from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest

from quantos.anomalies import detect_stock_anomaly
from quantos.collectors import RawMarketRecord
from quantos.config import Settings
from quantos.schemas import MarketBar
from quantos.storage import MarketDataRepository, StorageError

SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_bar(
    symbol: str = "000001.SZ",
    minute: int = 35,
    available_minute: int | None = None,
) -> MarketBar:
    available_minute = minute if available_minute is None else available_minute
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2026, 8, 26, 9, minute, tzinfo=SHANGHAI),
        frequency="5m",
        open=Decimal("10.00"),
        high=Decimal("10.20"),
        low=Decimal("9.90"),
        close=Decimal("10.10"),
        volume=1000,
        amount=Decimal("10050.00"),
        source="eastmoney",
        source_record_id=f"{symbol}:5m:2026-08-26T09:{minute:02d}:00+08:00",
        collected_at=datetime(2026, 8, 26, 9, 45, tzinfo=SHANGHAI),
        available_at=datetime(2026, 8, 26, 9, available_minute, tzinfo=SHANGHAI),
    )


def make_daily_bar(
    trade_date: date,
    *,
    symbol: str = "600519.SH",
    available_at: datetime | None = None,
) -> MarketBar:
    timestamp = datetime.combine(
        trade_date, datetime.min.time(), tzinfo=SHANGHAI
    ).replace(hour=15)
    collected_at = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)
    if collected_at < timestamp:
        collected_at = timestamp.replace(hour=19)
    return MarketBar(
        symbol=symbol,
        timestamp=timestamp,
        frequency="1d",
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        prev_close=Decimal("100"),
        volume=12300,
        amount=Decimal("456000"),
        source="tushare",
        source_record_id=f"tushare:{symbol}:1d:{trade_date}",
        collected_at=collected_at,
        available_at=available_at
        or datetime.combine(trade_date, datetime.min.time(), tzinfo=SHANGHAI).replace(
            hour=18
        ),
    )


@pytest.fixture
def repository(tmp_path: Path) -> MarketDataRepository:
    return MarketDataRepository(Settings.from_project_root(tmp_path))


def test_write_raw_and_normalized_use_separate_parquet_trees(
    repository: MarketDataRepository,
) -> None:
    raw = RawMarketRecord(
        provider="eastmoney",
        symbol="000001.SZ",
        provider_record_id="raw-1",
        received_at=datetime(2026, 8, 26, 9, 36, tzinfo=SHANGHAI),
        fields={"vendor_close": "10.10"},
    )

    assert repository.write_raw([raw]) == 1
    assert repository.write_bars([make_bar()]) == 1
    assert list(repository.settings.raw_market_dir.rglob("*.parquet"))
    assert list(repository.settings.normalized_market_dir.rglob("*.parquet"))

    raw_file = next(repository.settings.raw_market_dir.rglob("*.parquet"))
    with duckdb.connect() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [str(raw_file)]
            ).fetchall()
        }
    assert "fields_json" in columns
    assert "close" not in columns


def test_write_is_idempotent_by_source_record_identity(
    repository: MarketDataRepository,
) -> None:
    bar = make_bar()
    assert repository.write_bars([bar, bar]) == 1
    assert repository.write_bars([bar]) == 0
    assert len(repository.read_by_date(date(2026, 8, 26), as_of_time=bar.available_at)) == 1


def test_read_only_repository_queries_parquet_and_rejects_writes(tmp_path) -> None:
    settings = Settings.from_project_root(tmp_path)
    writable = MarketDataRepository(settings)
    bar = make_bar()
    writable.write_bars([bar])
    read_only = MarketDataRepository(settings, read_only=True)

    assert read_only.read_by_symbol(
        bar.symbol, as_of_time=bar.available_at,
    ) == [bar]
    with pytest.raises(StorageError, match="read-only"):
        read_only.write_bars([bar])


def test_read_by_date_enforces_point_in_time(repository: MarketDataRepository) -> None:
    visible = make_bar(minute=35, available_minute=35)
    future = make_bar(symbol="600000.SH", minute=36, available_minute=41)
    repository.write_bars([visible, future])

    bars = repository.read_by_date(
        date(2026, 8, 26),
        as_of_time=datetime(2026, 8, 26, 9, 40, tzinfo=SHANGHAI),
    )

    assert [bar.symbol for bar in bars] == ["000001.SZ"]


def test_read_by_symbol_supports_date_range(repository: MarketDataRepository) -> None:
    repository.write_bars([make_bar(), make_bar(symbol="600000.SH")])

    bars = repository.read_by_symbol(
        "600000.SH",
        as_of_time=datetime(2026, 8, 26, 10, 0, tzinfo=SHANGHAI),
        start_date=date(2026, 8, 26),
        end_date=date(2026, 8, 26),
    )

    assert len(bars) == 1
    assert bars[0].symbol == "600000.SH"


def test_read_by_symbol_date_range_uses_market_timezone(
    repository: MarketDataRepository, monkeypatch,
) -> None:
    trade_date = date(2026, 8, 20)
    bar = replace(
        make_daily_bar(trade_date),
        timestamp=datetime.combine(
            trade_date, datetime.min.time(), tzinfo=SHANGHAI,
        ),
    )
    repository.write_daily_bars([bar])
    connect = repository._connect

    def connect_in_utc():
        connection = connect()
        connection.execute("SET TimeZone='UTC'")
        return connection

    monkeypatch.setattr(repository, "_connect", connect_in_utc)
    bars = repository.read_by_symbol(
        bar.symbol,
        as_of_time=datetime(2026, 9, 1, 20, 0, tzinfo=SHANGHAI),
        start_date=trade_date,
        end_date=trade_date,
    )

    assert bars == [bar]


def test_read_requires_timezone_aware_as_of(repository: MarketDataRepository) -> None:
    with pytest.raises(ValueError, match="as_of_time"):
        repository.read_by_date(
            date(2026, 8, 26), as_of_time=datetime(2026, 8, 26, 9, 40)
        )


def test_read_by_symbol_rejects_noncanonical_path_value(
    repository: MarketDataRepository,
) -> None:
    with pytest.raises(ValueError, match="canonical"):
        repository.read_by_symbol(
            "*",
            as_of_time=datetime(2026, 8, 26, 9, 40, tzinfo=SHANGHAI),
        )


def test_full_market_daily_write_uses_one_batch_file_and_is_idempotent(
    repository: MarketDataRepository,
) -> None:
    trade_date = date(2026, 8, 31)
    bars = [
        make_daily_bar(trade_date),
        make_daily_bar(trade_date, symbol="000001.SZ"),
    ]
    assert repository.write_daily_bars(bars) == 2
    assert repository.write_daily_bars(bars) == 0
    files = list(
        (repository.settings.normalized_market_dir / f"trade_date={trade_date}").rglob(
            "*.parquet"
        )
    )
    assert len(files) == 1
    assert len(repository.read_by_date(trade_date, as_of_time=bars[0].collected_at)) == 2


def test_history_query_is_ascending_and_applies_lookback(
    repository: MarketDataRepository,
) -> None:
    dates = [date(2026, 7, 1) + timedelta(days=index) for index in range(25)]
    repository.write_daily_bars([make_daily_bar(item) for item in dates])
    history = repository.load_market_bar_history(
        "600519.SH",
        end_date=dates[-1],
        lookback=20,
        as_of_time=datetime(2026, 9, 1, 20, 0, tzinfo=SHANGHAI),
    )
    assert [bar.timestamp.date() for bar in history] == dates[-20:]


def test_history_query_excludes_pit_invisible_and_future_trade_dates(
    repository: MarketDataRepository,
) -> None:
    visible_date = date(2026, 8, 28)
    target_date = date(2026, 8, 31)
    future_date = date(2026, 9, 1)
    invisible = make_daily_bar(
        date(2026, 8, 27),
        available_at=datetime(2026, 9, 2, 18, 0, tzinfo=SHANGHAI),
    )
    repository.write_daily_bars(
        [
            invisible,
            make_daily_bar(visible_date),
            make_daily_bar(target_date),
            make_daily_bar(future_date),
        ]
    )
    history = repository.load_market_bar_history(
        "600519.SH",
        end_date=target_date - timedelta(days=1),
        lookback=20,
        as_of_time=datetime(2026, 9, 1, 20, 0, tzinfo=SHANGHAI),
    )
    assert [bar.timestamp.date() for bar in history] == [visible_date]


def test_persisted_history_connects_to_stock_anomaly_without_target_leakage(
    repository: MarketDataRepository,
) -> None:
    target_date = date(2026, 8, 31)
    history_dates = [target_date - timedelta(days=index) for index in range(1, 21)]
    history_bars = [
        replace(
            make_daily_bar(item),
            close=Decimal(str(99 + index % 3)),
            source_record_id=f"tushare:600519.SH:1d:{item}",
        )
        for index, item in enumerate(history_dates)
    ]
    current = make_daily_bar(target_date)
    repository.write_daily_bars(history_bars + [current])
    history = repository.load_market_bar_history(
        "600519.SH",
        end_date=target_date - timedelta(days=1),
        lookback=20,
        as_of_time=datetime(2026, 9, 1, 20, 0, tzinfo=SHANGHAI),
    )
    anomaly = detect_stock_anomaly(
        current,
        history,
        as_of_time=datetime(2026, 9, 1, 20, 0, tzinfo=SHANGHAI),
    )
    assert anomaly.history_observations == 20
    assert all(bar.timestamp.date() < target_date for bar in history)
