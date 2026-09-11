from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from quantos.announcement_pipeline import (
    collect_current_announcements,
    ingest_announcement_history_proxy,
)
from quantos.schemas import AnnouncementRecord

SHANGHAI = ZoneInfo("Asia/Shanghai")


def record(mode):
    published = datetime(2026, 8, 31, 8, 0, tzinfo=SHANGHAI)
    collected = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)
    return AnnouncementRecord(
        announcement_id=f"id-{mode}", symbol="600519.SH", company_name="贵州茅台",
        title="公告", url=None, published_at=published, collected_at=collected,
        available_at=collected, source="cninfo", provider="cninfo",
        provider_record_id=f"id-{mode}", pit_mode=mode,
    )


class Provider:
    def fetch_announcements(self, *, start_date, end_date, pit_mode):
        return [record(pit_mode)]


class Storage:
    def __init__(self):
        self.ids = set()

    def write_announcements(self, records):
        new = [item for item in records if item.announcement_id not in self.ids]
        self.ids.update(item.announcement_id for item in new)
        return len(new)


def test_current_collection_accumulates_strict_live_idempotently() -> None:
    storage = Storage()
    first = collect_current_announcements(Provider(), storage, as_of_date=date(2026, 9, 1))
    second = collect_current_announcements(Provider(), storage, as_of_date=date(2026, 9, 1))
    assert first.pit_mode == "strict_live" and first.stored_count == 1
    assert second.stored_count == 0 and second.duplicate_count == 1


def test_historical_ingestion_cannot_become_strict() -> None:
    result = ingest_announcement_history_proxy(
        Provider(), Storage(), start_date=date(2026, 8, 30), end_date=date(2026, 9, 1)
    )
    assert result.pit_mode == "source_timestamp_proxy"
    assert result.raw_count == result.stored_count == 1


def test_pipeline_rejects_provider_mode_confusion() -> None:
    class WrongProvider:
        def fetch_announcements(self, *, start_date, end_date, pit_mode):
            return [record("source_timestamp_proxy")]

    with pytest.raises(ValueError, match="PIT mode"):
        collect_current_announcements(WrongProvider(), Storage(), as_of_date=date(2026, 9, 1))
