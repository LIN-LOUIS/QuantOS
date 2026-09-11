"""One-shot official announcement collection without scheduling side effects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, Sequence

from quantos.collectors import AnnouncementProvider
from quantos.schemas import AnnouncementRecord


class AnnouncementStorage(Protocol):
    def write_announcements(self, records: Sequence[AnnouncementRecord]) -> int: ...


@dataclass(frozen=True, slots=True)
class AnnouncementCollectionResult:
    start_date: date
    end_date: date
    pit_mode: str
    raw_count: int
    stored_count: int
    duplicate_count: int


def collect_current_announcements(
    provider: AnnouncementProvider,
    repository: AnnouncementStorage,
    *,
    as_of_date: date,
) -> AnnouncementCollectionResult:
    """Collect one current date as strict-live evidence for future PIT use."""

    return _collect(
        provider, repository, start_date=as_of_date, end_date=as_of_date,
        pit_mode="strict_live",
    )


def ingest_announcement_history_proxy(
    provider: AnnouncementProvider,
    repository: AnnouncementStorage,
    *,
    start_date: date,
    end_date: date,
) -> AnnouncementCollectionResult:
    """Backfill historical records while preserving explicit proxy semantics."""

    return _collect(
        provider, repository, start_date=start_date, end_date=end_date,
        pit_mode="source_timestamp_proxy",
    )


def _collect(
    provider: AnnouncementProvider,
    repository: AnnouncementStorage,
    *,
    start_date: date,
    end_date: date,
    pit_mode: str,
) -> AnnouncementCollectionResult:
    records = provider.fetch_announcements(
        start_date=start_date, end_date=end_date, pit_mode=pit_mode
    )
    if any(record.pit_mode != pit_mode for record in records):
        raise ValueError("provider returned an unexpected announcement PIT mode")
    stored = repository.write_announcements(records)
    return AnnouncementCollectionResult(
        start_date=start_date, end_date=end_date, pit_mode=pit_mode,
        raw_count=len(records), stored_count=stored,
        duplicate_count=len(records) - stored,
    )
