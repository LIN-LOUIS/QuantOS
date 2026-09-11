from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from quantos.collectors import compare_market_bars
from quantos.schemas import MarketBar

SHANGHAI = ZoneInfo("Asia/Shanghai")


def bar(*, volume=3215600, amount=Decimal("5440082548"), source="tushare"):
    timestamp = datetime(2024, 1, 2, 15, 0, tzinfo=SHANGHAI)
    return MarketBar(
        symbol="600519.SH",
        timestamp=timestamp,
        frequency="1d",
        open=Decimal("1715.00"),
        high=Decimal("1718.19"),
        low=Decimal("1678.10"),
        close=Decimal("1685.01"),
        volume=volume,
        amount=amount,
        source=source,
        source_record_id=f"{source}-record",
        collected_at=datetime(2026, 8, 27, tzinfo=SHANGHAI),
        available_at=timestamp,
    )


def test_cross_provider_comparison_allows_small_decimal_differences() -> None:
    candidate = bar(amount=Decimal("5440000000"), source="eastmoney")
    assert compare_market_bars(bar(), candidate) == ()


def test_cross_provider_comparison_detects_unit_scale_errors() -> None:
    candidate = bar(
        volume=32156,
        amount=Decimal("5440082.548"),
        source="provider-with-wrong-units",
    )
    issues = compare_market_bars(bar(), candidate)
    assert "volume_mismatch" in issues
    assert "amount_mismatch" in issues
