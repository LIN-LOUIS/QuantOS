from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pytest

from quantos.collectors import (
    EastmoneyMarketCollector,
    ProviderPayloadError,
    ProviderResponseError,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeTransport:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get_json(self, url: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        self.calls.append((url, params))
        return self.payload


def collector(payload: Mapping[str, Any]) -> tuple[EastmoneyMarketCollector, FakeTransport]:
    transport = FakeTransport(payload)
    instance = EastmoneyMarketCollector(
        transport=transport,
        clock=lambda: datetime(2026, 8, 26, 9, 36, tzinfo=SHANGHAI),
    )
    return instance, transport


def test_fetch_bars_returns_raw_records_without_network() -> None:
    instance, transport = collector(
        {
            "rc": 0,
            "data": {
                "klines": [
                    "2026-08-26 09:35,10.00,10.10,10.20,9.90,1000,10050.00,0,0,0,0"
                ]
            },
        }
    )

    records = instance.fetch_bars(
        symbols=["000001.SZ"],
        frequency="5m",
        start_time=datetime(2026, 8, 26, 9, 30, tzinfo=SHANGHAI),
        end_time=datetime(2026, 8, 26, 9, 35, tzinfo=SHANGHAI),
        as_of_time=datetime(2026, 8, 26, 9, 40, tzinfo=SHANGHAI),
    )

    assert len(records) == 1
    assert records[0].provider == "eastmoney"
    assert records[0].fields["close"] == "10.10"
    assert transport.calls[0][1]["secid"] == "0.000001"


def test_fetch_bars_drops_provider_rows_after_requested_cutoff() -> None:
    instance, _ = collector(
        {
            "rc": 0,
            "data": {
                "klines": [
                    "2026-08-26 09:35,10,10,10,10,1,10",
                    "2026-08-26 09:40,11,11,11,11,1,11",
                ]
            },
        }
    )
    records = instance.fetch_bars(
        symbols=["600000.SH"],
        frequency="5m",
        start_time=datetime(2026, 8, 26, 9, 30, tzinfo=SHANGHAI),
        end_time=datetime(2026, 8, 26, 9, 35, tzinfo=SHANGHAI),
        as_of_time=datetime(2026, 8, 26, 9, 40, tzinfo=SHANGHAI),
    )
    assert len(records) == 1


def test_fetch_bars_drops_provider_rows_before_requested_start() -> None:
    instance, _ = collector(
        {
            "rc": 0,
            "data": {
                "klines": [
                    "2026-08-26 09:30,9,9,9,9,1,9",
                    "2026-08-26 09:35,10,10,10,10,1,10",
                ]
            },
        }
    )
    records = instance.fetch_bars(
        symbols=["600000.SH"],
        frequency="5m",
        start_time=datetime(2026, 8, 26, 9, 35, tzinfo=SHANGHAI),
        end_time=datetime(2026, 8, 26, 9, 35, tzinfo=SHANGHAI),
        as_of_time=datetime(2026, 8, 26, 9, 40, tzinfo=SHANGHAI),
    )
    assert len(records) == 1
    assert records[0].fields["timestamp"] == "2026-08-26 09:35"


def test_fetch_bars_rejects_window_beyond_as_of_time() -> None:
    instance, _ = collector({"rc": 0, "data": {"klines": []}})
    with pytest.raises(ValueError, match="as_of_time"):
        instance.fetch_bars(
            symbols=["000001.SZ"],
            frequency="5m",
            start_time=datetime(2026, 8, 26, 9, 30, tzinfo=SHANGHAI),
            end_time=datetime(2026, 8, 26, 9, 40, tzinfo=SHANGHAI),
            as_of_time=datetime(2026, 8, 26, 9, 35, tzinfo=SHANGHAI),
        )


def test_provider_error_is_explicit() -> None:
    instance, _ = collector({"rc": 102, "data": None})
    with pytest.raises(ProviderResponseError, match="rc=102"):
        instance.fetch_bars(
            symbols=["000001.SZ"],
            frequency="5m",
            start_time=datetime(2026, 8, 26, 9, 30, tzinfo=SHANGHAI),
            end_time=datetime(2026, 8, 26, 9, 35, tzinfo=SHANGHAI),
            as_of_time=datetime(2026, 8, 26, 9, 40, tzinfo=SHANGHAI),
        )


def test_malformed_payload_is_explicit() -> None:
    instance, _ = collector({"rc": 0, "data": {}})
    with pytest.raises(ProviderPayloadError, match="klines"):
        instance.fetch_bars(
            symbols=["000001.SZ"],
            frequency="5m",
            start_time=datetime(2026, 8, 26, 9, 30, tzinfo=SHANGHAI),
            end_time=datetime(2026, 8, 26, 9, 35, tzinfo=SHANGHAI),
            as_of_time=datetime(2026, 8, 26, 9, 40, tzinfo=SHANGHAI),
        )
