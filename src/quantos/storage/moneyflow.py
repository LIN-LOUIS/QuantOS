"""Append-only Parquet/DuckDB persistence for canonical moneyflow records."""

from __future__ import annotations

import glob
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence
from uuid import uuid4

import duckdb

from quantos.config import Settings
from quantos.schemas import MoneyFlowRecord
from quantos.schemas._validation import require_aware

from .market import StorageError


class MoneyFlowRepository:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_directories()

    def write_records(self, records: Sequence[MoneyFlowRecord]) -> int:
        if not records:
            return 0
        existing = self._existing_ids(record.record_id for record in records)
        new_records: list[MoneyFlowRecord] = []
        seen = set(existing)
        for record in records:
            if record.record_id not in seen:
                new_records.append(record)
                seen.add(record.record_id)
        grouped: dict[tuple[date, str], list[MoneyFlowRecord]] = {}
        for record in new_records:
            grouped.setdefault((record.trade_date, record.source), []).append(record)
        for (trade_date, source), daily_records in grouped.items():
            target_dir = self.settings.moneyflow_dir / f"trade_date={trade_date}" / f"source={source}"
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"part-{uuid4().hex}.parquet"
            try:
                with self._connect() as connection:
                    connection.execute(_TABLE_SQL)
                    connection.executemany(
                        "INSERT INTO moneyflow_batch VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [self._row(record) for record in daily_records],
                    )
                    escaped = str(target).replace("'", "''")
                    connection.execute(f"COPY moneyflow_batch TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            except duckdb.Error as exc:
                raise StorageError(f"failed to write moneyflow data: {exc}") from exc
        return len(new_records)

    def read_by_date(self, trade_date: date, *, as_of_time: datetime) -> list[MoneyFlowRecord]:
        require_aware(as_of_time, "as_of_time")
        pattern = self.settings.moneyflow_dir / f"trade_date={trade_date}" / "source=*" / "*.parquet"
        return self._query(pattern, "trade_date = ? AND available_at <= ?", [trade_date, as_of_time])

    def load_history(self, symbol: str, *, end_date: date, lookback: int, as_of_time: datetime) -> list[MoneyFlowRecord]:
        require_aware(as_of_time, "as_of_time")
        if lookback < 1:
            raise ValueError("lookback must be positive")
        pattern = self.settings.moneyflow_dir / "trade_date=*" / "source=*" / "*.parquet"
        records = self._query(
            pattern,
            "symbol = ? AND trade_date <= ? AND available_at <= ?",
            [symbol, end_date, as_of_time],
        )
        return records[-lookback:]

    def _query(self, pattern: Path, where_clause: str, parameters: Sequence[object]) -> list[MoneyFlowRecord]:
        if not glob.glob(str(pattern)):
            return []
        escaped = str(pattern).replace("'", "''")
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT symbol, trade_date,
                           buy_sm_volume, buy_sm_amount, sell_sm_volume, sell_sm_amount,
                           buy_md_volume, buy_md_amount, sell_md_volume, sell_md_amount,
                           buy_lg_volume, buy_lg_amount, sell_lg_volume, sell_lg_amount,
                           buy_elg_volume, buy_elg_amount, sell_elg_volume, sell_elg_amount,
                           net_mf_volume, net_mf_amount, source, available_at,
                           collected_at, schema_version
                    FROM read_parquet('{escaped}', hive_partitioning = false)
                    WHERE {where_clause}
                    ORDER BY trade_date, symbol, source
                    """,
                    list(parameters),
                ).fetchall()
        except duckdb.Error as exc:
            raise StorageError(f"failed to query moneyflow data: {exc}") from exc
        try:
            return [self._from_row(row) for row in rows]
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise StorageError(f"stored moneyflow data is invalid: {exc}") from exc

    def _existing_ids(self, identities: Iterable[str]) -> set[str]:
        candidates = list(dict.fromkeys(identities))
        if not candidates or not list(self.settings.moneyflow_dir.rglob("*.parquet")):
            return set()
        pattern = str(self.settings.moneyflow_dir / "**" / "*.parquet").replace("'", "''")
        placeholders = ", ".join("?" for _ in candidates)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT DISTINCT record_id FROM read_parquet('{pattern}') WHERE record_id IN ({placeholders})",
                    candidates,
                ).fetchall()
        except duckdb.Error as exc:
            raise StorageError(f"failed to inspect moneyflow identities: {exc}") from exc
        return {str(row[0]) for row in rows}

    def _connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.settings.duckdb_path))

    @staticmethod
    def _row(record: MoneyFlowRecord) -> tuple[object, ...]:
        return (
            record.record_id, record.symbol, record.trade_date,
            record.buy_sm_volume, record.buy_sm_amount, record.sell_sm_volume, record.sell_sm_amount,
            record.buy_md_volume, record.buy_md_amount, record.sell_md_volume, record.sell_md_amount,
            record.buy_lg_volume, record.buy_lg_amount, record.sell_lg_volume, record.sell_lg_amount,
            record.buy_elg_volume, record.buy_elg_amount, record.sell_elg_volume, record.sell_elg_amount,
            record.net_mf_volume, record.net_mf_amount, record.source,
            record.available_at, record.collected_at, record.schema_version,
        )

    @staticmethod
    def _from_row(row: tuple[object, ...]) -> MoneyFlowRecord:
        return MoneyFlowRecord(
            symbol=str(row[0]), trade_date=row[1],
            buy_sm_volume=int(row[2]), buy_sm_amount=Decimal(row[3]), sell_sm_volume=int(row[4]), sell_sm_amount=Decimal(row[5]),
            buy_md_volume=int(row[6]), buy_md_amount=Decimal(row[7]), sell_md_volume=int(row[8]), sell_md_amount=Decimal(row[9]),
            buy_lg_volume=int(row[10]), buy_lg_amount=Decimal(row[11]), sell_lg_volume=int(row[12]), sell_lg_amount=Decimal(row[13]),
            buy_elg_volume=int(row[14]), buy_elg_amount=Decimal(row[15]), sell_elg_volume=int(row[16]), sell_elg_amount=Decimal(row[17]),
            net_mf_volume=int(row[18]), net_mf_amount=Decimal(row[19]), source=str(row[20]),
            available_at=row[21], collected_at=row[22], schema_version=str(row[23]),
        )


_TABLE_SQL = """
CREATE TEMP TABLE moneyflow_batch (
    record_id VARCHAR, symbol VARCHAR, trade_date DATE,
    buy_sm_volume BIGINT, buy_sm_amount DECIMAL(38, 4), sell_sm_volume BIGINT, sell_sm_amount DECIMAL(38, 4),
    buy_md_volume BIGINT, buy_md_amount DECIMAL(38, 4), sell_md_volume BIGINT, sell_md_amount DECIMAL(38, 4),
    buy_lg_volume BIGINT, buy_lg_amount DECIMAL(38, 4), sell_lg_volume BIGINT, sell_lg_amount DECIMAL(38, 4),
    buy_elg_volume BIGINT, buy_elg_amount DECIMAL(38, 4), sell_elg_volume BIGINT, sell_elg_amount DECIMAL(38, 4),
    net_mf_volume BIGINT, net_mf_amount DECIMAL(38, 4), source VARCHAR,
    available_at TIMESTAMPTZ, collected_at TIMESTAMPTZ, schema_version VARCHAR
)
"""
