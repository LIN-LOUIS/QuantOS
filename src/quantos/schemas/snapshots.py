"""Deterministic whole-market snapshot schemas."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ._validation import require_aware, require_non_empty

_SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ)$")


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Daily SHSE/SZSE aggregate built only from explicit PIT-visible inputs.

    Historical accuracy remains limited until SectorMembership observations are
    archived over time; this schema does not reconstruct membership from a later
    SecurityMaster snapshot.
    """

    trade_date: date
    as_of_time: datetime
    universe: str
    security_member_count: int
    valid_bar_count: int
    coverage_ratio: Decimal
    advancer_count: int
    decliner_count: int
    flat_count: int
    advancer_ratio: Decimal | None
    total_volume: int
    total_amount: Decimal
    sector_count: int
    sector_advancer_count: int
    sector_decliner_count: int
    sector_flat_count: int
    top_sector_codes: tuple[str, ...]
    bottom_sector_codes: tuple[str, ...]
    incomplete_coverage_sector_count: int
    missing_bar_count: int
    missing_bar_symbols: tuple[str, ...]
    available_at: datetime
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        require_non_empty(self.universe, "universe")
        if self.universe != "shse_szse_a_share":
            raise ValueError("universe must be shse_szse_a_share")
        for field_name in ("as_of_time", "available_at"):
            require_aware(getattr(self, field_name), field_name)
        if self.available_at > self.as_of_time:
            raise ValueError("available_at cannot be after as_of_time")
        counts = (
            self.security_member_count,
            self.valid_bar_count,
            self.advancer_count,
            self.decliner_count,
            self.flat_count,
            self.total_volume,
            self.sector_count,
            self.sector_advancer_count,
            self.sector_decliner_count,
            self.sector_flat_count,
            self.incomplete_coverage_sector_count,
            self.missing_bar_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("snapshot counts and total_volume must be non-negative")
        if self.valid_bar_count > self.security_member_count:
            raise ValueError("security_member_count must cover valid_bar_count")
        if self.missing_bar_count != self.security_member_count - self.valid_bar_count:
            raise ValueError("missing_bar_count must match coverage counts")
        if len(self.missing_bar_symbols) != self.missing_bar_count:
            raise ValueError("missing_bar_symbols must match missing_bar_count")
        if tuple(sorted(self.missing_bar_symbols)) != self.missing_bar_symbols:
            raise ValueError("missing_bar_symbols must use deterministic ordering")
        if not all(_SYMBOL_PATTERN.fullmatch(symbol) for symbol in self.missing_bar_symbols):
            raise ValueError("missing_bar_symbols must contain canonical SH/SZ symbols")
        if self.advancer_count + self.decliner_count + self.flat_count != self.valid_bar_count:
            raise ValueError("market breadth counts must equal valid_bar_count")
        if (
            self.sector_advancer_count
            + self.sector_decliner_count
            + self.sector_flat_count
            != self.sector_count
        ):
            raise ValueError("sector breadth counts must equal sector_count")
        if not Decimal("0") <= self.coverage_ratio <= Decimal("1"):
            raise ValueError("coverage_ratio must be between zero and one")
        if self.advancer_ratio is not None and not (
            Decimal("0") <= self.advancer_ratio <= Decimal("1")
        ):
            raise ValueError("advancer_ratio must be between zero and one")
        if self.total_amount < 0:
            raise ValueError("total_amount must be non-negative")
