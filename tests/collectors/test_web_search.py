from datetime import datetime

import pytest

from quantos.collectors import ExaWebSearchProvider, TavilyWebSearchProvider
from quantos.collectors.base import ProviderConfigurationError, ProviderTransportError
from quantos.collectors.web_search import normalize_web_url, stable_web_result_id
from quantos.config import MARKET_TIMEZONE


NOW = datetime(2026, 9, 2, 10, tzinfo=MARKET_TIMEZONE)
START = datetime(2026, 8, 30, 15, tzinfo=MARKET_TIMEZONE)
END = datetime(2026, 9, 1, 3, tzinfo=MARKET_TIMEZONE)


def _search(provider):
    return provider.search(query="贵州茅台股份有限公司", query_symbol="600519.SH",
                           start_time=START, end_time=END, max_results=5)


@pytest.mark.parametrize("provider", [TavilyWebSearchProvider, ExaWebSearchProvider])
def test_missing_key(provider, monkeypatch):
    monkeypatch.delenv(provider.environment_variable, raising=False)
    with pytest.raises(ProviderConfigurationError):
        _search(provider())


def test_tavily_payload_mapping_and_request_shape():
    captured = {}
    def transport(url, headers, body, timeout):
        captured.update(url=url, headers=headers, body=body, timeout=timeout)
        return {"results": [{"title": "贵州茅台公告", "url": "HTTPS://X.TEST/a/?utm_source=z&x=1#f",
                             "content": "贵州茅台股份有限公司", "published_date": "2026-08-31T02:00:00Z", "score": 0.8}]}
    item = _search(TavilyWebSearchProvider(api_key="fake", transport=transport, clock=lambda: NOW,
                                           request_interval_seconds=0))[0]
    assert item.provider == "tavily" and item.query_symbol == "600519.SH"
    assert item.url == "https://x.test/a?x=1" and item.provider_score == 0.8
    assert item.available_at == NOW and not item.strict_pit
    assert captured["body"]["include_answer"] is False
    assert captured["body"]["include_raw_content"] is False


def test_exa_payload_mapping_nullable_timestamp_and_no_contents():
    captured = {}
    def transport(url, headers, body, timeout):
        captured.update(headers=headers, body=body)
        return {"results": [{"id": "native-1", "title": "贵州茅台", "url": "https://x.test/a", "score": 0.4}]}
    item = _search(ExaWebSearchProvider(api_key="fake", transport=transport, clock=lambda: NOW,
                                        request_interval_seconds=0))[0]
    assert item.published_at is None and item.timestamp_basis == "unknown"
    assert item.available_at == NOW and item.provider_record_id == "native-1"
    assert "contents" not in captured["body"]


@pytest.mark.parametrize("provider", [TavilyWebSearchProvider, ExaWebSearchProvider])
def test_duplicate_records_are_idempotent(provider):
    payload = {"results": [{"id": "one", "title": "A", "url": "https://X.test/a?utm_source=z"},
                           {"id": "two", "title": "A", "url": "https://x.test/a"}]}
    instance = provider(api_key="fake", transport=lambda *_: payload, clock=lambda: NOW,
                        request_interval_seconds=0)
    assert len(_search(instance)) == 1


def test_url_normalization_and_stable_cross_provider_identity():
    left = "HTTPS://Example.COM/a/?utm_source=x&b=2&a=1#frag"
    right = "https://example.com/a?a=1&b=2"
    assert normalize_web_url(left) == right
    assert stable_web_result_id(left) == stable_web_result_id(right)
    assert stable_web_result_id("https://example.com/a?id=1") != stable_web_result_id("https://example.com/a?id=2")


@pytest.mark.parametrize("provider", [TavilyWebSearchProvider, ExaWebSearchProvider])
def test_secret_never_appears_in_failure(provider):
    secret = "fake-secret-must-not-leak"
    def failure(*_):
        raise RuntimeError(secret)
    with pytest.raises(ProviderTransportError) as caught:
        _search(provider(api_key=secret, transport=failure, request_interval_seconds=0))
    assert secret not in str(caught.value) and secret not in repr(caught.value)


def test_naive_provider_timestamp_is_interpreted_as_market_local_time():
    payload = {"results": [{"title": "A", "url": "https://x.test/a", "publishedDate": "2026-08-31T14:00:00"}]}
    item = _search(ExaWebSearchProvider(api_key="fake", transport=lambda *_: payload,
                                        clock=lambda: NOW, request_interval_seconds=0))[0]
    assert item.published_at.utcoffset().total_seconds() == 8 * 3600
