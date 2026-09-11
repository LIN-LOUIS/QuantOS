from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from quantos.collectors import ProviderConfigurationError, ProviderTransportError, TushareNewsProvider

SHANGHAI = ZoneInfo("Asia/Shanghai")
COLLECTED = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)


class Client:
    def __init__(self, fail_news=False, fail_anns=False):
        self.fail_news = fail_news
        self.fail_anns = fail_anns

    def news(self, **kwargs):
        if self.fail_news:
            raise RuntimeError("secret provider detail")
        return [{
            "datetime": "2026-08-31 14:00:00", "title": "贵州茅台发布公告",
            "content": "正文", "channels": "公司,市场", "src": "sina",
        }]

    def anns_d(self, **kwargs):
        if self.fail_anns:
            raise RuntimeError("secret provider detail")
        return [{
            "ann_date": kwargs["ann_date"], "ts_code": "600519.SH",
            "name": "贵州茅台", "title": "关于回购的公告", "url": "https://example.test/a",
            "rec_time": f"{kwargs['ann_date']}090000",
        }]


def provider(client=None):
    return TushareNewsProvider(
        client=client or Client(), environ={"TUSHARE_TOKEN": "fake-token"},
        clock=lambda: COLLECTED,
    )


def test_tushare_news_mapping_and_proxy_semantics() -> None:
    result = provider().fetch_news(
        start_time=datetime(2026, 8, 31, 0, 0, tzinfo=SHANGHAI),
        end_time=datetime(2026, 8, 31, 23, 59, tzinfo=SHANGHAI),
        source="sina", pit_mode="source_timestamp_proxy",
    )[0]
    assert result.source_type == "news" and result.url is None
    assert result.channels == ("公司", "市场")
    assert result.available_at == COLLECTED
    assert result.strict_pit is False


def test_tushare_announcement_mapping_has_direct_canonical_symbol() -> None:
    result = provider().fetch_announcements(
        start_date=date(2026, 8, 31), end_date=date(2026, 8, 31),
        pit_mode="source_timestamp_proxy",
    )[0]
    assert result.symbol == "600519.SH"
    assert result.company_name == "贵州茅台"
    assert result.available_at == COLLECTED


def test_news_provider_permission_or_transport_failure_is_isolated() -> None:
    with pytest.raises(ProviderTransportError) as captured:
        provider(Client(fail_news=True)).fetch_news(
            start_time=datetime(2026, 8, 31, 0, 0, tzinfo=SHANGHAI),
            end_time=datetime(2026, 8, 31, 1, 0, tzinfo=SHANGHAI), source="sina",
        )
    assert "secret provider detail" not in str(captured.value)


def test_announcement_provider_failure_is_isolated() -> None:
    with pytest.raises(ProviderTransportError):
        provider(Client(fail_anns=True)).fetch_announcements(
            start_date=date(2026, 8, 31), end_date=date(2026, 8, 31)
        )


def test_news_provider_requires_token_without_leaking_it() -> None:
    with pytest.raises(ProviderConfigurationError) as captured:
        TushareNewsProvider(client=Client(), environ={})
    assert "token-value" not in str(captured.value)
