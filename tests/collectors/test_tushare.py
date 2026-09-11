from __future__ import annotations

import logging
import math
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantos.collectors import (
    ProviderConfigurationError,
    ProviderPayloadError,
    ProviderTransportError,
    TushareMarketCollector,
)
from quantos.normalizers import MarketNormalizer
from quantos.observability import HealthPipelineRunner, ResultStatus, RunHealth
from quantos.schemas import JobContext, JobType

SHANGHAI = ZoneInfo("Asia/Shanghai")
TOKEN_NAME = "TUSHARE_TOKEN"


def daily_row(**overrides):
    row = {
        "ts_code": "600519.SH",
        "trade_date": "20240102",
        "open": 1715.00,
        "high": 1718.19,
        "low": 1678.10,
        "close": 1685.01,
        "pre_close": 1726.00,
        "change": -40.99,
        "pct_chg": -2.375,
        "vol": 321.56,
        "amount": 5440.082548,
    }
    row.update(overrides)
    return row


class FakeClient:
    def __init__(
        self,
        response=None,
        error: Exception | None = None,
        stock_basic_response=None,
        stock_basic_error: Exception | None = None,
        trade_cal_response=None,
        trade_cal_error: Exception | None = None,
    ) -> None:
        self.response = [daily_row()] if response is None else response
        self.error = error
        self.stock_basic_response = (
            [stock_basic_row()]
            if stock_basic_response is None
            else stock_basic_response
        )
        self.stock_basic_error = stock_basic_error
        self.trade_cal_response = (
            [
                {"cal_date": "20240102", "is_open": "1"},
                {"cal_date": "20240103", "is_open": "1"},
            ]
            if trade_cal_response is None
            else trade_cal_response
        )
        self.trade_cal_error = trade_cal_error
        self.calls = []
        self.stock_basic_calls = []
        self.trade_cal_calls = []

    def daily(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response

    def stock_basic(self, **kwargs):
        self.stock_basic_calls.append(kwargs)
        if self.stock_basic_error:
            raise self.stock_basic_error
        return self.stock_basic_response

    def trade_cal(self, **kwargs):
        self.trade_cal_calls.append(kwargs)
        if self.trade_cal_error:
            raise self.trade_cal_error
        return self.trade_cal_response


def stock_basic_row(**overrides):
    row = {
        "ts_code": "600519.SH",
        "symbol": "600519",
        "name": "贵州茅台",
        "area": "贵州",
        "industry": "白酒",
        "market": "主板",
        "exchange": "SSE",
        "list_status": "L",
        "list_date": "20010827",
        "delist_date": "",
    }
    row.update(overrides)
    return row


def collector(client=None, *, environ=None):
    return TushareMarketCollector(
        client=client or FakeClient(),
        environ=environ or {TOKEN_NAME: "unit-test-placeholder"},
        clock=lambda: datetime(2024, 1, 5, 16, 0, tzinfo=SHANGHAI),
    )


def fetch(instance, *, symbols=("600519.SH",)):
    return instance.fetch_bars(
        symbols=symbols,
        frequency="1d",
        start_time=datetime(2024, 1, 2, 0, 0, tzinfo=SHANGHAI),
        end_time=datetime(2024, 1, 5, 23, 59, 59, tzinfo=SHANGHAI),
        as_of_time=datetime(2024, 1, 5, 23, 59, 59, tzinfo=SHANGHAI),
    )


def test_token_is_read_from_environment_mapping() -> None:
    captured = []
    TushareMarketCollector(
        environ={TOKEN_NAME: "unit-test-placeholder"},
        client_factory=lambda token: captured.append(token) or FakeClient(),
    )
    assert len(captured) == 1
    assert captured[0]


def test_missing_token_fails_safely() -> None:
    with pytest.raises(
        ProviderConfigurationError, match="TUSHARE_TOKEN is not configured"
    ):
        TushareMarketCollector(client=FakeClient(), environ={})


def test_token_does_not_enter_errors_or_logs(caplog) -> None:
    sensitive = "sensitive-test-value"
    instance = TushareMarketCollector(
        client=FakeClient(error=RuntimeError(sensitive)),
        environ={TOKEN_NAME: sensitive},
    )
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ProviderTransportError) as info:
            fetch(instance)
    assert sensitive not in str(info.value)
    assert sensitive not in caplog.text


def test_daily_response_maps_raw_fields_and_units() -> None:
    client = FakeClient()
    record = fetch(collector(client))[0]

    assert record.provider == "tushare"
    assert record.symbol == "600519.SH"
    assert record.fields["timestamp"] == "2024-01-02"
    assert record.fields["available_at"] == "2024-01-02T18:00:00+08:00"
    assert record.fields["open"] == "1715.0"
    assert record.fields["high"] == "1718.19"
    assert record.fields["low"] == "1678.1"
    assert record.fields["close"] == "1685.01"
    assert record.fields["prev_close"] == "1726.0"
    assert Decimal(record.fields["volume"]) == Decimal("32156")
    assert Decimal(record.fields["amount"]) == Decimal("5440082.548000")
    assert client.calls == [
        {
            "ts_code": "600519.SH",
            "start_date": "20240102",
            "end_date": "20240105",
        }
    ]


def test_tushare_daily_normalizes_with_post_close_availability() -> None:
    bar = MarketNormalizer().normalize(fetch(collector())[0])

    assert bar.timestamp == datetime(2024, 1, 2, 15, 0, tzinfo=SHANGHAI)
    assert bar.available_at == datetime(2024, 1, 2, 18, 0, tzinfo=SHANGHAI)
    assert bar.prev_close == Decimal("1726.0")
    assert bar.volume == 32156
    assert bar.amount == Decimal("5440082.548000")


def test_empty_daily_response_returns_empty_records() -> None:
    assert fetch(collector(FakeClient(response=[]))) == []


def test_malformed_daily_response_is_explicit() -> None:
    with pytest.raises(ProviderPayloadError, match="missing fields"):
        fetch(collector(FakeClient(response=[{"ts_code": "600519.SH"}])))


def test_tushare_sdk_error_is_safely_wrapped() -> None:
    with pytest.raises(ProviderTransportError, match="daily request failed") as info:
        fetch(collector(FakeClient(error=RuntimeError("SDK details"))))
    assert "SDK details" not in str(info.value)


def test_trade_calendar_returns_completed_open_dates_ascending() -> None:
    client = FakeClient(
        trade_cal_response=[
            {"cal_date": "20240103", "is_open": "1"},
            {"cal_date": "20240101", "is_open": "0"},
            {"cal_date": "20240102", "is_open": "1"},
        ]
    )
    result = collector(client).fetch_trade_dates(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 3),
        as_of_time=datetime(2024, 1, 5, 20, 0, tzinfo=SHANGHAI),
    )
    assert result == [date(2024, 1, 2), date(2024, 1, 3)]
    assert client.trade_cal_calls == [
        {
            "exchange": "SSE",
            "start_date": "20240101",
            "end_date": "20240103",
            "is_open": "1",
            "fields": "cal_date,is_open",
        }
    ]


def test_market_wide_daily_fetch_maps_sh_sz_and_skips_bse() -> None:
    client = FakeClient(
        response=[
            daily_row(),
            daily_row(ts_code="000001.SZ"),
            daily_row(ts_code="920001.BJ"),
        ]
    )
    records = collector(client).fetch_market_bars_by_trade_date(
        trade_date=date(2024, 1, 2),
        as_of_time=datetime(2024, 1, 5, 20, 0, tzinfo=SHANGHAI),
    )
    assert [record.symbol for record in records] == ["000001.SZ", "600519.SH"]
    assert client.calls == [{"trade_date": "20240102"}]
    bars = MarketNormalizer().normalize_many(records)
    assert all(bar.available_at.hour == 18 for bar in bars)
    assert bars[0].volume == 32156
    assert bars[0].amount == Decimal("5440082.548000")


def test_market_wide_daily_preserves_ingestion_and_historical_availability() -> None:
    record = collector().fetch_market_bars_by_trade_date(
        trade_date=date(2024, 1, 2),
        as_of_time=datetime(2024, 1, 5, 20, 0, tzinfo=SHANGHAI),
    )[0]
    assert record.received_at == datetime(2024, 1, 5, 16, 0, tzinfo=SHANGHAI)
    assert record.fields["available_at"] == "2024-01-02T18:00:00+08:00"


def test_market_wide_daily_error_does_not_leak_provider_details(caplog) -> None:
    sensitive = "token-like-sensitive-detail"
    instance = TushareMarketCollector(
        client=FakeClient(error=RuntimeError(sensitive)),
        environ={TOKEN_NAME: sensitive},
    )
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ProviderTransportError) as info:
            instance.fetch_market_bars_by_trade_date(
                trade_date=date(2024, 1, 2),
                as_of_time=datetime(2024, 1, 5, 20, 0, tzinfo=SHANGHAI),
            )
    assert sensitive not in str(info.value)
    assert sensitive not in caplog.text


def test_stock_basic_maps_listed_security_without_relabeling_industry() -> None:
    client = FakeClient()
    record = collector(client).fetch_listed_securities(
        as_of_time=datetime(2026, 8, 31, 12, 0, tzinfo=SHANGHAI)
    )[0]

    assert record.symbol == "600519.SH"
    assert record.name == "贵州茅台"
    assert record.exchange == "SHSE"
    assert record.status == "listed"
    assert record.list_date == date(2001, 8, 27)
    assert record.industry == "白酒"
    assert record.industry_classification == "Tushare stock_basic"
    assert client.stock_basic_calls[0]["list_status"] == "L"
    assert "delist_date" in client.stock_basic_calls[0]["fields"]


def test_stock_basic_supports_explicit_bse_mapping() -> None:
    client = FakeClient(
        stock_basic_response=[
            stock_basic_row(
                ts_code="430047.BJ",
                symbol="430047",
                name="诺思兰德",
                exchange="BSE",
                list_date="20201124",
            )
        ]
    )
    record = collector(client).fetch_listed_securities(
        as_of_time=datetime(2026, 8, 31, 12, 0, tzinfo=SHANGHAI)
    )[0]
    assert record.symbol == "430047.BJ"
    assert record.exchange == "BSE"


def test_stock_basic_handles_dataframe_nan_as_missing_optional_text() -> None:
    client = FakeClient(
        stock_basic_response=[stock_basic_row(industry=math.nan, delist_date=math.nan)]
    )
    record = collector(client).fetch_listed_securities(
        as_of_time=datetime(2026, 8, 31, 12, 0, tzinfo=SHANGHAI)
    )[0]
    assert record.industry is None
    assert record.industry_classification is None


def test_stock_basic_malformed_payload_is_explicit() -> None:
    client = FakeClient(stock_basic_response=[{"ts_code": "600519.SH"}])
    with pytest.raises(ProviderPayloadError, match="missing fields"):
        collector(client).fetch_listed_securities(
            as_of_time=datetime(2026, 8, 31, 12, 0, tzinfo=SHANGHAI)
        )


def test_stock_basic_token_and_provider_details_do_not_leak(caplog) -> None:
    sensitive = "sensitive-stock-basic-token"
    instance = TushareMarketCollector(
        client=FakeClient(stock_basic_error=RuntimeError(sensitive)),
        environ={TOKEN_NAME: sensitive},
    )
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ProviderTransportError) as info:
            instance.fetch_listed_securities(
                as_of_time=datetime(2026, 8, 31, 12, 0, tzinfo=SHANGHAI)
            )
    assert sensitive not in str(info.value)
    assert sensitive not in caplog.text


class InMemoryRepository:
    def __init__(self) -> None:
        self.bars = []

    def write_raw(self, records):
        return len(records)

    def write_bars(self, bars):
        self.bars.extend(bars)
        return len(bars)

    def read_by_symbol(self, symbol, *, as_of_time, start_date, end_date):
        return [
            bar
            for bar in self.bars
            if bar.symbol == symbol
            and start_date <= bar.timestamp.date() <= end_date
            and bar.available_at <= as_of_time
        ]


def run_pipeline(instance):
    ticks = iter(index / 1000 for index in range(100))
    runner = HealthPipelineRunner(
        collector=instance,
        normalizer=MarketNormalizer(),
        repository=InMemoryRepository(),
        wall_clock=lambda: datetime(2024, 1, 5, 23, 59, tzinfo=SHANGHAI),
        monotonic_clock=lambda: next(ticks),
    )
    context = JobContext(
        run_id="tushare-run",
        job_type=JobType.MARKET_INGESTION,
        trade_date=date(2024, 1, 5),
        as_of_time=datetime(2024, 1, 5, 23, 59, 59, tzinfo=SHANGHAI),
    )
    return runner.run(
        job_context=context,
        symbols=["600519.SH"],
        frequency="1d",
        start_time=datetime(2024, 1, 2, 0, 0, tzinfo=SHANGHAI),
        end_time=datetime(2024, 1, 5, 23, 59, 59, tzinfo=SHANGHAI),
    )


def test_tushare_pipeline_success() -> None:
    report = run_pipeline(collector())
    assert report.status is RunHealth.HEALTHY
    assert all(module.status is ResultStatus.SUCCESS for module in report.modules)


def test_tushare_failure_causes_fail_skip_unhealthy() -> None:
    report = run_pipeline(collector(FakeClient(error=RuntimeError("offline"))))
    assert report.status is RunHealth.UNHEALTHY
    assert report.modules[0].status is ResultStatus.FAILED
    assert all(module.status is ResultStatus.SKIPPED for module in report.modules[1:])
