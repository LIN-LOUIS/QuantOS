"""Deterministic daily sector analytics over canonical market bars."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from quantos.schemas import MarketBar, SectorMembership, SectorRank, SectorSnapshot
from quantos.schemas._validation import require_aware

RETURN_EPSILON_PCT = Decimal("0.00000001")
UNIVERSE = "shse_szse_a_share"
_SUPPORTED_EXCHANGES = {"SHSE", "SZSE"}


class SectorAnalyticsError(ValueError):
    """Inputs cannot be joined deterministically without guessing."""


def calculate_stock_return_pct(bar: MarketBar) -> Decimal | None:
    """Return close-to-previous-close percentage, or None for an invalid base."""

    if bar.close <= 0 or bar.prev_close is None or bar.prev_close <= 0:
        return None
    return (bar.close / bar.prev_close - Decimal("1")) * Decimal("100")


def build_sector_snapshots(
    bars: Sequence[MarketBar],
    memberships: Sequence[SectorMembership],
    *,
    trade_date: date,
    as_of_time: datetime,
    epsilon: Decimal = RETURN_EPSILON_PCT,
    historical_membership_mode: str = "strict_pit",
) -> list[SectorSnapshot]:
    """PIT join daily bars to effective SHSE/SZSE memberships by symbol."""

    require_aware(as_of_time, "as_of_time")
    if epsilon < 0 or not epsilon.is_finite():
        raise ValueError("epsilon must be a finite non-negative Decimal")
    current_memberships = _current_memberships(
        memberships, trade_date=trade_date, as_of_time=as_of_time
    )
    visible_bars = _visible_daily_bars(
        bars, trade_date=trade_date, as_of_time=as_of_time
    )

    membership_groups: dict[tuple[str, str, str], list[SectorMembership]] = defaultdict(list)
    for membership in current_memberships.values():
        membership_groups[
            (
                membership.sector_code,
                membership.sector_name,
                membership.classification,
            )
        ].append(membership)

    snapshots = [
        _build_snapshot(
            key=key,
            members=members,
            bars_by_symbol=visible_bars,
            trade_date=trade_date,
            as_of_time=as_of_time,
            epsilon=epsilon,
            historical_membership_mode=historical_membership_mode,
        )
        for key, members in sorted(membership_groups.items())
    ]
    return snapshots


def rank_sectors(
    snapshots: Sequence[SectorSnapshot], *, descending: bool = True
) -> list[SectorRank]:
    """Rank equal-weight returns with stable sector-code tie-breaks."""

    ordered = sorted(
        snapshots,
        key=lambda snapshot: (
            snapshot.equal_weight_return_pct is None,
            (
                -snapshot.equal_weight_return_pct
                if descending
                else snapshot.equal_weight_return_pct
            )
            if snapshot.equal_weight_return_pct is not None
            else Decimal("0"),
            snapshot.sector_code,
            snapshot.sector_name,
        ),
    )
    return [
        SectorRank(
            rank=index,
            sector_code=snapshot.sector_code,
            sector_name=snapshot.sector_name,
            return_pct=snapshot.equal_weight_return_pct,
            advancer_ratio=snapshot.advancer_ratio,
            total_amount=snapshot.total_amount,
        )
        for index, snapshot in enumerate(ordered, start=1)
    ]


def _build_snapshot(
    *,
    key: tuple[str, str, str],
    members: Sequence[SectorMembership],
    bars_by_symbol: dict[str, MarketBar],
    trade_date: date,
    as_of_time: datetime,
    epsilon: Decimal,
    historical_membership_mode: str,
) -> SectorSnapshot:
    returns: list[tuple[str, Decimal, MarketBar]] = []
    for member in members:
        bar = bars_by_symbol.get(member.symbol)
        if bar is None:
            continue
        return_pct = calculate_stock_return_pct(bar)
        if return_pct is not None:
            returns.append((member.symbol, return_pct, bar))

    returns.sort(key=lambda item: item[0])
    values = sorted(item[1] for item in returns)
    valid_count = len(returns)
    member_count = len(members)
    advancers = sum(value > epsilon for value in values)
    decliners = sum(value < -epsilon for value in values)
    flats = valid_count - advancers - decliners
    gainer = min(returns, key=lambda item: (-item[1], item[0])) if returns else None
    loser = min(returns, key=lambda item: (item[1], item[0])) if returns else None
    availability = max(
        [member.available_at for member in members]
        + [item[2].available_at for item in returns]
    )
    return SectorSnapshot(
        trade_date=trade_date,
        as_of_time=as_of_time,
        sector_code=key[0],
        sector_name=key[1],
        classification=key[2],
        member_count=member_count,
        valid_bar_count=valid_count,
        coverage_ratio=Decimal(valid_count) / Decimal(member_count),
        equal_weight_return_pct=(
            sum(values, start=Decimal("0")) / Decimal(valid_count)
            if values
            else None
        ),
        median_return_pct=_median(values),
        advancer_count=advancers,
        decliner_count=decliners,
        flat_count=flats,
        advancer_ratio=(
            Decimal(advancers) / Decimal(valid_count) if valid_count else None
        ),
        total_volume=sum(item[2].volume for item in returns),
        total_amount=sum((item[2].amount for item in returns), start=Decimal("0")),
        top_gainer_symbol=gainer[0] if gainer else None,
        top_gainer_return_pct=gainer[1] if gainer else None,
        top_loser_symbol=loser[0] if loser else None,
        top_loser_return_pct=loser[1] if loser else None,
        available_at=availability,
        universe=UNIVERSE,
        historical_membership_mode=historical_membership_mode,
    )


def _current_memberships(
    memberships: Sequence[SectorMembership],
    *,
    trade_date: date,
    as_of_time: datetime,
) -> dict[str, SectorMembership]:
    current: dict[str, SectorMembership] = {}
    for membership in memberships:
        if (
            membership.exchange not in _SUPPORTED_EXCHANGES
            or not membership.sector_code
            or not membership.sector_name
            or not membership.classification
            or membership.available_at > as_of_time
            or membership.effective_from > trade_date
            or (
                membership.effective_to is not None
                and membership.effective_to <= trade_date
            )
        ):
            continue
        incumbent = current.get(membership.symbol)
        if incumbent is None or (
            membership.available_at,
            membership.effective_from,
            membership.source,
            membership.sector_code,
        ) > (
            incumbent.available_at,
            incumbent.effective_from,
            incumbent.source,
            incumbent.sector_code,
        ):
            current[membership.symbol] = membership
    return current


def _visible_daily_bars(
    bars: Sequence[MarketBar], *, trade_date: date, as_of_time: datetime
) -> dict[str, MarketBar]:
    visible: dict[str, MarketBar] = {}
    for bar in bars:
        if (
            bar.frequency != "1d"
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


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    midpoint = len(values) // 2
    if len(values) % 2:
        return values[midpoint]
    return (values[midpoint - 1] + values[midpoint]) / Decimal("2")
