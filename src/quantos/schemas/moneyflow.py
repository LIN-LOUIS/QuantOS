"""Canonical fund-flow records and deterministic evidence aggregates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ._validation import require_aware, require_non_empty
from .candidates import AnomalyCandidate

_SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ)$")


@dataclass(frozen=True, slots=True)
class MoneyFlowRecord:
    symbol: str
    trade_date: date
    buy_sm_volume: int
    buy_sm_amount: Decimal
    sell_sm_volume: int
    sell_sm_amount: Decimal
    buy_md_volume: int
    buy_md_amount: Decimal
    sell_md_volume: int
    sell_md_amount: Decimal
    buy_lg_volume: int
    buy_lg_amount: Decimal
    sell_lg_volume: int
    sell_lg_amount: Decimal
    buy_elg_volume: int
    buy_elg_amount: Decimal
    sell_elg_volume: int
    sell_elg_amount: Decimal
    net_mf_volume: int
    net_mf_amount: Decimal
    source: str
    available_at: datetime
    collected_at: datetime
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        if not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol must use a canonical SH/SZ symbol")
        for name in ("source", "schema_version"):
            require_non_empty(getattr(self, name), name)
        for name in ("available_at", "collected_at"):
            require_aware(getattr(self, name), name)
        volumes = (
            self.buy_sm_volume,
            self.sell_sm_volume,
            self.buy_md_volume,
            self.sell_md_volume,
            self.buy_lg_volume,
            self.sell_lg_volume,
            self.buy_elg_volume,
            self.sell_elg_volume,
        )
        amounts = (
            self.buy_sm_amount,
            self.sell_sm_amount,
            self.buy_md_amount,
            self.sell_md_amount,
            self.buy_lg_amount,
            self.sell_lg_amount,
            self.buy_elg_amount,
            self.sell_elg_amount,
        )
        if any(value < 0 for value in volumes + amounts):
            raise ValueError("buy/sell moneyflow values must be non-negative")

    @property
    def record_id(self) -> str:
        return f"{self.source}:{self.symbol}:moneyflow:{self.trade_date.isoformat()}"


@dataclass(frozen=True, slots=True)
class FundFlowEvidence:
    trade_date: date
    as_of_time: datetime
    symbol: str
    net_mf_amount: Decimal
    net_mf_ratio: Decimal | None
    large_net_amount: Decimal
    extra_large_net_amount: Decimal
    large_plus_extra_net_amount: Decimal
    large_plus_extra_ratio: Decimal | None
    small_net_amount: Decimal
    medium_net_amount: Decimal
    flow_direction: str
    available_at: datetime
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        if not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol must use a canonical SH/SZ symbol")
        require_aware(self.as_of_time, "as_of_time")
        require_aware(self.available_at, "available_at")
        if self.available_at > self.as_of_time:
            raise ValueError("available_at cannot be after as_of_time")
        if self.flow_direction not in {"inflow", "outflow", "flat"}:
            raise ValueError("flow_direction is invalid")


@dataclass(frozen=True, slots=True)
class CandidateFundFlowEvidence:
    candidate: AnomalyCandidate
    fund_flow: FundFlowEvidence | None
    sector_net_mf_amount: Decimal | None
    available_at: datetime
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        require_aware(self.available_at, "available_at")
        if self.available_at > self.candidate.as_of_time:
            raise ValueError("available_at cannot be after candidate as_of_time")
        if self.fund_flow is not None:
            if self.fund_flow.symbol != self.candidate.symbol:
                raise ValueError("candidate and fund flow symbols must match")
            if self.fund_flow.trade_date != self.candidate.trade_date:
                raise ValueError("candidate and fund flow dates must match")


@dataclass(frozen=True, slots=True)
class SectorFundFlow:
    trade_date: date
    as_of_time: datetime
    sector_code: str
    sector_name: str
    classification: str
    member_count: int
    valid_moneyflow_count: int
    coverage_ratio: Decimal
    net_mf_amount: Decimal
    large_net_amount: Decimal
    extra_large_net_amount: Decimal
    large_plus_extra_net_amount: Decimal
    inflow_stock_count: int
    outflow_stock_count: int
    flat_stock_count: int
    available_at: datetime
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        for name in ("sector_code", "sector_name", "classification"):
            require_non_empty(getattr(self, name), name)
        require_aware(self.as_of_time, "as_of_time")
        require_aware(self.available_at, "available_at")
        if self.available_at > self.as_of_time:
            raise ValueError("available_at cannot be after as_of_time")
        if self.member_count < 1 or not 0 <= self.valid_moneyflow_count <= self.member_count:
            raise ValueError("sector member counts are invalid")
        if not Decimal("0") <= self.coverage_ratio <= Decimal("1"):
            raise ValueError("coverage_ratio must be between zero and one")
        if self.inflow_stock_count + self.outflow_stock_count + self.flat_stock_count != self.valid_moneyflow_count:
            raise ValueError("flow breadth counts must equal valid_moneyflow_count")


@dataclass(frozen=True, slots=True)
class MarketFundFlow:
    trade_date: date
    as_of_time: datetime
    valid_moneyflow_count: int
    net_mf_amount: Decimal
    large_plus_extra_net_amount: Decimal
    inflow_stock_count: int
    outflow_stock_count: int
    flat_stock_count: int
    available_at: datetime
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        require_aware(self.as_of_time, "as_of_time")
        require_aware(self.available_at, "available_at")
        if self.available_at > self.as_of_time:
            raise ValueError("available_at cannot be after as_of_time")
        if self.valid_moneyflow_count < 0:
            raise ValueError("valid_moneyflow_count cannot be negative")
        if self.inflow_stock_count + self.outflow_stock_count + self.flat_stock_count != self.valid_moneyflow_count:
            raise ValueError("flow breadth counts must equal valid_moneyflow_count")
