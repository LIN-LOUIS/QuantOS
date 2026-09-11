"""Pure Point-in-Time historical anomaly detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from quantos.schemas import (
    AnomalyStatus,
    MarketBar,
    SectorAnomaly,
    SectorSnapshot,
    StockAnomaly,
)
from quantos.schemas._validation import require_aware
from quantos.sectors import calculate_stock_return_pct


@dataclass(frozen=True, slots=True)
class AnomalyConfig:
    lookback_days: int = 20
    min_observations: int = 10
    return_z_threshold: Decimal = Decimal("3.0")
    volume_ratio_threshold: Decimal = Decimal("3.0")
    amount_ratio_threshold: Decimal = Decimal("3.0")
    breadth_z_threshold: Decimal = Decimal("3.0")
    zero_std_epsilon: Decimal = Decimal("0.000000000001")

    def __post_init__(self) -> None:
        if self.lookback_days < 1:
            raise ValueError("lookback_days must be positive")
        if not 1 <= self.min_observations <= self.lookback_days:
            raise ValueError("min_observations must be between 1 and lookback_days")
        for field_name in (
            "return_z_threshold",
            "volume_ratio_threshold",
            "amount_ratio_threshold",
            "breadth_z_threshold",
        ):
            value = getattr(self, field_name)
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{field_name} must be finite and positive")
        if not self.zero_std_epsilon.is_finite() or self.zero_std_epsilon < 0:
            raise ValueError("zero_std_epsilon must be finite and non-negative")


def detect_stock_anomaly(
    current: MarketBar,
    history: Sequence[MarketBar],
    *,
    as_of_time: datetime,
    config: AnomalyConfig = AnomalyConfig(),
) -> StockAnomaly:
    """Detect market anomalies without affecting operational system health."""

    require_aware(as_of_time, "as_of_time")
    _require_current_bar_visible(current, as_of_time)
    historical = _stock_history(current, history, as_of_time, config.lookback_days)
    current_return = calculate_stock_return_pct(current)
    if len(historical) < config.min_observations:
        return StockAnomaly(
            trade_date=current.timestamp.date(),
            as_of_time=as_of_time,
            symbol=current.symbol,
            return_pct=current_return,
            return_zscore=None,
            price_anomaly=False,
            volume=current.volume,
            volume_median=None,
            volume_ratio=None,
            volume_anomaly=False,
            amount=current.amount,
            amount_median=None,
            amount_ratio=None,
            amount_anomaly=False,
            history_observations=len(historical),
            status=AnomalyStatus.INSUFFICIENT_HISTORY,
            available_at=max(
                [current.available_at] + [item[0].available_at for item in historical]
            ),
        )

    historical_returns = [item[1] for item in historical]
    return_zscore = _zscore(
        current_return, historical_returns, config.zero_std_epsilon
    )
    volume_median = _median([Decimal(item[0].volume) for item in historical])
    amount_median = _median([item[0].amount for item in historical])
    volume_ratio = _positive_ratio(Decimal(current.volume), volume_median)
    amount_ratio = _positive_ratio(current.amount, amount_median)
    computable = all(
        value is not None for value in (return_zscore, volume_ratio, amount_ratio)
    )
    return StockAnomaly(
        trade_date=current.timestamp.date(),
        as_of_time=as_of_time,
        symbol=current.symbol,
        return_pct=current_return,
        return_zscore=return_zscore,
        price_anomaly=(
            return_zscore is not None
            and abs(return_zscore) >= config.return_z_threshold
        ),
        volume=current.volume,
        volume_median=volume_median,
        volume_ratio=volume_ratio,
        volume_anomaly=(
            volume_ratio is not None
            and volume_ratio >= config.volume_ratio_threshold
        ),
        amount=current.amount,
        amount_median=amount_median,
        amount_ratio=amount_ratio,
        amount_anomaly=(
            amount_ratio is not None
            and amount_ratio >= config.amount_ratio_threshold
        ),
        history_observations=len(historical),
        status=AnomalyStatus.OK if computable else AnomalyStatus.NOT_COMPUTABLE,
        available_at=max(
            [current.available_at] + [item[0].available_at for item in historical]
        ),
    )


def detect_sector_anomaly(
    current: SectorSnapshot,
    history: Sequence[SectorSnapshot],
    *,
    as_of_time: datetime,
    config: AnomalyConfig = AnomalyConfig(),
) -> SectorAnomaly:
    """Detect sector return, breadth, and amount anomalies from prior snapshots."""

    require_aware(as_of_time, "as_of_time")
    _require_current_sector_visible(current, as_of_time)
    historical = _sector_history(current, history, as_of_time, config.lookback_days)
    if len(historical) < config.min_observations:
        return SectorAnomaly(
            trade_date=current.trade_date,
            as_of_time=as_of_time,
            sector_code=current.sector_code,
            sector_name=current.sector_name,
            classification=current.classification,
            return_pct=current.equal_weight_return_pct,
            return_zscore=None,
            advancer_ratio=current.advancer_ratio,
            breadth_zscore=None,
            total_amount=current.total_amount,
            amount_median=None,
            amount_ratio=None,
            price_anomaly=False,
            breadth_anomaly=False,
            amount_anomaly=False,
            history_observations=len(historical),
            status=AnomalyStatus.INSUFFICIENT_HISTORY,
            available_at=max(
                [current.available_at] + [item.available_at for item in historical]
            ),
        )

    historical_returns = [item.equal_weight_return_pct for item in historical]
    historical_breadth = [item.advancer_ratio for item in historical]
    return_zscore = _zscore_optional(
        current.equal_weight_return_pct,
        historical_returns,
        config.zero_std_epsilon,
    )
    breadth_zscore = _zscore_optional(
        current.advancer_ratio,
        historical_breadth,
        config.zero_std_epsilon,
    )
    amount_median = _median([item.total_amount for item in historical])
    amount_ratio = _positive_ratio(current.total_amount, amount_median)
    computable = all(
        value is not None for value in (return_zscore, breadth_zscore, amount_ratio)
    )
    return SectorAnomaly(
        trade_date=current.trade_date,
        as_of_time=as_of_time,
        sector_code=current.sector_code,
        sector_name=current.sector_name,
        classification=current.classification,
        return_pct=current.equal_weight_return_pct,
        return_zscore=return_zscore,
        advancer_ratio=current.advancer_ratio,
        breadth_zscore=breadth_zscore,
        total_amount=current.total_amount,
        amount_median=amount_median,
        amount_ratio=amount_ratio,
        price_anomaly=(
            return_zscore is not None
            and abs(return_zscore) >= config.return_z_threshold
        ),
        breadth_anomaly=(
            breadth_zscore is not None
            and abs(breadth_zscore) >= config.breadth_z_threshold
        ),
        amount_anomaly=(
            amount_ratio is not None
            and amount_ratio >= config.amount_ratio_threshold
        ),
        history_observations=len(historical),
        status=AnomalyStatus.OK if computable else AnomalyStatus.NOT_COMPUTABLE,
        available_at=max(
            [current.available_at] + [item.available_at for item in historical]
        ),
    )


def _stock_history(
    current: MarketBar,
    history: Sequence[MarketBar],
    as_of_time: datetime,
    limit: int,
) -> list[tuple[MarketBar, Decimal]]:
    by_date: dict[object, tuple[MarketBar, Decimal]] = {}
    for bar in history:
        value = calculate_stock_return_pct(bar)
        if (
            bar.symbol != current.symbol
            or bar.frequency != "1d"
            or bar.timestamp.date() >= current.timestamp.date()
            or bar.available_at > as_of_time
            or value is None
        ):
            continue
        trade_date = bar.timestamp.date()
        incumbent = by_date.get(trade_date)
        if incumbent is None or (
            bar.available_at,
            bar.collected_at,
            bar.source,
            bar.source_record_id,
        ) > (
            incumbent[0].available_at,
            incumbent[0].collected_at,
            incumbent[0].source,
            incumbent[0].source_record_id,
        ):
            by_date[trade_date] = (bar, value)
    return [by_date[key] for key in sorted(by_date, reverse=True)[:limit]]


def _sector_history(
    current: SectorSnapshot,
    history: Sequence[SectorSnapshot],
    as_of_time: datetime,
    limit: int,
) -> list[SectorSnapshot]:
    by_date: dict[object, SectorSnapshot] = {}
    for snapshot in history:
        if (
            snapshot.sector_code != current.sector_code
            or snapshot.classification != current.classification
            or snapshot.universe != current.universe
            or snapshot.trade_date >= current.trade_date
            or snapshot.available_at > as_of_time
            or snapshot.as_of_time > as_of_time
        ):
            continue
        incumbent = by_date.get(snapshot.trade_date)
        if incumbent is None or (
            snapshot.available_at,
            snapshot.as_of_time,
            snapshot.sector_name,
        ) > (
            incumbent.available_at,
            incumbent.as_of_time,
            incumbent.sector_name,
        ):
            by_date[snapshot.trade_date] = snapshot
    return [by_date[key] for key in sorted(by_date, reverse=True)[:limit]]


def _require_current_bar_visible(current: MarketBar, as_of_time: datetime) -> None:
    if current.frequency != "1d":
        raise ValueError("current MarketBar must use 1d frequency")
    if current.available_at > as_of_time:
        raise ValueError("current MarketBar is not PIT-visible")


def _require_current_sector_visible(
    current: SectorSnapshot, as_of_time: datetime
) -> None:
    if current.available_at > as_of_time or current.as_of_time > as_of_time:
        raise ValueError("current SectorSnapshot is not PIT-visible")


def _zscore(
    current: Decimal | None,
    history: Sequence[Decimal],
    zero_std_epsilon: Decimal,
) -> Decimal | None:
    if current is None or not history:
        return None
    mean = sum(history, start=Decimal("0")) / Decimal(len(history))
    variance = sum(
        ((value - mean) ** 2 for value in history), start=Decimal("0")
    ) / Decimal(len(history))
    standard_deviation = variance.sqrt()
    if standard_deviation <= zero_std_epsilon:
        return None
    return (current - mean) / standard_deviation


def _zscore_optional(
    current: Decimal | None,
    history: Sequence[Decimal | None],
    zero_std_epsilon: Decimal,
) -> Decimal | None:
    if current is None or any(value is None for value in history):
        return None
    return _zscore(current, [value for value in history if value is not None], zero_std_epsilon)


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")


def _positive_ratio(current: Decimal, baseline: Decimal | None) -> Decimal | None:
    if baseline is None or baseline <= 0:
        return None
    return current / baseline
