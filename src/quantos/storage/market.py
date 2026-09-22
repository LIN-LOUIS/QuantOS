"""DuckDB-backed access to separate raw and normalized Parquet datasets."""

from __future__ import annotations

import json
import glob
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence
from uuid import uuid4

import duckdb

from quantos.collectors import RawMarketRecord
from quantos.config import Settings
from quantos.schemas import MarketBar
from quantos.schemas._validation import require_aware


class StorageError(RuntimeError):
    """A local storage operation failed."""


_SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


class MarketDataRepository:
    """Append-only Parquet persistence with mandatory PIT reads."""

    def __init__(self, settings: Settings, *, read_only: bool = False) -> None:
        self.settings = settings
        self.read_only = read_only
        if not read_only:
            self.settings.ensure_directories()
            self._initialize_catalog()

    def write_raw(self, records: Sequence[RawMarketRecord]) -> int:
        """Append previously unseen raw provider records to raw Parquet."""

        self._require_writable()
        if not records:
            return 0
        existing = self._existing_ids(
            self.settings.raw_market_dir,
            "provider_record_id",
            [record.provider_record_id for record in records],
        )
        new_records: list[RawMarketRecord] = []
        seen = set(existing)
        for record in records:
            if record.provider_record_id not in seen:
                new_records.append(record)
                seen.add(record.provider_record_id)
        if not new_records:
            return 0

        grouped: dict[date, list[RawMarketRecord]] = {}
        for record in new_records:
            grouped.setdefault(record.received_at.date(), []).append(record)
        for partition_date, partition_records in grouped.items():
            target_dir = self.settings.raw_market_dir / f"received_date={partition_date}"
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"part-{uuid4().hex}.parquet"
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        CREATE TEMP TABLE raw_batch (
                            provider VARCHAR,
                            symbol VARCHAR,
                            provider_record_id VARCHAR,
                            received_at TIMESTAMPTZ,
                            fields_json JSON,
                            schema_version VARCHAR
                        )
                        """
                    )
                    connection.executemany(
                        "INSERT INTO raw_batch VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            (
                                record.provider,
                                record.symbol,
                                record.provider_record_id,
                                record.received_at,
                                json.dumps(
                                    record.fields, ensure_ascii=False, default=str
                                ),
                                "raw-v1",
                            )
                            for record in partition_records
                        ],
                    )
                    self._copy_to_parquet(connection, "raw_batch", target)
            except StorageError:
                raise
            except duckdb.Error as exc:
                raise StorageError(f"failed to write raw market data: {exc}") from exc
        return len(new_records)

    def write_bars(self, bars: Sequence[MarketBar]) -> int:
        """Append previously unseen normalized bars to partitioned Parquet."""

        self._require_writable()
        if not bars:
            return 0
        existing = self._existing_ids(
            self.settings.normalized_market_dir,
            "source_record_id",
            [bar.source_record_id for bar in bars],
        )
        new_bars: list[MarketBar] = []
        seen = set(existing)
        for bar in bars:
            if bar.source_record_id not in seen:
                new_bars.append(bar)
                seen.add(bar.source_record_id)
        if not new_bars:
            return 0

        grouped: dict[tuple[date, str], list[MarketBar]] = {}
        for bar in new_bars:
            grouped.setdefault((bar.timestamp.date(), bar.symbol), []).append(bar)
        for (trade_date, symbol), partition_bars in grouped.items():
            target_dir = (
                self.settings.normalized_market_dir
                / f"trade_date={trade_date}"
                / f"symbol={symbol}"
            )
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"part-{uuid4().hex}.parquet"
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        CREATE TEMP TABLE bar_batch (
                            symbol VARCHAR,
                            timestamp TIMESTAMPTZ,
                            frequency VARCHAR,
                            open DECIMAL(38, 10),
                            high DECIMAL(38, 10),
                            low DECIMAL(38, 10),
                            close DECIMAL(38, 10),
                            volume BIGINT,
                            amount DECIMAL(38, 10),
                            prev_close DECIMAL(38, 10),
                            source VARCHAR,
                            source_record_id VARCHAR,
                            collected_at TIMESTAMPTZ,
                            available_at TIMESTAMPTZ,
                            schema_version VARCHAR
                        )
                        """
                    )
                    connection.executemany(
                        "INSERT INTO bar_batch VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [self._bar_row(bar) for bar in partition_bars],
                    )
                    self._copy_to_parquet(connection, "bar_batch", target)
            except StorageError:
                raise
            except duckdb.Error as exc:
                raise StorageError(
                    f"failed to write normalized market data: {exc}"
                ) from exc
        return len(new_bars)

    def write_daily_bars(self, bars: Sequence[MarketBar]) -> int:
        """Append full-market daily batches without creating one file per symbol."""

        self._require_writable()
        if not bars:
            return 0
        if any(bar.frequency != "1d" for bar in bars):
            raise ValueError("write_daily_bars accepts only 1d bars")
        existing = self._existing_ids(
            self.settings.normalized_market_dir,
            "source_record_id",
            [bar.source_record_id for bar in bars],
        )
        new_bars: list[MarketBar] = []
        seen = set(existing)
        for bar in bars:
            if bar.source_record_id not in seen:
                new_bars.append(bar)
                seen.add(bar.source_record_id)
        if not new_bars:
            return 0
        grouped: dict[date, list[MarketBar]] = {}
        for bar in new_bars:
            grouped.setdefault(bar.timestamp.date(), []).append(bar)
        for trade_date, daily_bars in grouped.items():
            target_dir = (
                self.settings.normalized_market_dir
                / f"trade_date={trade_date}"
                / "market=shse_szse"
            )
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"part-{uuid4().hex}.parquet"
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        CREATE TEMP TABLE daily_bar_batch (
                            symbol VARCHAR,
                            timestamp TIMESTAMPTZ,
                            frequency VARCHAR,
                            open DECIMAL(38, 10),
                            high DECIMAL(38, 10),
                            low DECIMAL(38, 10),
                            close DECIMAL(38, 10),
                            volume BIGINT,
                            amount DECIMAL(38, 10),
                            prev_close DECIMAL(38, 10),
                            source VARCHAR,
                            source_record_id VARCHAR,
                            collected_at TIMESTAMPTZ,
                            available_at TIMESTAMPTZ,
                            schema_version VARCHAR
                        )
                        """
                    )
                    connection.executemany(
                        "INSERT INTO daily_bar_batch VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [self._bar_row(bar) for bar in daily_bars],
                    )
                    self._copy_to_parquet(connection, "daily_bar_batch", target)
            except StorageError:
                raise
            except duckdb.Error as exc:
                raise StorageError(
                    f"failed to write daily market data: {exc}"
                ) from exc
        return len(new_bars)

    def read_by_date(self, trade_date: date, *, as_of_time: datetime) -> list[MarketBar]:
        require_aware(as_of_time, "as_of_time")
        pattern = self.settings.normalized_market_dir / f"trade_date={trade_date}" / "**" / "*.parquet"
        return self._query_bars(pattern, "available_at <= ?", [as_of_time])

    def read_by_symbol(
        self,
        symbol: str,
        *,
        as_of_time: datetime,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[MarketBar]:
        require_aware(as_of_time, "as_of_time")
        if not _SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("symbol must use the canonical 000001.SZ format")
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("start_date cannot be after end_date")
        pattern = self.settings.normalized_market_dir / "trade_date=*" / "**" / "*.parquet"
        clauses = ["available_at <= ?", "symbol = ?"]
        parameters: list[object] = [as_of_time, symbol]
        if start_date is not None:
            clauses.append("CAST(timestamp AS DATE) >= ?")
            parameters.append(start_date)
        if end_date is not None:
            clauses.append("CAST(timestamp AS DATE) <= ?")
            parameters.append(end_date)
        return self._query_bars(pattern, " AND ".join(clauses), parameters)

    def load_market_bar_history(
        self,
        symbol: str,
        *,
        end_date: date,
        lookback: int,
        as_of_time: datetime,
    ) -> list[MarketBar]:
        """Load the latest PIT-visible observations, returned date ascending."""

        require_aware(as_of_time, "as_of_time")
        if lookback < 1:
            raise ValueError("lookback must be positive")
        bars = self.read_by_symbol(
            symbol,
            as_of_time=as_of_time,
            end_date=end_date,
        )
        return bars[-lookback:]

    def _initialize_catalog(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS storage_metadata (
                        key VARCHAR PRIMARY KEY,
                        value VARCHAR NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT OR REPLACE INTO storage_metadata VALUES ('schema_version', 'v1')"
                )
        except duckdb.Error as exc:
            raise StorageError(f"failed to initialize DuckDB catalog: {exc}") from exc

    def _connect(self) -> duckdb.DuckDBPyConnection:
        if self.read_only:
            return duckdb.connect(":memory:")
        return duckdb.connect(str(self.settings.duckdb_path))

    def _require_writable(self) -> None:
        if self.read_only:
            raise StorageError("market repository is read-only")

    @staticmethod
    def _copy_to_parquet(
        connection: duckdb.DuckDBPyConnection, table: str, target: Path
    ) -> None:
        escaped = str(target).replace("'", "''")
        try:
            connection.execute(
                f"COPY {table} TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        except duckdb.Error as exc:
            raise StorageError(f"failed to write Parquet file: {exc}") from exc

    def _existing_ids(
        self, root: Path, column: str, candidate_ids: Iterable[str]
    ) -> set[str]:
        candidates = list(dict.fromkeys(candidate_ids))
        files = list(root.rglob("*.parquet"))
        if not files or not candidates:
            return set()
        pattern = str(root / "**" / "*.parquet")
        placeholders = ", ".join("?" for _ in candidates)
        escaped_pattern = pattern.replace("'", "''")
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT DISTINCT {column} FROM read_parquet('{escaped_pattern}') "
                    f"WHERE {column} IN ({placeholders})",
                    candidates,
                ).fetchall()
        except duckdb.Error as exc:
            raise StorageError(f"failed to inspect Parquet identities: {exc}") from exc
        return {row[0] for row in rows}

    def _query_bars(
        self, pattern: Path, where_clause: str, parameters: Sequence[object]
    ) -> list[MarketBar]:
        # DuckDB's read_parquet raises when a glob matches no files.
        if not glob.glob(str(pattern)):
            return []
        escaped_pattern = str(pattern).replace("'", "''")
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT symbol, timestamp, frequency, open, high, low, close,
                           volume, amount, prev_close, source, source_record_id,
                           collected_at, available_at, schema_version
                    FROM read_parquet('{escaped_pattern}', hive_partitioning = false)
                    WHERE {where_clause}
                    ORDER BY timestamp, symbol
                    """,
                    list(parameters),
                ).fetchall()
        except duckdb.Error as exc:
            raise StorageError(f"failed to query normalized Parquet: {exc}") from exc
        try:
            return [self._row_to_bar(row) for row in rows]
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise StorageError(f"stored market data is invalid: {exc}") from exc

    @staticmethod
    def _bar_row(bar: MarketBar) -> tuple[object, ...]:
        return (
            bar.symbol,
            bar.timestamp,
            bar.frequency,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.amount,
            bar.prev_close,
            bar.source,
            bar.source_record_id,
            bar.collected_at,
            bar.available_at,
            bar.schema_version,
        )

    def _row_to_bar(self, row: tuple[object, ...]) -> MarketBar:
        market_tz = self.settings.market_timezone
        timestamp, collected_at, available_at = row[1], row[12], row[13]
        if not all(
            isinstance(value, datetime)
            for value in (timestamp, collected_at, available_at)
        ):
            raise StorageError("stored market timestamps have invalid types")
        return MarketBar(
            symbol=str(row[0]),
            timestamp=timestamp.astimezone(market_tz),
            frequency=str(row[2]),
            open=Decimal(row[3]),
            high=Decimal(row[4]),
            low=Decimal(row[5]),
            close=Decimal(row[6]),
            volume=int(row[7]),
            amount=Decimal(row[8]),
            prev_close=Decimal(row[9]) if row[9] is not None else None,
            source=str(row[10]),
            source_record_id=str(row[11]),
            collected_at=collected_at.astimezone(market_tz),
            available_at=available_at.astimezone(market_tz),
            schema_version=str(row[14]),
        )
