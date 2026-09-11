"""Deterministic Point-in-Time fund-flow evidence and aggregations."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Sequence

from quantos.schemas import (
    AnomalyCandidate,
    CandidateFundFlowEvidence,
    FundFlowEvidence,
    MarketBar,
    MarketFundFlow,
    MoneyFlowRecord,
    SectorFundFlow,
    SectorMembership,
)
from quantos.schemas._validation import require_aware

FLOW_EPSILON_AMOUNT = Decimal("0.000001")


def build_fund_flow_evidence(
    record: MoneyFlowRecord,
    market_bar: MarketBar,
    *,
    as_of_time: datetime,
    epsilon: Decimal = FLOW_EPSILON_AMOUNT,
) -> FundFlowEvidence:
    require_aware(as_of_time, "as_of_time")
    _validate_epsilon(epsilon)
    if record.available_at > as_of_time or market_bar.available_at > as_of_time:
        raise ValueError("fund-flow evidence input is not PIT-visible")
    if record.symbol != market_bar.symbol or record.trade_date != market_bar.timestamp.date():
        raise ValueError("MoneyFlowRecord and MarketBar identities must match")
    large = record.buy_lg_amount - record.sell_lg_amount
    extra_large = record.buy_elg_amount - record.sell_elg_amount
    combined = large + extra_large
    denominator = market_bar.amount
    return FundFlowEvidence(
        trade_date=record.trade_date,
        as_of_time=as_of_time,
        symbol=record.symbol,
        net_mf_amount=record.net_mf_amount,
        net_mf_ratio=(record.net_mf_amount / denominator if denominator > 0 else None),
        large_net_amount=large,
        extra_large_net_amount=extra_large,
        large_plus_extra_net_amount=combined,
        large_plus_extra_ratio=(combined / denominator if denominator > 0 else None),
        small_net_amount=record.buy_sm_amount - record.sell_sm_amount,
        medium_net_amount=record.buy_md_amount - record.sell_md_amount,
        flow_direction=_direction(record.net_mf_amount, epsilon),
        available_at=max(record.available_at, market_bar.available_at),
    )


def enrich_candidates_with_fund_flow(
    candidates: Sequence[AnomalyCandidate],
    records: Sequence[MoneyFlowRecord],
    market_bars: Sequence[MarketBar],
    *,
    as_of_time: datetime,
    sector_flows: Sequence[SectorFundFlow] = (),
) -> list[CandidateFundFlowEvidence]:
    """Attach evidence without changing candidate rank, tier, or signals."""

    require_aware(as_of_time, "as_of_time")
    flows = _visible_records(records, as_of_time)
    bars = {
        (bar.timestamp.date(), bar.symbol): bar
        for bar in market_bars
        if bar.available_at <= as_of_time and bar.frequency == "1d"
    }
    sectors = {
        (item.trade_date, item.sector_code, item.classification): item
        for item in sector_flows
        if item.available_at <= as_of_time and item.as_of_time <= as_of_time
    }
    output: list[CandidateFundFlowEvidence] = []
    for candidate in sorted(candidates, key=lambda item: item.rank):
        if candidate.available_at > as_of_time or candidate.as_of_time > as_of_time:
            continue
        key = (candidate.trade_date, candidate.symbol)
        record = flows.get(key)
        bar = bars.get(key)
        evidence = (
            build_fund_flow_evidence(record, bar, as_of_time=as_of_time)
            if record is not None and bar is not None
            else None
        )
        sector = (
            sectors.get((candidate.trade_date, candidate.sector_code, candidate.classification))
            if candidate.sector_code is not None and candidate.classification is not None
            else None
        )
        availability = [candidate.available_at]
        if evidence is not None:
            availability.append(evidence.available_at)
        if sector is not None:
            availability.append(sector.available_at)
        output.append(
            CandidateFundFlowEvidence(
                candidate=candidate,
                fund_flow=evidence,
                sector_net_mf_amount=sector.net_mf_amount if sector is not None else None,
                available_at=max(availability),
            )
        )
    return output


def build_sector_fund_flows(
    records: Sequence[MoneyFlowRecord],
    memberships: Sequence[SectorMembership],
    *,
    trade_date: date,
    as_of_time: datetime,
    epsilon: Decimal = FLOW_EPSILON_AMOUNT,
) -> list[SectorFundFlow]:
    require_aware(as_of_time, "as_of_time")
    _validate_epsilon(epsilon)
    current_memberships = _visible_memberships(memberships, trade_date, as_of_time)
    flows = _visible_records(records, as_of_time, trade_date=trade_date)
    groups: dict[tuple[str, str, str], list[SectorMembership]] = defaultdict(list)
    for membership in current_memberships.values():
        groups[(membership.sector_code, membership.sector_name, membership.classification)].append(membership)
    output: list[SectorFundFlow] = []
    for key, members in sorted(groups.items()):
        member_flows = [flows[(trade_date, item.symbol)] for item in members if (trade_date, item.symbol) in flows]
        large = [item.buy_lg_amount - item.sell_lg_amount for item in member_flows]
        extra = [item.buy_elg_amount - item.sell_elg_amount for item in member_flows]
        availability = [item.available_at for item in members] + [item.available_at for item in member_flows]
        output.append(
            SectorFundFlow(
                trade_date=trade_date,
                as_of_time=as_of_time,
                sector_code=key[0],
                sector_name=key[1],
                classification=key[2],
                member_count=len(members),
                valid_moneyflow_count=len(member_flows),
                coverage_ratio=Decimal(len(member_flows)) / Decimal(len(members)),
                net_mf_amount=sum((item.net_mf_amount for item in member_flows), Decimal("0")),
                large_net_amount=sum(large, Decimal("0")),
                extra_large_net_amount=sum(extra, Decimal("0")),
                large_plus_extra_net_amount=sum(large, Decimal("0")) + sum(extra, Decimal("0")),
                inflow_stock_count=sum(item.net_mf_amount > epsilon for item in member_flows),
                outflow_stock_count=sum(item.net_mf_amount < -epsilon for item in member_flows),
                flat_stock_count=sum(abs(item.net_mf_amount) <= epsilon for item in member_flows),
                available_at=max(availability),
            )
        )
    return output


def rank_sector_fund_flows(
    sector_flows: Sequence[SectorFundFlow], *, descending: bool = True
) -> list[SectorFundFlow]:
    return sorted(
        sector_flows,
        key=lambda item: (
            -item.net_mf_amount if descending else item.net_mf_amount,
            item.sector_code,
            item.sector_name,
        ),
    )


def build_market_fund_flow(
    records: Sequence[MoneyFlowRecord],
    *,
    trade_date: date,
    as_of_time: datetime,
    epsilon: Decimal = FLOW_EPSILON_AMOUNT,
) -> MarketFundFlow:
    require_aware(as_of_time, "as_of_time")
    _validate_epsilon(epsilon)
    flows = list(_visible_records(records, as_of_time, trade_date=trade_date).values())
    combined = [
        (item.buy_lg_amount - item.sell_lg_amount)
        + (item.buy_elg_amount - item.sell_elg_amount)
        for item in flows
    ]
    return MarketFundFlow(
        trade_date=trade_date,
        as_of_time=as_of_time,
        valid_moneyflow_count=len(flows),
        net_mf_amount=sum((item.net_mf_amount for item in flows), Decimal("0")),
        large_plus_extra_net_amount=sum(combined, Decimal("0")),
        inflow_stock_count=sum(item.net_mf_amount > epsilon for item in flows),
        outflow_stock_count=sum(item.net_mf_amount < -epsilon for item in flows),
        flat_stock_count=sum(abs(item.net_mf_amount) <= epsilon for item in flows),
        available_at=max((item.available_at for item in flows), default=as_of_time),
    )


def _visible_records(
    records: Sequence[MoneyFlowRecord],
    as_of_time: datetime,
    *,
    trade_date: date | None = None,
) -> dict[tuple[date, str], MoneyFlowRecord]:
    visible: dict[tuple[date, str], MoneyFlowRecord] = {}
    for record in records:
        if record.available_at > as_of_time or (trade_date is not None and record.trade_date != trade_date):
            continue
        key = (record.trade_date, record.symbol)
        incumbent = visible.get(key)
        if incumbent is None or (record.available_at, record.collected_at, record.source) > (
            incumbent.available_at, incumbent.collected_at, incumbent.source
        ):
            visible[key] = record
    return visible


def _visible_memberships(
    memberships: Sequence[SectorMembership], trade_date: date, as_of_time: datetime
) -> dict[str, SectorMembership]:
    visible: dict[str, SectorMembership] = {}
    for item in memberships:
        if item.available_at > as_of_time or item.effective_from > trade_date or (
            item.effective_to is not None and item.effective_to <= trade_date
        ):
            continue
        incumbent = visible.get(item.symbol)
        if incumbent is None or (item.available_at, item.effective_from, item.source, item.sector_code) > (
            incumbent.available_at, incumbent.effective_from, incumbent.source, incumbent.sector_code
        ):
            visible[item.symbol] = item
    return visible


def _direction(value: Decimal, epsilon: Decimal) -> str:
    if value > epsilon:
        return "inflow"
    if value < -epsilon:
        return "outflow"
    return "flat"


def _validate_epsilon(epsilon: Decimal) -> None:
    if not epsilon.is_finite() or epsilon < 0:
        raise ValueError("epsilon must be finite and non-negative")
