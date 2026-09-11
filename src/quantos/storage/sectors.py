"""Parquet persistence for sector analytics and daily membership archives."""

from __future__ import annotations

import glob
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence
from uuid import uuid4

import duckdb

from quantos.config import Settings
from quantos.schemas import SectorMembershipSnapshot, SectorSnapshot
from quantos.schemas._validation import require_aware

from .market import StorageError


class SectorDataRepository:
    """Append-only sector datasets using the existing DuckDB/Parquet stack."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_directories()

    def write_snapshots(self, snapshots: Sequence[SectorSnapshot]) -> int:
        if not snapshots:
            return 0
        identities = [_snapshot_id(snapshot) for snapshot in snapshots]
        existing = self._existing_ids(
            self.settings.sector_snapshot_dir, "snapshot_id", identities
        )
        new_rows: list[tuple[SectorSnapshot, str]] = []
        seen = set(existing)
        for snapshot, identity in zip(snapshots, identities):
            if identity not in seen:
                new_rows.append((snapshot, identity))
                seen.add(identity)
        grouped: dict[date, list[tuple[SectorSnapshot, str]]] = {}
        for snapshot, identity in new_rows:
            grouped.setdefault(snapshot.trade_date, []).append((snapshot, identity))
        for trade_date, rows in grouped.items():
            target_dir = self.settings.sector_snapshot_dir / f"trade_date={trade_date}"
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"part-{uuid4().hex}.parquet"
            try:
                with self._connect() as connection:
                    connection.execute(_SECTOR_SNAPSHOT_TABLE_SQL)
                    connection.executemany(
                        "INSERT INTO sector_snapshot_batch VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [self._snapshot_row(snapshot, identity) for snapshot, identity in rows],
                    )
                    self._copy(connection, "sector_snapshot_batch", target)
            except duckdb.Error as exc:
                raise StorageError(f"failed to write sector snapshots: {exc}") from exc
        return len(new_rows)

    def load_snapshot_history(
        self,
        sector_code: str,
        *,
        end_date: date,
        lookback: int,
        as_of_time: datetime,
        historical_membership_mode: str = "strict_pit",
    ) -> list[SectorSnapshot]:
        require_aware(as_of_time, "as_of_time")
        if lookback < 1:
            raise ValueError("lookback must be positive")
        pattern = self.settings.sector_snapshot_dir / "trade_date=*" / "*.parquet"
        if not glob.glob(str(pattern)):
            return []
        escaped = str(pattern).replace("'", "''")
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT trade_date, as_of_time, sector_code, sector_name,
                           classification, member_count, valid_bar_count,
                           coverage_ratio, equal_weight_return_pct,
                           median_return_pct, advancer_count, decliner_count,
                           flat_count, advancer_ratio, total_volume, total_amount,
                           top_gainer_symbol, top_gainer_return_pct,
                           top_loser_symbol, top_loser_return_pct, available_at,
                           universe, historical_membership_mode, schema_version
                    FROM read_parquet('{escaped}', hive_partitioning = false)
                    WHERE sector_code = ? AND trade_date <= ?
                      AND available_at <= ? AND as_of_time <= ?
                      AND historical_membership_mode = ?
                    ORDER BY trade_date DESC, available_at DESC, as_of_time DESC
                    """,
                    [
                        sector_code,
                        end_date,
                        as_of_time,
                        as_of_time,
                        historical_membership_mode,
                    ],
                ).fetchall()
        except duckdb.Error as exc:
            raise StorageError(f"failed to read sector snapshot history: {exc}") from exc
        latest: dict[date, tuple[object, ...]] = {}
        for row in rows:
            latest.setdefault(row[0], row)
        selected = [latest[key] for key in sorted(latest)[-lookback:]]
        return [self._row_to_snapshot(row) for row in selected]

    def write_membership_snapshots(
        self, snapshots: Sequence[SectorMembershipSnapshot]
    ) -> int:
        if not snapshots:
            return 0
        identities = [_membership_snapshot_id(snapshot) for snapshot in snapshots]
        existing = self._existing_ids(
            self.settings.sector_membership_snapshot_dir,
            "snapshot_id",
            identities,
        )
        new_rows: list[tuple[SectorMembershipSnapshot, str]] = []
        seen = set(existing)
        for snapshot, identity in zip(snapshots, identities):
            if identity not in seen:
                new_rows.append((snapshot, identity))
                seen.add(identity)
        grouped: dict[date, list[tuple[SectorMembershipSnapshot, str]]] = {}
        for snapshot, identity in new_rows:
            grouped.setdefault(snapshot.as_of_date, []).append((snapshot, identity))
        for as_of_date, rows in grouped.items():
            target_dir = (
                self.settings.sector_membership_snapshot_dir
                / f"as_of_date={as_of_date}"
            )
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"part-{uuid4().hex}.parquet"
            try:
                with self._connect() as connection:
                    connection.execute(_MEMBERSHIP_SNAPSHOT_TABLE_SQL)
                    connection.executemany(
                        "INSERT INTO membership_snapshot_batch VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                identity,
                                snapshot.as_of_date,
                                snapshot.symbol,
                                snapshot.sector_code,
                                snapshot.sector_name,
                                snapshot.classification,
                                snapshot.source,
                                snapshot.available_at,
                                snapshot.historical_membership_mode,
                                snapshot.schema_version,
                            )
                            for snapshot, identity in rows
                        ],
                    )
                    self._copy(connection, "membership_snapshot_batch", target)
            except duckdb.Error as exc:
                raise StorageError(
                    f"failed to write sector membership snapshots: {exc}"
                ) from exc
        return len(new_rows)

    def _connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.settings.duckdb_path))

    def _existing_ids(
        self, root: Path, column: str, candidate_ids: Iterable[str]
    ) -> set[str]:
        candidates = list(dict.fromkeys(candidate_ids))
        if not candidates or not list(root.rglob("*.parquet")):
            return set()
        pattern = str(root / "**" / "*.parquet").replace("'", "''")
        placeholders = ", ".join("?" for _ in candidates)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT DISTINCT {column} FROM read_parquet('{pattern}') "
                    f"WHERE {column} IN ({placeholders})",
                    candidates,
                ).fetchall()
        except duckdb.Error as exc:
            raise StorageError(f"failed to inspect sector identities: {exc}") from exc
        return {str(row[0]) for row in rows}

    @staticmethod
    def _copy(connection: duckdb.DuckDBPyConnection, table: str, target: Path) -> None:
        escaped = str(target).replace("'", "''")
        connection.execute(
            f"COPY {table} TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )

    @staticmethod
    def _snapshot_row(snapshot: SectorSnapshot, identity: str) -> tuple[object, ...]:
        return (
            identity,
            snapshot.trade_date,
            snapshot.as_of_time,
            snapshot.sector_code,
            snapshot.sector_name,
            snapshot.classification,
            snapshot.member_count,
            snapshot.valid_bar_count,
            snapshot.coverage_ratio,
            snapshot.equal_weight_return_pct,
            snapshot.median_return_pct,
            snapshot.advancer_count,
            snapshot.decliner_count,
            snapshot.flat_count,
            snapshot.advancer_ratio,
            snapshot.total_volume,
            snapshot.total_amount,
            snapshot.top_gainer_symbol,
            snapshot.top_gainer_return_pct,
            snapshot.top_loser_symbol,
            snapshot.top_loser_return_pct,
            snapshot.available_at,
            snapshot.universe,
            snapshot.historical_membership_mode,
            snapshot.schema_version,
        )

    @staticmethod
    def _row_to_snapshot(row: tuple[object, ...]) -> SectorSnapshot:
        return SectorSnapshot(
            trade_date=row[0],
            as_of_time=row[1],
            sector_code=str(row[2]),
            sector_name=str(row[3]),
            classification=str(row[4]),
            member_count=int(row[5]),
            valid_bar_count=int(row[6]),
            coverage_ratio=Decimal(row[7]),
            equal_weight_return_pct=Decimal(row[8]) if row[8] is not None else None,
            median_return_pct=Decimal(row[9]) if row[9] is not None else None,
            advancer_count=int(row[10]),
            decliner_count=int(row[11]),
            flat_count=int(row[12]),
            advancer_ratio=Decimal(row[13]) if row[13] is not None else None,
            total_volume=int(row[14]),
            total_amount=Decimal(row[15]),
            top_gainer_symbol=str(row[16]) if row[16] is not None else None,
            top_gainer_return_pct=Decimal(row[17]) if row[17] is not None else None,
            top_loser_symbol=str(row[18]) if row[18] is not None else None,
            top_loser_return_pct=Decimal(row[19]) if row[19] is not None else None,
            available_at=row[20],
            universe=str(row[21]),
            historical_membership_mode=str(row[22]),
            schema_version=str(row[23]),
        )


def _snapshot_id(snapshot: SectorSnapshot) -> str:
    return "|".join(
        (
            snapshot.trade_date.isoformat(),
            snapshot.sector_code,
            snapshot.classification,
            snapshot.as_of_time.isoformat(),
            snapshot.historical_membership_mode,
            snapshot.schema_version,
        )
    )


def _membership_snapshot_id(snapshot: SectorMembershipSnapshot) -> str:
    return "|".join(
        (
            snapshot.as_of_date.isoformat(),
            snapshot.symbol,
            snapshot.sector_code,
            snapshot.classification,
            snapshot.historical_membership_mode,
            snapshot.schema_version,
        )
    )


_SECTOR_SNAPSHOT_TABLE_SQL = """
CREATE TEMP TABLE sector_snapshot_batch (
    snapshot_id VARCHAR, trade_date DATE, as_of_time TIMESTAMPTZ,
    sector_code VARCHAR, sector_name VARCHAR, classification VARCHAR,
    member_count INTEGER, valid_bar_count INTEGER,
    coverage_ratio DECIMAL(38, 18), equal_weight_return_pct DECIMAL(38, 18),
    median_return_pct DECIMAL(38, 18), advancer_count INTEGER,
    decliner_count INTEGER, flat_count INTEGER,
    advancer_ratio DECIMAL(38, 18), total_volume BIGINT,
    total_amount DECIMAL(38, 10), top_gainer_symbol VARCHAR,
    top_gainer_return_pct DECIMAL(38, 18), top_loser_symbol VARCHAR,
    top_loser_return_pct DECIMAL(38, 18), available_at TIMESTAMPTZ,
    universe VARCHAR, historical_membership_mode VARCHAR, schema_version VARCHAR
)
"""

_MEMBERSHIP_SNAPSHOT_TABLE_SQL = """
CREATE TEMP TABLE membership_snapshot_batch (
    snapshot_id VARCHAR, as_of_date DATE, symbol VARCHAR, sector_code VARCHAR,
    sector_name VARCHAR, classification VARCHAR, source VARCHAR,
    available_at TIMESTAMPTZ, historical_membership_mode VARCHAR,
    schema_version VARCHAR
)
"""
