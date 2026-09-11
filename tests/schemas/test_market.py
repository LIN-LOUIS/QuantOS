from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantos.schemas import MarketBar

SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_bar(**overrides: object) -> MarketBar:
    values = {
        "symbol": "000001.SZ",
        "timestamp": datetime(2026, 8, 26, 9, 35, tzinfo=SHANGHAI),
        "frequency": "5m",
        "open": Decimal("10.00"),
        "high": Decimal("10.20"),
        "low": Decimal("9.90"),
        "close": Decimal("10.10"),
        "volume": 1000,
        "amount": Decimal("10050.00"),
        "source": "test",
        "source_record_id": "record-1",
        "collected_at": datetime(2026, 8, 26, 9, 36, tzinfo=SHANGHAI),
        "available_at": datetime(2026, 8, 26, 9, 36, tzinfo=SHANGHAI),
    }
    values.update(overrides)
    return MarketBar(**values)


def test_market_bar_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timestamp"):
        make_bar(timestamp=datetime(2026, 8, 26, 9, 35))


def test_market_bar_rejects_invalid_ohlc() -> None:
    with pytest.raises(ValueError, match="high"):
        make_bar(high=Decimal("9.95"))


def test_market_bar_enforces_point_in_time_availability() -> None:
    bar = make_bar()
    assert not bar.is_available_as_of(
        datetime(2026, 8, 26, 9, 35, 59, tzinfo=SHANGHAI)
    )
    assert bar.is_available_as_of(
        datetime(2026, 8, 26, 9, 36, tzinfo=SHANGHAI)
    )
