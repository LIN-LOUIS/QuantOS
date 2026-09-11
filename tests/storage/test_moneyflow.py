from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from quantos.config import Settings
from quantos.schemas import MoneyFlowRecord
from quantos.storage import MoneyFlowRepository

SHANGHAI = ZoneInfo("Asia/Shanghai")


def flow(trade_date: date = date(2026, 8, 31), symbol: str = "600519.SH", **overrides) -> MoneyFlowRecord:
    values = {
        "symbol": symbol, "trade_date": trade_date,
        "buy_sm_volume": 100, "buy_sm_amount": Decimal("1000"),
        "sell_sm_volume": 50, "sell_sm_amount": Decimal("500"),
        "buy_md_volume": 200, "buy_md_amount": Decimal("2000"),
        "sell_md_volume": 100, "sell_md_amount": Decimal("1000"),
        "buy_lg_volume": 300, "buy_lg_amount": Decimal("3000"),
        "sell_lg_volume": 100, "sell_lg_amount": Decimal("1000"),
        "buy_elg_volume": 400, "buy_elg_amount": Decimal("4000"),
        "sell_elg_volume": 100, "sell_elg_amount": Decimal("1000"),
        "net_mf_volume": 700, "net_mf_amount": Decimal("7000"),
        "source": "tushare",
        "available_at": datetime.combine(trade_date, datetime.min.time(), tzinfo=SHANGHAI).replace(hour=19),
        "collected_at": datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
    }
    values.update(overrides)
    return MoneyFlowRecord(**values)


def test_moneyflow_storage_and_idempotency(tmp_path: Path) -> None:
    repository = MoneyFlowRepository(Settings.from_project_root(tmp_path))
    records = [flow(), flow(symbol="000001.SZ")]
    assert repository.write_records(records) == 2
    assert repository.write_records(records) == 0
    assert len(list(repository.settings.moneyflow_dir.rglob("*.parquet"))) == 1


def test_moneyflow_pit_before_and_after_1900(tmp_path: Path) -> None:
    repository = MoneyFlowRepository(Settings.from_project_root(tmp_path))
    repository.write_records([flow()])
    assert repository.read_by_date(
        date(2026, 8, 31), as_of_time=datetime(2026, 8, 31, 18, 59, tzinfo=SHANGHAI)
    ) == []
    assert len(repository.read_by_date(
        date(2026, 8, 31), as_of_time=datetime(2026, 8, 31, 19, 0, tzinfo=SHANGHAI)
    )) == 1


def test_moneyflow_history_is_ascending_and_limited(tmp_path: Path) -> None:
    repository = MoneyFlowRepository(Settings.from_project_root(tmp_path))
    start = date(2026, 7, 1)
    records = [flow(start + timedelta(days=index)) for index in range(25)]
    repository.write_records(records)
    history = repository.load_history(
        "600519.SH", end_date=records[-1].trade_date, lookback=20,
        as_of_time=datetime(2026, 9, 1, 23, 0, tzinfo=SHANGHAI),
    )
    assert [item.trade_date for item in history] == [item.trade_date for item in records[-20:]]


def test_moneyflow_history_excludes_future_date_and_pit_invisible(tmp_path: Path) -> None:
    repository = MoneyFlowRepository(Settings.from_project_root(tmp_path))
    invisible = replace(flow(date(2026, 8, 29)), available_at=datetime(2026, 9, 2, 19, 0, tzinfo=SHANGHAI))
    repository.write_records([invisible, flow(date(2026, 8, 30)), flow(date(2026, 9, 1))])
    history = repository.load_history(
        "600519.SH", end_date=date(2026, 8, 31), lookback=20,
        as_of_time=datetime(2026, 9, 1, 23, 0, tzinfo=SHANGHAI),
    )
    assert [item.trade_date for item in history] == [date(2026, 8, 30)]
