from datetime import datetime

import pytest

from quantos.collectors import GdeltNewsProvider, ProviderPayloadError
from quantos.config import MARKET_TIMEZONE


def _dt(day: int, hour: int = 12):
    return datetime(2026, 8, day, hour, tzinfo=MARKET_TIMEZONE)


def test_gdelt_maps_structured_metadata_and_explicit_query_window():
    calls = []

    def transport(url, params, timeout):
        calls.append((url, params, timeout))
        return {"articles": [{
            "url": "HTTPS://Example.COM/a?b=2&a=1#fragment",
            "title": "平安银行发布公告", "seendate": "20260831T070000Z",
            "domain": "Example.COM", "language": "Chinese",
        }]}

    provider = GdeltNewsProvider(transport=transport, clock=lambda: _dt(31, 20))
    records = provider.search_company_news(
        query_symbol="000001.SZ", query_term="平安银行",
        start_time=_dt(30, 15), end_time=_dt(31, 15), max_records=20,
    )

    assert len(records) == 1
    record = records[0]
    assert record.url == "https://example.com/a?a=1&b=2"
    assert record.content is None
    assert record.provider == "gdelt"
    assert record.timestamp_basis == "provider_reported"
    assert record.evidence_basis == "structured_news_metadata"
    assert record.pit_mode == "source_timestamp_proxy"
    params = calls[0][1]
    assert params["mode"] == "artlist"
    assert params["format"] == "json"
    assert params["query"] == '"平安银行"'
    assert params["maxrecords"] == "20"
    assert params["startdatetime"] and params["enddatetime"]


def test_gdelt_url_identity_deduplicates_same_article():
    article = {"url": "https://example.com/a", "title": "标题", "seendate": "20260831T070000Z"}
    provider = GdeltNewsProvider(
        transport=lambda *_: {"articles": [article, article]}, clock=lambda: _dt(31, 20)
    )
    records = provider.search_company_news(
        query_symbol="000001.SZ", query_term="平安银行",
        start_time=_dt(30), end_time=_dt(31),
    )
    assert len(records) == 1


def test_gdelt_request_pacing_is_sequential_and_injected():
    sleeps = []
    provider = GdeltNewsProvider(
        transport=lambda *_: {"articles": []}, clock=lambda: _dt(31, 20),
        request_interval_seconds=0.5, sleeper=sleeps.append,
    )
    for symbol in ("000001.SZ", "600519.SH"):
        provider.search_company_news(
            query_symbol=symbol, query_term="公司", start_time=_dt(30), end_time=_dt(31)
        )
    assert sleeps == [0.5]


def test_gdelt_rejects_malformed_payload_without_leaking_body():
    provider = GdeltNewsProvider(
        transport=lambda *_: {"articles": "secret response body"}, clock=lambda: _dt(31, 20)
    )
    with pytest.raises(ProviderPayloadError, match="malformed") as error:
        provider.search_company_news(
            query_symbol="000001.SZ", query_term="平安银行",
            start_time=_dt(30), end_time=_dt(31),
        )
    assert "secret" not in str(error.value)
