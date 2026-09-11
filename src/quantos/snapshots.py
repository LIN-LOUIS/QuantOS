"""Pure deterministic MarketSnapshot construction."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from quantos.schemas import MarketBar, MarketSnapshot, SectorSnapshot
from quantos.schemas._validation import require_aware
from quantos.sectors.analytics import (
    RETURN_EPSILON_PCT,
    UNIVERSE,
    SectorAnalyticsError,
    calculate_stock_return_pct,
    rank_sectors,
)

_MEMBER_SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ)$")


def build_market_snapshot(
    bars: Sequence[MarketBar],
    sector_snapshots: Sequence[SectorSnapshot],
    *,
    security_member_symbols: Sequence[str],
    trade_date: date,
    as_of_time: datetime,
    top_n: int = 10,
) -> MarketSnapshot:
    """Aggregate explicit SH/SZ inputs without acquiring or persisting data.

    ``security_member_symbols`` must come from the caller's PIT-resolved
    SectorMembership set. Until those observations are historically archived,
    this function cannot by itself guarantee historical membership completeness.
    """

    require_aware(as_of_time, "as_of_time")
    if top_n < 1:
        raise ValueError("top_n must be positive")
    member_symbols = _validate_member_symbols(security_member_symbols)
    visible_bars = _visible_member_bars(
        bars,
        member_symbols=member_symbols,
        trade_date=trade_date,
        as_of_time=as_of_time,
    )
    valid_returns: dict[str, tuple[Decimal, MarketBar]] = {}
    for symbol, bar in visible_bars.items():
        return_pct = calculate_stock_return_pct(bar)
        if return_pct is not None:
            valid_returns[symbol] = (return_pct, bar)

    visible_sectors = _visible_sector_snapshots(
        sector_snapshots, trade_date=trade_date, as_of_time=as_of_time
    )
    market_returns = [item[0] for item in valid_returns.values()]
    advancers = sum(value > RETURN_EPSILON_PCT for value in market_returns)
    decliners = sum(value < -RETURN_EPSILON_PCT for value in market_returns)
    flats = len(market_returns) - advancers - decliners

    sector_advancers = sum(
        snapshot.equal_weight_return_pct is not None
        and snapshot.equal_weight_return_pct > RETURN_EPSILON_PCT
        for snapshot in visible_sectors
    )
    sector_decliners = sum(
        snapshot.equal_weight_return_pct is not None
        and snapshot.equal_weight_return_pct < -RETURN_EPSILON_PCT
        for snapshot in visible_sectors
    )
    sector_flats = len(visible_sectors) - sector_advancers - sector_decliners

    descending_ranks = rank_sectors(visible_sectors, descending=True)
    ascending_ranks = rank_sectors(visible_sectors, descending=False)
    top_codes = tuple(
        rank.sector_code
        for rank in descending_ranks
        if rank.return_pct is not None
    )[:top_n]
    bottom_codes = tuple(
        rank.sector_code for rank in ascending_ranks if rank.return_pct is not None
    )[:top_n]
    valid_symbols = set(valid_returns)
    missing_symbols = tuple(sorted(member_symbols.difference(valid_symbols)))
    input_availability = [bar.available_at for bar in visible_bars.values()] + [
        snapshot.available_at for snapshot in visible_sectors
    ]
    available_at = max(input_availability) if input_availability else as_of_time
    member_count = len(member_symbols)
    valid_count = len(valid_returns)
    return MarketSnapshot(
        trade_date=trade_date,
        as_of_time=as_of_time,
        universe=UNIVERSE,
        security_member_count=member_count,
        valid_bar_count=valid_count,
        coverage_ratio=(
            Decimal(valid_count) / Decimal(member_count)
            if member_count
            else Decimal("0")
        ),
        advancer_count=advancers,
        decliner_count=decliners,
        flat_count=flats,
        advancer_ratio=(
            Decimal(advancers) / Decimal(valid_count) if valid_count else None
        ),
        total_volume=sum(item[1].volume for item in valid_returns.values()),
        total_amount=sum(
            (item[1].amount for item in valid_returns.values()),
            start=Decimal("0"),
        ),
        sector_count=len(visible_sectors),
        sector_advancer_count=sector_advancers,
        sector_decliner_count=sector_decliners,
        sector_flat_count=sector_flats,
        top_sector_codes=top_codes,
        bottom_sector_codes=bottom_codes,
        incomplete_coverage_sector_count=sum(
            snapshot.coverage_ratio < Decimal("1") for snapshot in visible_sectors
        ),
        missing_bar_count=len(missing_symbols),
        missing_bar_symbols=missing_symbols,
        available_at=available_at,
    )


def _validate_member_symbols(symbols: Sequence[str]) -> set[str]:
    result = set(symbols)
    if len(result) != len(symbols):
        raise ValueError("security_member_symbols must be unique")
    if not all(_MEMBER_SYMBOL_PATTERN.fullmatch(symbol) for symbol in result):
        raise ValueError("security_member_symbols must contain canonical SH/SZ symbols")
    return result


def _visible_member_bars(
    bars: Sequence[MarketBar],
    *,
    member_symbols: set[str],
    trade_date: date,
    as_of_time: datetime,
) -> dict[str, MarketBar]:
    visible: dict[str, MarketBar] = {}
    for bar in bars:
        if (
            bar.symbol not in member_symbols
            or bar.frequency != "1d"
            or bar.timestamp.date() != trade_date
            or bar.available_at > as_of_time
        ):
            continue
        if bar.symbol in visible:
            raise SectorAnalyticsError(
                f"multiple PIT-visible daily bars for symbol {bar.symbol}"
            )
        visible[bar.symbol] = bar
    return visible


def _visible_sector_snapshots(
    snapshots: Sequence[SectorSnapshot],
    *,
    trade_date: date,
    as_of_time: datetime,
) -> list[SectorSnapshot]:
    visible: dict[tuple[str, str, str], SectorSnapshot] = {}
    for snapshot in snapshots:
        if (
            snapshot.trade_date != trade_date
            or snapshot.available_at > as_of_time
            or snapshot.universe != UNIVERSE
        ):
            continue
        key = (snapshot.sector_code, snapshot.sector_name, snapshot.classification)
        if key in visible:
            raise SectorAnalyticsError(
                f"multiple PIT-visible snapshots for sector {snapshot.sector_code}"
            )
        visible[key] = snapshot
    return sorted(visible.values(), key=lambda snapshot: (
        snapshot.sector_code,
        snapshot.sector_name,
        snapshot.classification,
    ))
