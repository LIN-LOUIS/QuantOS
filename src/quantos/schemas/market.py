"""Provider-neutral normalized market bars."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ._validation import require_aware, require_non_empty

_SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


@dataclass(frozen=True, slots=True)
class MarketBar:
    symbol: str
    timestamp: datetime
    frequency: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    amount: Decimal
    source: str
    source_record_id: str
    collected_at: datetime
    available_at: datetime
    prev_close: Decimal | None = None
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        if not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol must use the canonical 000001.SZ format")
        for field_name in ("frequency", "source", "source_record_id", "schema_version"):
            require_non_empty(getattr(self, field_name), field_name)
        for field_name in ("timestamp", "collected_at", "available_at"):
            require_aware(getattr(self, field_name), field_name)
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be greater than or equal to OHLC values")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be less than or equal to OHLC values")
        if self.volume < 0 or self.amount < 0:
            raise ValueError("volume and amount must be non-negative")
        if self.collected_at < self.timestamp:
            raise ValueError("collected_at cannot precede the market timestamp")
        if self.available_at < self.timestamp:
            raise ValueError("available_at cannot precede the market timestamp")

    def is_available_as_of(self, as_of_time: datetime) -> bool:
        require_aware(as_of_time, "as_of_time")
        return self.available_at <= as_of_time
