"""Deterministic market-anomaly triage results."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ._validation import require_aware, require_non_empty

_SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ)$")
_SIGNAL_TYPES = {
    "price_volume_amount",
    "price_volume",
    "price_amount",
    "volume_amount",
    "price_only",
    "volume_only",
    "amount_only",
}
_SECTOR_CONTEXTS = {"sector_confirmed", "stock_specific", "mixed", "unavailable"}


@dataclass(frozen=True, slots=True)
class AnomalyCandidate:
    """A ranked anomaly enriched only with PIT-visible sector context."""

    trade_date: date
    as_of_time: datetime
    symbol: str
    sector_code: str | None
    sector_name: str | None
    classification: str | None
    return_pct: Decimal | None
    return_zscore: Decimal | None
    volume_ratio: Decimal | None
    amount_ratio: Decimal | None
    price_anomaly: bool
    volume_anomaly: bool
    amount_anomaly: bool
    signal_count: int
    signal_type: str
    sector_return_pct: Decimal | None
    sector_advancer_ratio: Decimal | None
    excess_return_pct: Decimal | None
    sector_context: str
    priority_tier: int
    rank: int
    history_observations: int
    available_at: datetime
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        if not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol must use a canonical SH/SZ symbol")
        require_aware(self.as_of_time, "as_of_time")
        require_aware(self.available_at, "available_at")
        if self.available_at > self.as_of_time:
            raise ValueError("available_at cannot be after as_of_time")
        if self.signal_count != sum(
            (self.price_anomaly, self.volume_anomaly, self.amount_anomaly)
        ):
            raise ValueError("signal_count must equal enabled anomaly signals")
        if self.signal_count < 1 or self.signal_count > 3:
            raise ValueError("candidate must contain between one and three signals")
        if self.signal_type not in _SIGNAL_TYPES:
            raise ValueError("signal_type is invalid")
        if self.sector_context not in _SECTOR_CONTEXTS:
            raise ValueError("sector_context is invalid")
        if not 1 <= self.priority_tier <= 5:
            raise ValueError("priority_tier must be between 1 and 5")
        if self.rank < 1:
            raise ValueError("rank must be positive")
        if self.history_observations < 0:
            raise ValueError("history_observations must be non-negative")
        sector_fields = (self.sector_code, self.sector_name, self.classification)
        if any(value is None for value in sector_fields) and any(
            value is not None for value in sector_fields
        ):
            raise ValueError("sector identity fields must be all present or all absent")
        for value, name in zip(
            sector_fields, ("sector_code", "sector_name", "classification")
        ):
            if value is not None:
                require_non_empty(value, name)
        if self.sector_advancer_ratio is not None and not (
            Decimal("0") <= self.sector_advancer_ratio <= Decimal("1")
        ):
            raise ValueError("sector_advancer_ratio must be between zero and one")
