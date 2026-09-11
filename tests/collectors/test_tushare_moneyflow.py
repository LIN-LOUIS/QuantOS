from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantos.collectors import (
    ProviderConfigurationError,
    ProviderTransportError,
    TushareMoneyFlowCollector,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 8, 31)


def row(**overrides):
    values = {"ts_code": "600519.SH", "trade_date": "20260831"}
    for prefix in ("buy_sm", "sell_sm", "buy_md", "sell_md", "buy_lg", "sell_lg", "buy_elg", "sell_elg"):
        values[f"{prefix}_vol"] = "1.5"
        values[f"{prefix}_amount"] = "2.5"
    values["net_mf_vol"] = "-3.5"
    values["net_mf_amount"] = "4.5"
    values.update(overrides)
    return values


class Client:
    def __init__(self, response=None, error=None):
        self.response = [row()] if response is None else response
        self.error = error
        self.calls = []

    def moneyflow(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def collector(client=None):
    return TushareMoneyFlowCollector(
        client=client or Client(),
        environ={"TUSHARE_TOKEN": "fake-token"},
        clock=lambda: datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
    )


def test_moneyflow_mapping_and_market_wide_request() -> None:
    client = Client()
    record = collector(client).fetch_by_trade_date(
        trade_date=TRADE_DATE,
        as_of_time=datetime(2026, 8, 31, 19, 0, tzinfo=SHANGHAI),
    )[0]
    assert client.calls == [{"trade_date": "20260831"}]
    assert record.symbol == "600519.SH"
    assert record.trade_date == TRADE_DATE
    assert record.source == "tushare"


def test_moneyflow_volume_hands_convert_to_shares_once() -> None:
    record = collector().fetch_by_trade_date(
        trade_date=TRADE_DATE, as_of_time=datetime(2026, 8, 31, 20, 0, tzinfo=SHANGHAI)
    )[0]
    assert record.buy_sm_volume == 150
    assert record.net_mf_volume == -350
    assert record.buy_sm_volume != 15000


def test_moneyflow_wan_yuan_converts_to_yuan_once() -> None:
    record = collector().fetch_by_trade_date(
        trade_date=TRADE_DATE, as_of_time=datetime(2026, 8, 31, 20, 0, tzinfo=SHANGHAI)
    )[0]
    assert record.buy_sm_amount == Decimal("25000.0")
    assert record.net_mf_amount == Decimal("45000.0")
    assert record.buy_sm_amount != Decimal("250000000")


def test_moneyflow_is_invisible_before_1900() -> None:
    with pytest.raises(ValueError, match="not available"):
        collector().fetch_by_trade_date(
            trade_date=TRADE_DATE,
            as_of_time=datetime(2026, 8, 31, 18, 59, tzinfo=SHANGHAI),
        )


def test_moneyflow_is_visible_at_1900_and_preserves_historical_availability() -> None:
    record = collector().fetch_by_trade_date(
        trade_date=TRADE_DATE,
        as_of_time=datetime(2026, 8, 31, 19, 0, tzinfo=SHANGHAI),
    )[0]
    assert record.available_at == datetime(2026, 8, 31, 19, 0, tzinfo=SHANGHAI)
    assert record.collected_at == datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)


def test_moneyflow_requires_token_without_disclosure() -> None:
    with pytest.raises(ProviderConfigurationError) as captured:
        TushareMoneyFlowCollector(client=Client(), environ={})
    assert "token-value" not in str(captured.value)


def test_moneyflow_provider_failure_is_typed_and_redacted() -> None:
    error = RuntimeError("provider unavailable")
    with pytest.raises(ProviderTransportError) as captured:
        collector(Client(error=error)).fetch_by_trade_date(
            trade_date=TRADE_DATE,
            as_of_time=datetime(2026, 8, 31, 20, 0, tzinfo=SHANGHAI),
        )
    assert "provider unavailable" not in str(captured.value)
