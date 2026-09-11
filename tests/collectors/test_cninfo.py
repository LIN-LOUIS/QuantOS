from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from quantos.collectors import CninfoAnnouncementProvider, ProviderResponseError, ProviderTransportError

SHANGHAI = ZoneInfo("Asia/Shanghai")
COLLECTED = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)
PUBLISHED_MS = int(datetime(2026, 8, 31, 8, 30, tzinfo=SHANGHAI).timestamp() * 1000)


def payload(**overrides):
    row = {
        "announcementId": "1225000001", "secCode": "000001",
        "secName": "平安银行", "announcementTitle": "<em>平安银行</em>回购公告",
        "announcementTime": PUBLISHED_MS,
        "adjunctUrl": "finalpage/2026-08-31/1225000001.PDF",
    }
    row.update(overrides.pop("row", {}))
    value = {"announcements": [row], "totalpages": 1, "hasMore": False}
    value.update(overrides)
    return value


class Transport:
    def __init__(self, response=None, error=None):
        self.response = payload() if response is None else response
        self.error = error
        self.calls = []

    def __call__(self, url, form, timeout):
        self.calls.append((url, form, timeout))
        if self.error:
            raise self.error
        return self.response


def provider(transport, **kwargs):
    return CninfoAnnouncementProvider(
        transport=transport, clock=lambda: COLLECTED, columns=("szse",), **kwargs
    )


def test_cninfo_payload_mapping_uses_native_id_pdf_and_proxy_time() -> None:
    result = provider(Transport()).fetch_announcements(
        start_date=date(2026, 8, 30), end_date=date(2026, 9, 1),
        pit_mode="source_timestamp_proxy",
    )[0]
    assert result.announcement_id == "1225000001"
    assert result.symbol == "000001.SZ"
    assert result.title == "平安银行回购公告"
    assert result.url == "https://static.cninfo.com.cn/finalpage/2026-08-31/1225000001.PDF"
    assert result.published_at == datetime(2026, 8, 31, 8, 30, tzinfo=SHANGHAI)
    assert result.collected_at == result.available_at == COLLECTED
    assert result.strict_pit is False


def test_cninfo_sse_symbol_uses_explicit_column_metadata_not_prefix_guess() -> None:
    transport = Transport(payload(row={"announcementId": "2", "secCode": "000001"}))
    result = CninfoAnnouncementProvider(
        transport=transport, clock=lambda: COLLECTED, columns=("sse",)
    ).fetch_announcements(
        start_date=date(2026, 8, 31), end_date=date(2026, 8, 31)
    )[0]
    assert result.symbol == "000001.SH"


def test_cninfo_missing_native_id_uses_stable_hash() -> None:
    response = payload(row={"announcementId": ""})
    first = provider(Transport(response)).fetch_announcements(
        start_date=date(2026, 8, 31), end_date=date(2026, 8, 31)
    )[0]
    second = provider(Transport(response)).fetch_announcements(
        start_date=date(2026, 8, 31), end_date=date(2026, 8, 31)
    )[0]
    assert first.announcement_id == second.announcement_id
    assert len(first.announcement_id) == 64


def test_cninfo_local_symbol_filter_is_exact() -> None:
    assert provider(Transport()).fetch_announcements(
        start_date=date(2026, 8, 31), end_date=date(2026, 8, 31),
        symbols=["600519.SH"],
    ) == []


def test_cninfo_refuses_silent_partial_pagination() -> None:
    response = payload(totalpages=2, hasMore=True)
    with pytest.raises(ProviderResponseError, match="partial"):
        provider(Transport(response), max_pages=1).fetch_announcements(
            start_date=date(2026, 8, 31), end_date=date(2026, 8, 31)
        )


def test_cninfo_provider_failure_is_typed_and_isolated() -> None:
    with pytest.raises(ProviderTransportError) as captured:
        provider(Transport(error=TimeoutError("private detail"))).fetch_announcements(
            start_date=date(2026, 8, 31), end_date=date(2026, 8, 31)
        )
    assert "private detail" not in str(captured.value)
