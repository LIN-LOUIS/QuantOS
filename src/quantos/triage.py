"""Pure deterministic triage and ranking of stock market anomalies."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from quantos.schemas import (
    AnomalyCandidate,
    SectorMembership,
    SectorSnapshot,
    StockAnomaly,
)
from quantos.schemas._validation import require_aware


@dataclass(frozen=True, slots=True)
class AnomalyTriageConfig:
    """Objective context thresholds; no weighted composite score."""

    excess_return_threshold_pct: Decimal = Decimal("5.0")
    direction_epsilon_pct: Decimal = Decimal("0.00000001")

    def __post_init__(self) -> None:
        if (
            not self.excess_return_threshold_pct.is_finite()
            or self.excess_return_threshold_pct <= 0
        ):
            raise ValueError("excess_return_threshold_pct must be finite and positive")
        if (
            not self.direction_epsilon_pct.is_finite()
            or self.direction_epsilon_pct < 0
        ):
            raise ValueError("direction_epsilon_pct must be finite and non-negative")


def rank_anomaly_candidates(
    anomalies: Sequence[StockAnomaly],
    memberships: Sequence[SectorMembership],
    sector_snapshots: Sequence[SectorSnapshot],
    *,
    as_of_time: datetime,
    top_n: int | None = 20,
    config: AnomalyTriageConfig = AnomalyTriageConfig(),
) -> list[AnomalyCandidate]:
    """PIT-enrich visible anomaly signals and apply stable tuple ordering."""

    require_aware(as_of_time, "as_of_time")
    if top_n is not None and top_n < 0:
        raise ValueError("top_n must be non-negative or None")
    visible_anomalies = _latest_visible_anomalies(anomalies, as_of_time)
    candidates = [
        _build_candidate(
            anomaly,
            memberships,
            sector_snapshots,
            as_of_time=as_of_time,
            config=config,
        )
        for anomaly in visible_anomalies
        if any(
            (anomaly.price_anomaly, anomaly.volume_anomaly, anomaly.amount_anomaly)
        )
    ]
    ordered = sorted(candidates, key=_ranking_key)
    if top_n is not None:
        ordered = ordered[:top_n]
    return [replace(candidate, rank=index) for index, candidate in enumerate(ordered, 1)]


def _build_candidate(
    anomaly: StockAnomaly,
    memberships: Sequence[SectorMembership],
    snapshots: Sequence[SectorSnapshot],
    *,
    as_of_time: datetime,
    config: AnomalyTriageConfig,
) -> AnomalyCandidate:
    signal_type, tier = _signal_type_and_tier(anomaly)
    membership = _visible_membership(
        anomaly.symbol, anomaly.trade_date, memberships, as_of_time
    )
    snapshot = (
        _visible_snapshot(membership, anomaly.trade_date, snapshots, as_of_time)
        if membership is not None
        else None
    )
    sector_return = snapshot.equal_weight_return_pct if snapshot is not None else None
    excess_return = (
        anomaly.return_pct - sector_return
        if anomaly.return_pct is not None and sector_return is not None
        else None
    )
    context = _sector_context(
        anomaly.return_pct,
        sector_return,
        excess_return,
        config,
        available=snapshot is not None,
    )
    used_availability = [anomaly.available_at]
    if snapshot is not None and membership is not None:
        used_availability.append(membership.available_at)
    if snapshot is not None:
        used_availability.append(snapshot.available_at)
    return AnomalyCandidate(
        trade_date=anomaly.trade_date,
        as_of_time=as_of_time,
        symbol=anomaly.symbol,
        sector_code=membership.sector_code if snapshot is not None else None,
        sector_name=membership.sector_name if snapshot is not None else None,
        classification=membership.classification if snapshot is not None else None,
        return_pct=anomaly.return_pct,
        return_zscore=anomaly.return_zscore,
        volume_ratio=anomaly.volume_ratio,
        amount_ratio=anomaly.amount_ratio,
        price_anomaly=anomaly.price_anomaly,
        volume_anomaly=anomaly.volume_anomaly,
        amount_anomaly=anomaly.amount_anomaly,
        signal_count=sum(
            (anomaly.price_anomaly, anomaly.volume_anomaly, anomaly.amount_anomaly)
        ),
        signal_type=signal_type,
        sector_return_pct=sector_return,
        sector_advancer_ratio=(snapshot.advancer_ratio if snapshot is not None else None),
        excess_return_pct=excess_return,
        sector_context=context,
        priority_tier=tier,
        rank=1,
        history_observations=anomaly.history_observations,
        available_at=max(used_availability),
    )


def _signal_type_and_tier(anomaly: StockAnomaly) -> tuple[str, int]:
    signals = (
        anomaly.price_anomaly,
        anomaly.volume_anomaly,
        anomaly.amount_anomaly,
    )
    mapping = {
        (True, True, True): ("price_volume_amount", 1),
        (True, True, False): ("price_volume", 2),
        (True, False, True): ("price_amount", 2),
        (True, False, False): ("price_only", 3),
        (False, True, True): ("volume_amount", 4),
        (False, True, False): ("volume_only", 5),
        (False, False, True): ("amount_only", 5),
    }
    return mapping[signals]


def _latest_visible_anomalies(
    anomalies: Sequence[StockAnomaly], as_of_time: datetime
) -> list[StockAnomaly]:
    latest: dict[tuple[object, str], StockAnomaly] = {}
    for anomaly in anomalies:
        if anomaly.available_at > as_of_time or anomaly.as_of_time > as_of_time:
            continue
        key = (anomaly.trade_date, anomaly.symbol)
        incumbent = latest.get(key)
        if incumbent is None or (
            anomaly.available_at,
            anomaly.as_of_time,
            anomaly.schema_version,
        ) > (
            incumbent.available_at,
            incumbent.as_of_time,
            incumbent.schema_version,
        ):
            latest[key] = anomaly
    return [latest[key] for key in sorted(latest)]


def _visible_membership(
    symbol: str,
    trade_date: object,
    memberships: Sequence[SectorMembership],
    as_of_time: datetime,
) -> SectorMembership | None:
    visible = [
        item
        for item in memberships
        if item.symbol == symbol
        and item.available_at <= as_of_time
        and item.effective_from <= trade_date
        and (item.effective_to is None or item.effective_to > trade_date)
    ]
    return max(
        visible,
        key=lambda item: (
            item.available_at,
            item.effective_from,
            item.source,
            item.sector_code,
        ),
        default=None,
    )


def _visible_snapshot(
    membership: SectorMembership,
    trade_date: object,
    snapshots: Sequence[SectorSnapshot],
    as_of_time: datetime,
) -> SectorSnapshot | None:
    visible = [
        item
        for item in snapshots
        if item.trade_date == trade_date
        and item.sector_code == membership.sector_code
        and item.classification == membership.classification
        and item.available_at <= as_of_time
        and item.as_of_time <= as_of_time
    ]
    return max(
        visible,
        key=lambda item: (
            item.available_at,
            item.as_of_time,
            item.sector_name,
        ),
        default=None,
    )


def _sector_context(
    stock_return: Decimal | None,
    sector_return: Decimal | None,
    excess_return: Decimal | None,
    config: AnomalyTriageConfig,
    *,
    available: bool,
) -> str:
    if not available:
        return "unavailable"
    if stock_return is None or sector_return is None or excess_return is None:
        return "mixed"
    if abs(excess_return) >= config.excess_return_threshold_pct:
        return "stock_specific"
    epsilon = config.direction_epsilon_pct
    same_direction = (
        stock_return > epsilon and sector_return > epsilon
    ) or (stock_return < -epsilon and sector_return < -epsilon)
    return "sector_confirmed" if same_direction else "mixed"


def _ranking_key(candidate: AnomalyCandidate) -> tuple[object, ...]:
    max_ratio = max(
        candidate.volume_ratio or Decimal("0"),
        candidate.amount_ratio or Decimal("0"),
    )
    return (
        candidate.priority_tier,
        -candidate.signal_count,
        -abs(candidate.return_zscore or Decimal("0")),
        -max_ratio,
        -abs(candidate.excess_return_pct or Decimal("0")),
        candidate.symbol,
    )
