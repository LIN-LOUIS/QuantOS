"""Market anomaly result schemas, separate from operational system health."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from ._validation import require_aware, require_non_empty

_SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ)$")


class AnomalyStatus(str, Enum):
    OK = "ok"
    INSUFFICIENT_HISTORY = "insufficient_history"
    NOT_COMPUTABLE = "not_computable"


@dataclass(frozen=True, slots=True)
class StockAnomaly:
    trade_date: date
    as_of_time: datetime
    symbol: str
    return_pct: Decimal | None
    return_zscore: Decimal | None
    price_anomaly: bool
    volume: int
    volume_median: Decimal | None
    volume_ratio: Decimal | None
    volume_anomaly: bool
    amount: Decimal
    amount_median: Decimal | None
    amount_ratio: Decimal | None
    amount_anomaly: bool
    history_observations: int
    status: AnomalyStatus
    available_at: datetime
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        if not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol must use a canonical SH/SZ symbol")
        _validate_common(
            self.as_of_time,
            self.available_at,
            self.history_observations,
            self.volume,
            self.amount,
        )


@dataclass(frozen=True, slots=True)
class SectorAnomaly:
    trade_date: date
    as_of_time: datetime
    sector_code: str
    sector_name: str
    classification: str
    return_pct: Decimal | None
    return_zscore: Decimal | None
    advancer_ratio: Decimal | None
    breadth_zscore: Decimal | None
    total_amount: Decimal
    amount_median: Decimal | None
    amount_ratio: Decimal | None
    price_anomaly: bool
    breadth_anomaly: bool
    amount_anomaly: bool
    history_observations: int
    status: AnomalyStatus
    available_at: datetime
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        for field_name in ("sector_code", "sector_name", "classification"):
            require_non_empty(getattr(self, field_name), field_name)
        _validate_common(
            self.as_of_time,
            self.available_at,
            self.history_observations,
            0,
            self.total_amount,
        )
        if self.advancer_ratio is not None and not (
            Decimal("0") <= self.advancer_ratio <= Decimal("1")
        ):
            raise ValueError("advancer_ratio must be between zero and one")


def _validate_common(
    as_of_time: datetime,
    available_at: datetime,
    history_observations: int,
    volume: int,
    amount: Decimal,
) -> None:
    require_aware(as_of_time, "as_of_time")
    require_aware(available_at, "available_at")
    if available_at > as_of_time:
        raise ValueError("available_at cannot be after as_of_time")
    if history_observations < 0:
        raise ValueError("history_observations must be non-negative")
    if volume < 0 or amount < 0:
        raise ValueError("volume and amount must be non-negative")
