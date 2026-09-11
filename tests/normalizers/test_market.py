from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantos.collectors import RawMarketRecord
from quantos.normalizers import MarketNormalizationError, MarketNormalizer

SHANGHAI = ZoneInfo("Asia/Shanghai")


def raw_record(**field_overrides: object) -> RawMarketRecord:
    fields: dict[str, object] = {
        "timestamp": "2026-08-26 09:35",
        "frequency": "5m",
        "open": "10.00",
        "high": "10.20",
        "low": "9.90",
        "close": "10.10",
        "volume": "1000",
        "amount": "10050.00",
    }
    fields.update(field_overrides)
    return RawMarketRecord(
        provider="eastmoney",
        symbol="000001.SZ",
        provider_record_id="000001.SZ:5m:2026-08-26T09:35:00+08:00",
        received_at=datetime(2026, 8, 26, 9, 36, tzinfo=SHANGHAI),
        fields=fields,
    )


def test_normalize_converts_provider_payload_to_market_bar() -> None:
    bar = MarketNormalizer().normalize(raw_record())

    assert bar.symbol == "000001.SZ"
    assert bar.close == Decimal("10.10")
    assert bar.volume == 1000
    assert str(bar.timestamp.tzinfo) == "Asia/Shanghai"
    assert bar.available_at == bar.timestamp
    assert not hasattr(bar, "fields")


def test_normalize_rejects_missing_provider_field() -> None:
    record = raw_record()
    fields = dict(record.fields)
    del fields["amount"]
    missing = RawMarketRecord(
        provider=record.provider,
        symbol=record.symbol,
        provider_record_id=record.provider_record_id,
        received_at=record.received_at,
        fields=fields,
    )
    with pytest.raises(MarketNormalizationError, match="amount"):
        MarketNormalizer().normalize(missing)


def test_normalize_rejects_non_numeric_price() -> None:
    with pytest.raises(MarketNormalizationError, match="close must be numeric"):
        MarketNormalizer().normalize(raw_record(close="not-a-price"))


def test_normalize_rejects_fractional_volume() -> None:
    with pytest.raises(MarketNormalizationError, match="volume must be an integer"):
        MarketNormalizer().normalize(raw_record(volume="1.5"))


def test_daily_bar_is_not_available_before_market_close() -> None:
    bar = MarketNormalizer().normalize(
        replace(
            raw_record(timestamp="2026-08-26", frequency="1d"),
            received_at=datetime(2026, 8, 26, 15, 1, tzinfo=SHANGHAI),
        )
    )

    assert bar.timestamp == datetime(2026, 8, 26, 15, 0, tzinfo=SHANGHAI)
    assert not bar.is_available_as_of(
        datetime(2026, 8, 26, 14, 59, tzinfo=SHANGHAI)
    )
