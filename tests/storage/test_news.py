from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from quantos.config import Settings
from quantos.news import stable_news_id
from quantos.schemas import AnnouncementRecord, NewsRecord, WebSearchResult
from quantos.storage import NewsEvidenceRepository

SHANGHAI = ZoneInfo("Asia/Shanghai")
PUBLISHED = datetime(2026, 8, 31, 14, 0, tzinfo=SHANGHAI)
COLLECTED = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)


def news(pit_mode="source_timestamp_proxy"):
    identity = stable_news_id("sina", PUBLISHED, "标题", "正文")
    return NewsRecord(
        news_id=identity, source="sina", source_type="news", published_at=PUBLISHED,
        collected_at=COLLECTED, available_at=COLLECTED, title="标题", content="正文",
        url=None, channels=("公司",), provider="tushare", provider_record_id=identity,
        pit_mode=pit_mode,
    )


def announcement():
    return AnnouncementRecord(
        announcement_id="ann-1", symbol="600519.SH", company_name="贵州茅台",
        title="回购公告", url=None, published_at=PUBLISHED, collected_at=COLLECTED,
        available_at=COLLECTED, source="tushare_anns_d", provider="tushare",
        provider_record_id="ann-1", pit_mode="source_timestamp_proxy",
    )


def web_result():
    return WebSearchResult(
        result_id="web-1", query="贵州茅台股份有限公司", query_symbol="600519.SH",
        title="贵州茅台", snippet="", url="https://x.test/a", domain="x.test",
        published_at=None, timestamp_basis="unknown", collected_at=COLLECTED,
        available_at=COLLECTED, provider="tavily", provider_record_id="native-1",
    )


def test_news_storage_is_idempotent_and_pit_safe(tmp_path: Path) -> None:
    repository = NewsEvidenceRepository(Settings.from_project_root(tmp_path))
    assert repository.write_news([news(), news()]) == 1
    assert repository.write_news([news()]) == 0
    before = repository.query_news(
        start_time=PUBLISHED, end_time=PUBLISHED, as_of_time=COLLECTED.replace(hour=9)
    )
    after = repository.query_news(
        start_time=PUBLISHED, end_time=PUBLISHED, as_of_time=COLLECTED
    )
    assert before == [] and after == [news()]


def test_news_query_separates_proxy_and_strict_modes(tmp_path: Path) -> None:
    repository = NewsEvidenceRepository(Settings.from_project_root(tmp_path))
    repository.write_news([news()])
    assert repository.query_news(
        start_time=PUBLISHED, end_time=PUBLISHED, as_of_time=COLLECTED,
        pit_mode="strict_live",
    ) == []
    assert len(repository.query_news(
        start_time=PUBLISHED, end_time=PUBLISHED, as_of_time=COLLECTED,
        pit_mode="source_timestamp_proxy",
    )) == 1


def test_announcement_storage_idempotency_and_time_query(tmp_path: Path) -> None:
    repository = NewsEvidenceRepository(Settings.from_project_root(tmp_path))
    assert repository.write_announcements([announcement(), announcement()]) == 1
    assert repository.write_announcements([announcement()]) == 0
    result = repository.query_announcements(
        start_time=PUBLISHED, end_time=PUBLISHED, as_of_time=COLLECTED
    )
    assert result == [announcement()]


def test_strict_and_proxy_announcement_queries_never_mix(tmp_path: Path) -> None:
    repository = NewsEvidenceRepository(Settings.from_project_root(tmp_path))
    proxy = announcement()
    strict = AnnouncementRecord(
        announcement_id="strict-1", symbol="000001.SZ", company_name="平安银行",
        title="严格公告", url=None, published_at=PUBLISHED, collected_at=COLLECTED,
        available_at=COLLECTED, source="cninfo", provider="cninfo",
        provider_record_id="strict-1", pit_mode="strict_live",
    )
    repository.write_announcements([proxy, strict])
    assert repository.query_announcements_strict(
        start_time=PUBLISHED, end_time=PUBLISHED, as_of_time=COLLECTED
    ) == [strict]
    assert repository.query_announcements_proxy(
        start_time=PUBLISHED, end_time=PUBLISHED, as_of_time=COLLECTED
    ) == [proxy]


def test_announcement_query_filters_published_time_and_symbol(tmp_path: Path) -> None:
    repository = NewsEvidenceRepository(Settings.from_project_root(tmp_path))
    repository.write_announcements([announcement()])
    assert repository.query_announcements_proxy(
        start_time=PUBLISHED, end_time=PUBLISHED, as_of_time=COLLECTED,
        symbol="000001.SZ",
    ) == []
    assert repository.query_announcements_proxy(
        start_time=PUBLISHED, end_time=PUBLISHED, as_of_time=COLLECTED,
        symbol="600519.SH",
    ) == [announcement()]


def test_web_search_storage_is_idempotent_nullable_and_pit_separated(tmp_path: Path) -> None:
    repository = NewsEvidenceRepository(Settings.from_project_root(tmp_path))
    assert repository.write_web_search_results([web_result(), web_result()]) == 1
    assert repository.write_web_search_results([web_result()]) == 0
    assert repository.query_web_search_results(
        as_of_time=COLLECTED.replace(hour=9), pit_mode="source_timestamp_proxy"
    ) == []
    assert repository.query_web_search_results(
        as_of_time=COLLECTED, pit_mode="strict_live"
    ) == []
    assert repository.query_web_search_results(
        as_of_time=COLLECTED, pit_mode="source_timestamp_proxy"
    ) == [web_result()]


def test_strict_live_collection_time_is_hard_availability_boundary(tmp_path: Path) -> None:
    repository = NewsEvidenceRepository(Settings.from_project_root(tmp_path))
    strict = WebSearchResult(
        result_id="strict-1", query="贵州茅台", query_symbol="600519.SH", title="贵州茅台",
        snippet="", url="https://x.test/strict", domain="x.test",
        published_at=PUBLISHED, timestamp_basis="provider_reported", collected_at=COLLECTED,
        available_at=COLLECTED, provider="tavily", provider_record_id="strict-native",
        pit_mode="strict_live",
    )
    repository.write_web_search_results([strict])
    assert repository.query_web_search_results(
        as_of_time=COLLECTED.replace(minute=0) - timedelta(minutes=1), pit_mode="strict_live"
    ) == []
    assert repository.query_web_search_results(as_of_time=COLLECTED, pit_mode="strict_live") == [strict]
    assert strict.published_at < strict.available_at and strict.available_at == strict.collected_at


def test_proxy_and_strict_same_url_identity_coexist(tmp_path: Path) -> None:
    repository = NewsEvidenceRepository(Settings.from_project_root(tmp_path))
    proxy = web_result()
    strict = WebSearchResult(
        result_id=proxy.result_id, query=proxy.query, query_symbol=proxy.query_symbol,
        title=proxy.title, snippet=proxy.snippet, url=proxy.url, domain=proxy.domain,
        published_at=proxy.published_at, timestamp_basis=proxy.timestamp_basis,
        collected_at=COLLECTED + timedelta(minutes=1), available_at=COLLECTED + timedelta(minutes=1),
        provider=proxy.provider, provider_record_id=proxy.provider_record_id, pit_mode="strict_live",
    )
    assert repository.write_web_search_results([proxy, strict]) == 2
    assert len(repository.query_web_search_results(
        as_of_time=COLLECTED + timedelta(minutes=1), pit_mode="source_timestamp_proxy")) == 1
    assert len(repository.query_web_search_results(
        as_of_time=COLLECTED + timedelta(minutes=1), pit_mode="strict_live")) == 1
