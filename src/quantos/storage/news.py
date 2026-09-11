"""Parquet/DuckDB persistence for PIT-separated news and announcements."""

from __future__ import annotations

import glob
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Sequence
from uuid import uuid4

import duckdb

from quantos.config import Settings
from quantos.schemas import AnnouncementRecord, NewsRecord, WebSearchResult
from quantos.schemas._validation import require_aware

from .market import StorageError


class NewsEvidenceRepository:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_directories()

    def write_news(self, records: Sequence[NewsRecord]) -> int:
        new = self._dedup(records, self.settings.news_dir, "news_id", lambda item: item.news_id)
        groups: dict[tuple[object, str], list[NewsRecord]] = {}
        for item in new:
            groups.setdefault((item.published_at.date(), item.source), []).append(item)
        for (publish_date, source), items in groups.items():
            target = self._target(self.settings.news_dir, "publish_date", publish_date, source)
            try:
                with self._connect() as connection:
                    connection.execute(_NEWS_TABLE_SQL)
                    connection.executemany(
                        "INSERT INTO news_batch VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [(
                            item.news_id, item.source, item.source_type,
                            item.published_at, item.collected_at, item.available_at,
                            item.title, item.content, item.url,
                            json.dumps(item.channels, ensure_ascii=False), item.provider,
                            item.provider_record_id, item.pit_mode, item.strict_pit,
                            item.domain, item.language, item.timestamp_basis,
                            item.evidence_basis,
                            item.schema_version,
                        ) for item in items],
                    )
                    self._copy(connection, "news_batch", target)
            except duckdb.Error as exc:
                raise StorageError(f"failed to write news data: {exc}") from exc
        return len(new)

    def write_web_search_results(self, records: Sequence[WebSearchResult]) -> int:
        new = self._dedup_web_search(records)
        groups: dict[tuple[object, str], list[WebSearchResult]] = {}
        for item in new:
            groups.setdefault((item.collected_at.date(), item.provider), []).append(item)
        for (collected_date, provider), items in groups.items():
            target = self._target(self.settings.web_search_dir, "collected_date", collected_date, provider)
            try:
                with self._connect() as connection:
                    connection.execute(_WEB_SEARCH_TABLE_SQL)
                    connection.executemany(
                        "INSERT INTO web_search_batch VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [(item.result_id, item.query, item.query_symbol, item.title, item.snippet,
                          item.url, item.domain, item.published_at, item.timestamp_basis,
                          item.collected_at, item.available_at, item.provider,
                          item.provider_record_id, item.provider_score, item.fetch_status,
                          item.evidence_basis, item.pit_mode, item.strict_pit, item.schema_version)
                         for item in items],
                    )
                    self._copy(connection, "web_search_batch", target)
            except duckdb.Error as exc:
                raise StorageError(f"failed to write web search data: {exc}") from exc
        return len(new)

    def _dedup_web_search(self, records: Sequence[WebSearchResult]) -> list[WebSearchResult]:
        identity = lambda item: (
            item.result_id, item.provider, item.provider_record_id,
            item.pit_mode, item.collected_at,
        )
        unique = {identity(item): item for item in records}
        candidates = [unique[key] for key in sorted(unique, key=str)]
        if not candidates or not list(self.settings.web_search_dir.rglob("*.parquet")):
            return candidates
        pattern = str(self.settings.web_search_dir / "**" / "*.parquet").replace("'", "''")
        try:
            with self._connect() as connection:
                rows = connection.execute(f"""
                    SELECT result_id, provider, provider_record_id, pit_mode, collected_at
                    FROM read_parquet('{pattern}')
                """).fetchall()
        except duckdb.Error as exc:
            raise StorageError(f"failed to inspect web search identities: {exc}") from exc
        existing = {(str(row[0]), str(row[1]), str(row[2]), str(row[3]), row[4]) for row in rows}
        return [item for item in candidates if identity(item) not in existing]

    def query_web_search_results(self, *, as_of_time: datetime,
                                 pit_mode: str | None = None) -> list[WebSearchResult]:
        require_aware(as_of_time, "as_of_time")
        if pit_mode not in {None, "strict_live", "source_timestamp_proxy"}:
            raise ValueError("pit_mode is invalid")
        pattern = self.settings.web_search_dir / "collected_date=*" / "source=*" / "*.parquet"
        if not glob.glob(str(pattern)):
            return []
        mode_clause = " AND pit_mode = ?" if pit_mode else ""
        parameters: list[object] = [as_of_time]
        if pit_mode:
            parameters.append(pit_mode)
        escaped = str(pattern).replace("'", "''")
        try:
            with self._connect() as connection:
                rows = connection.execute(f"""
                    SELECT result_id, query, query_symbol, title, snippet, url, domain,
                           published_at, timestamp_basis, collected_at, available_at,
                           provider, provider_record_id, provider_score, fetch_status,
                           evidence_basis, pit_mode, schema_version
                    FROM read_parquet('{escaped}', hive_partitioning=false)
                    WHERE available_at <= ? {mode_clause}
                    ORDER BY collected_at, result_id
                """, parameters).fetchall()
        except duckdb.Error as exc:
            raise StorageError(f"failed to query web search data: {exc}") from exc
        return [WebSearchResult(
            result_id=str(row[0]), query=str(row[1]), query_symbol=str(row[2]), title=str(row[3]),
            snippet=str(row[4]), url=str(row[5]), domain=str(row[6]), published_at=row[7],
            timestamp_basis=str(row[8]), collected_at=row[9], available_at=row[10],
            provider=str(row[11]), provider_record_id=str(row[12]),
            provider_score=float(row[13]) if row[13] is not None else None,
            fetch_status=str(row[14]), evidence_basis=str(row[15]), pit_mode=str(row[16]),
            schema_version=str(row[17]),
        ) for row in rows]

    def write_announcements(self, records: Sequence[AnnouncementRecord]) -> int:
        new = self._dedup(
            records, self.settings.announcement_dir, "announcement_id",
            lambda item: item.announcement_id,
        )
        groups: dict[tuple[object, str], list[AnnouncementRecord]] = {}
        for item in new:
            groups.setdefault((item.published_at.date(), item.source), []).append(item)
        for (publish_date, source), items in groups.items():
            target = self._target(self.settings.announcement_dir, "publish_date", publish_date, source)
            try:
                with self._connect() as connection:
                    connection.execute(_ANNOUNCEMENT_TABLE_SQL)
                    connection.executemany(
                        "INSERT INTO announcement_batch VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [(
                            item.announcement_id, item.symbol, item.company_name,
                            item.title, item.url, item.published_at,
                            item.collected_at, item.available_at, item.source,
                            item.provider, item.provider_record_id, item.pit_mode,
                            item.strict_pit, "announcement", item.schema_version,
                            item.timestamp_basis, item.evidence_basis,
                        ) for item in items],
                    )
                    self._copy(connection, "announcement_batch", target)
            except duckdb.Error as exc:
                raise StorageError(f"failed to write announcement data: {exc}") from exc
        return len(new)

    def query_news(
        self, *, start_time: datetime, end_time: datetime,
        as_of_time: datetime, pit_mode: str | None = None,
    ) -> list[NewsRecord]:
        _validate_query(start_time, end_time, as_of_time, pit_mode)
        pattern = self.settings.news_dir / "publish_date=*" / "source=*" / "*.parquet"
        if not glob.glob(str(pattern)):
            return []
        mode_clause = " AND pit_mode = ?" if pit_mode else ""
        parameters: list[object] = [start_time, end_time, as_of_time]
        if pit_mode:
            parameters.append(pit_mode)
        escaped = str(pattern).replace("'", "''")
        try:
            with self._connect() as connection:
                rows = connection.execute(f"""
                    SELECT news_id, source, source_type, published_at, collected_at,
                           available_at, title, content, url, channels_json,
                           provider, provider_record_id, pit_mode, domain, language,
                           timestamp_basis, evidence_basis, schema_version
                    FROM read_parquet('{escaped}', hive_partitioning=false)
                    WHERE published_at BETWEEN ? AND ? AND available_at <= ? {mode_clause}
                    ORDER BY published_at, news_id
                """, parameters).fetchall()
        except duckdb.Error as exc:
            raise StorageError(f"failed to query news data: {exc}") from exc
        return [NewsRecord(
            news_id=str(row[0]), source=str(row[1]), source_type=str(row[2]),
            published_at=row[3], collected_at=row[4], available_at=row[5],
            title=str(row[6]), content=str(row[7]) if row[7] is not None else None,
            url=str(row[8]) if row[8] is not None else None,
            channels=tuple(json.loads(str(row[9]))), provider=str(row[10]),
            provider_record_id=str(row[11]), pit_mode=str(row[12]),
            domain=str(row[13]) if row[13] is not None else None,
            language=str(row[14]) if row[14] is not None else None,
            timestamp_basis=str(row[15]), evidence_basis=str(row[16]),
            schema_version=str(row[17]),
        ) for row in rows]

    def query_announcements(
        self, *, start_time: datetime, end_time: datetime,
        as_of_time: datetime, pit_mode: str | None = None,
        symbol: str | None = None,
    ) -> list[AnnouncementRecord]:
        _validate_query(start_time, end_time, as_of_time, pit_mode)
        pattern = self.settings.announcement_dir / "publish_date=*" / "source=*" / "*.parquet"
        if not glob.glob(str(pattern)):
            return []
        mode_clause = " AND pit_mode = ?" if pit_mode else ""
        symbol_clause = " AND symbol = ?" if symbol else ""
        parameters: list[object] = [start_time, end_time, as_of_time]
        if pit_mode:
            parameters.append(pit_mode)
        if symbol:
            parameters.append(symbol)
        escaped = str(pattern).replace("'", "''")
        try:
            with self._connect() as connection:
                rows = connection.execute(f"""
                    SELECT announcement_id, symbol, company_name, title, url,
                           published_at, collected_at, available_at, source,
                           provider, provider_record_id, pit_mode, schema_version,
                           timestamp_basis, evidence_basis
                    FROM read_parquet('{escaped}', hive_partitioning=false)
                    WHERE published_at BETWEEN ? AND ? AND available_at <= ? {mode_clause} {symbol_clause}
                    ORDER BY published_at, announcement_id
                """, parameters).fetchall()
        except duckdb.Error as exc:
            raise StorageError(f"failed to query announcement data: {exc}") from exc
        return [AnnouncementRecord(
            announcement_id=str(row[0]), symbol=str(row[1]), company_name=str(row[2]),
            title=str(row[3]), url=str(row[4]) if row[4] is not None else None,
            published_at=row[5], collected_at=row[6], available_at=row[7],
            source=str(row[8]), provider=str(row[9]), provider_record_id=str(row[10]),
            pit_mode=str(row[11]), schema_version=str(row[12]),
            timestamp_basis=str(row[13]), evidence_basis=str(row[14]),
        ) for row in rows]

    def query_announcements_strict(
        self,
        *,
        start_time: datetime,
        end_time: datetime,
        as_of_time: datetime,
        symbol: str | None = None,
    ) -> list[AnnouncementRecord]:
        """Strict query never falls back to historical proxy evidence."""

        return self.query_announcements(
            start_time=start_time, end_time=end_time, as_of_time=as_of_time,
            pit_mode="strict_live", symbol=symbol,
        )

    def query_announcements_proxy(
        self,
        *,
        start_time: datetime,
        end_time: datetime,
        as_of_time: datetime,
        symbol: str | None = None,
    ) -> list[AnnouncementRecord]:
        """Explicit research query that returns proxy records without relabeling."""

        return self.query_announcements(
            start_time=start_time, end_time=end_time, as_of_time=as_of_time,
            pit_mode="source_timestamp_proxy", symbol=symbol,
        )

    def _dedup(self, records, root: Path, column: str, identity):
        seen: set[str] = set()
        candidates = []
        for item in records:
            key = identity(item)
            if key not in seen:
                candidates.append(item)
                seen.add(key)
        if not candidates or not list(root.rglob("*.parquet")):
            return candidates
        pattern = str(root / "**" / "*.parquet").replace("'", "''")
        placeholders = ", ".join("?" for _ in candidates)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT DISTINCT {column} FROM read_parquet('{pattern}') WHERE {column} IN ({placeholders})",
                    [identity(item) for item in candidates],
                ).fetchall()
        except duckdb.Error as exc:
            raise StorageError(f"failed to inspect news identities: {exc}") from exc
        existing = {str(row[0]) for row in rows}
        return [item for item in candidates if identity(item) not in existing]

    def _target(self, root: Path, partition: str, value, source: str) -> Path:
        safe_source = re.sub(r"[^A-Za-z0-9_.-]+", "_", source).strip("._") or "unknown"
        directory = root / f"{partition}={value}" / f"source={safe_source}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"part-{uuid4().hex}.parquet"

    def _connect(self):
        return duckdb.connect(str(self.settings.duckdb_path))

    @staticmethod
    def _copy(connection, table: str, target: Path) -> None:
        escaped = str(target).replace("'", "''")
        connection.execute(f"COPY {table} TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)")


def _validate_query(start_time, end_time, as_of_time, pit_mode):
    for name, value in (("start_time", start_time), ("end_time", end_time), ("as_of_time", as_of_time)):
        require_aware(value, name)
    if start_time > end_time:
        raise ValueError("start_time cannot be after end_time")
    if pit_mode not in {None, "strict_live", "source_timestamp_proxy"}:
        raise ValueError("pit_mode is invalid")


_NEWS_TABLE_SQL = """
CREATE TEMP TABLE news_batch (
 news_id VARCHAR, source VARCHAR, source_type VARCHAR, published_at TIMESTAMPTZ,
 collected_at TIMESTAMPTZ, available_at TIMESTAMPTZ, title VARCHAR, content VARCHAR,
 url VARCHAR, channels_json JSON, provider VARCHAR, provider_record_id VARCHAR,
 pit_mode VARCHAR, strict_pit BOOLEAN, domain VARCHAR, language VARCHAR,
 timestamp_basis VARCHAR, evidence_basis VARCHAR, schema_version VARCHAR)
"""

_ANNOUNCEMENT_TABLE_SQL = """
CREATE TEMP TABLE announcement_batch (
 announcement_id VARCHAR, symbol VARCHAR, company_name VARCHAR, title VARCHAR,
 url VARCHAR, published_at TIMESTAMPTZ, collected_at TIMESTAMPTZ,
 available_at TIMESTAMPTZ, source VARCHAR, provider VARCHAR,
 provider_record_id VARCHAR, pit_mode VARCHAR, strict_pit BOOLEAN,
 source_type VARCHAR, schema_version VARCHAR, timestamp_basis VARCHAR,
 evidence_basis VARCHAR)
"""

_WEB_SEARCH_TABLE_SQL = """
CREATE TEMP TABLE web_search_batch (
 result_id VARCHAR, query VARCHAR, query_symbol VARCHAR, title VARCHAR, snippet VARCHAR,
 url VARCHAR, domain VARCHAR, published_at TIMESTAMPTZ, timestamp_basis VARCHAR,
 collected_at TIMESTAMPTZ, available_at TIMESTAMPTZ, provider VARCHAR,
 provider_record_id VARCHAR, provider_score DOUBLE, fetch_status VARCHAR,
 evidence_basis VARCHAR, pit_mode VARCHAR, strict_pit BOOLEAN, schema_version VARCHAR)
"""
