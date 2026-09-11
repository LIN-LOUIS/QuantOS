"""Point-in-Time sector membership records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ._validation import require_aware, require_non_empty

_SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


@dataclass(frozen=True, slots=True)
class SectorMembership:
    """A provider-sourced industry membership with preserved raw semantics."""

    symbol: str
    company_name: str
    exchange: str
    sector_code: str
    sector_name: str
    classification: str
    raw_industry: str
    source: str
    effective_from: date
    available_at: datetime
    effective_to: date | None = None
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        if not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol must use the canonical 000001.SZ format")
        for field_name in (
            "company_name",
            "exchange",
            "sector_code",
            "sector_name",
            "classification",
            "raw_industry",
            "source",
        ):
            require_non_empty(getattr(self, field_name), field_name)
        require_aware(self.available_at, "available_at")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to cannot precede effective_from")

    def is_available_as_of(self, as_of_time: datetime) -> bool:
        require_aware(as_of_time, "as_of_time")
        return self.available_at <= as_of_time


@dataclass(frozen=True, slots=True)
class SectorSnapshot:
    """Deterministic daily analytics for the SHSE/SZSE A-share universe."""

    trade_date: date
    as_of_time: datetime
    sector_code: str
    sector_name: str
    classification: str
    member_count: int
    valid_bar_count: int
    coverage_ratio: Decimal
    equal_weight_return_pct: Decimal | None
    median_return_pct: Decimal | None
    advancer_count: int
    decliner_count: int
    flat_count: int
    advancer_ratio: Decimal | None
    total_volume: int
    total_amount: Decimal
    top_gainer_symbol: str | None
    top_gainer_return_pct: Decimal | None
    top_loser_symbol: str | None
    top_loser_return_pct: Decimal | None
    available_at: datetime
    universe: str = "shse_szse_a_share"
    historical_membership_mode: str = "strict_pit"
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        for field_name in (
            "sector_code",
            "sector_name",
            "classification",
            "universe",
            "historical_membership_mode",
        ):
            require_non_empty(getattr(self, field_name), field_name)
        if self.historical_membership_mode not in {
            "strict_pit",
            "current_membership_proxy",
        }:
            raise ValueError("historical_membership_mode is invalid")
        for field_name in ("as_of_time", "available_at"):
            require_aware(getattr(self, field_name), field_name)
        if self.available_at > self.as_of_time:
            raise ValueError("available_at cannot be after as_of_time")
        counts = (
            self.member_count,
            self.valid_bar_count,
            self.advancer_count,
            self.decliner_count,
            self.flat_count,
            self.total_volume,
        )
        if any(value < 0 for value in counts):
            raise ValueError("snapshot counts and total_volume must be non-negative")
        if self.member_count == 0 or self.valid_bar_count > self.member_count:
            raise ValueError("member_count must cover valid_bar_count")
        if self.advancer_count + self.decliner_count + self.flat_count != self.valid_bar_count:
            raise ValueError("breadth counts must equal valid_bar_count")
        if not Decimal("0") <= self.coverage_ratio <= Decimal("1"):
            raise ValueError("coverage_ratio must be between zero and one")
        if self.advancer_ratio is not None and not (
            Decimal("0") <= self.advancer_ratio <= Decimal("1")
        ):
            raise ValueError("advancer_ratio must be between zero and one")
        if self.total_amount < 0:
            raise ValueError("total_amount must be non-negative")


@dataclass(frozen=True, slots=True)
class SectorRank:
    rank: int
    sector_code: str
    sector_name: str
    return_pct: Decimal | None
    advancer_ratio: Decimal | None
    total_amount: Decimal

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError("rank must be positive")
        require_non_empty(self.sector_code, "sector_code")
        require_non_empty(self.sector_name, "sector_name")


@dataclass(frozen=True, slots=True)
class SectorMembershipSnapshot:
    """Daily archive record that never implies unproven historical membership."""

    as_of_date: date
    symbol: str
    sector_code: str
    sector_name: str
    classification: str
    source: str
    available_at: datetime
    historical_membership_mode: str = "strict_pit"
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        if not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol must use the canonical 000001.SZ format")
        for field_name in (
            "sector_code",
            "sector_name",
            "classification",
            "source",
            "historical_membership_mode",
        ):
            require_non_empty(getattr(self, field_name), field_name)
        require_aware(self.available_at, "available_at")
        if self.historical_membership_mode not in {
            "strict_pit",
            "current_membership_proxy",
        }:
            raise ValueError("historical_membership_mode is invalid")
        if (
            self.historical_membership_mode == "strict_pit"
            and self.available_at.date() > self.as_of_date
        ):
            raise ValueError("strict PIT membership cannot be available after as_of_date")
