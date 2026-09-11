from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from random import Random
from zoneinfo import ZoneInfo

import pytest

from quantos.anomalies import AnomalyConfig, detect_sector_anomaly, detect_stock_anomaly
from quantos.observability import RunHealth
from quantos.schemas import AnomalyStatus, MarketBar, SectorSnapshot

SHANGHAI = ZoneInfo("Asia/Shanghai")
TARGET_DATE = date(2024, 1, 31)
AS_OF = datetime(2024, 1, 31, 20, 0, tzinfo=SHANGHAI)


def market_bar(
    trade_date: date,
    *,
    symbol: str = "600519.SH",
    return_pct: Decimal = Decimal("0"),
    volume: int = 100,
    amount: Decimal = Decimal("1000"),
) -> MarketBar:
    previous = Decimal("100")
    close = previous * (Decimal("1") + return_pct / Decimal("100"))
    timestamp = datetime.combine(trade_date, time(15), tzinfo=SHANGHAI)
    return MarketBar(
        symbol=symbol,
        timestamp=timestamp,
        frequency="1d",
        open=previous,
        high=max(previous, close),
        low=min(previous, close),
        close=close,
        prev_close=previous,
        volume=volume,
        amount=amount,
        source="test",
        source_record_id=f"{symbol}:{trade_date}",
        collected_at=datetime.combine(trade_date, time(19), tzinfo=SHANGHAI),
        available_at=datetime.combine(trade_date, time(18), tzinfo=SHANGHAI),
    )


def stock_history(
    count: int = 20,
    *,
    returns: tuple[Decimal, ...] = (Decimal("-1"), Decimal("0"), Decimal("1")),
    volume: int = 100,
    amount: Decimal = Decimal("1000"),
) -> list[MarketBar]:
    return [
        market_bar(
            TARGET_DATE - timedelta(days=index + 1),
            return_pct=returns[index % len(returns)],
            volume=volume,
            amount=amount,
        )
        for index in range(count)
    ]


def sector_snapshot(
    trade_date: date,
    *,
    return_pct: Decimal | None = Decimal("0"),
    advancer_ratio: Decimal | None = Decimal("0.5"),
    total_amount: Decimal = Decimal("1000"),
    sector_code: str = "C15",
) -> SectorSnapshot:
    valid_count = 10
    advancers = int((advancer_ratio or Decimal("0")) * valid_count)
    decliners = valid_count - advancers
    available_at = datetime.combine(trade_date, time(18, 30), tzinfo=SHANGHAI)
    return SectorSnapshot(
        trade_date=trade_date,
        as_of_time=available_at,
        sector_code=sector_code,
        sector_name="酒、饮料和精制茶制造业",
        classification="证监会行业分类",
        member_count=valid_count,
        valid_bar_count=valid_count,
        coverage_ratio=Decimal("1"),
        equal_weight_return_pct=return_pct,
        median_return_pct=return_pct,
        advancer_count=advancers,
        decliner_count=decliners,
        flat_count=0,
        advancer_ratio=advancer_ratio,
        total_volume=1000,
        total_amount=total_amount,
        top_gainer_symbol="600519.SH",
        top_gainer_return_pct=return_pct,
        top_loser_symbol="000001.SZ",
        top_loser_return_pct=return_pct,
        available_at=available_at,
    )


def sector_history(count: int = 20) -> list[SectorSnapshot]:
    returns = (Decimal("-1"), Decimal("0"), Decimal("1"))
    breadth = (Decimal("0.4"), Decimal("0.5"), Decimal("0.6"))
    return [
        sector_snapshot(
            TARGET_DATE - timedelta(days=index + 1),
            return_pct=returns[index % 3],
            advancer_ratio=breadth[index % 3],
        )
        for index in range(count)
    ]


def test_normal_stock_is_not_anomalous() -> None:
    result = detect_stock_anomaly(
        market_bar(TARGET_DATE), stock_history(), as_of_time=AS_OF
    )
    assert result.status is AnomalyStatus.OK
    assert not result.price_anomaly
    assert not result.volume_anomaly
    assert not result.amount_anomaly


def test_positive_stock_return_zscore_triggers_price_anomaly() -> None:
    result = detect_stock_anomaly(
        market_bar(TARGET_DATE, return_pct=Decimal("10")),
        stock_history(),
        as_of_time=AS_OF,
    )
    assert result.return_zscore > 3
    assert result.price_anomaly


def test_negative_stock_return_zscore_triggers_price_anomaly() -> None:
    result = detect_stock_anomaly(
        market_bar(TARGET_DATE, return_pct=Decimal("-10")),
        stock_history(),
        as_of_time=AS_OF,
    )
    assert result.return_zscore < -3
    assert result.price_anomaly


def test_zero_return_standard_deviation_is_not_computable() -> None:
    result = detect_stock_anomaly(
        market_bar(TARGET_DATE, return_pct=Decimal("1")),
        stock_history(returns=(Decimal("0"),)),
        as_of_time=AS_OF,
    )
    assert result.return_zscore is None
    assert not result.price_anomaly
    assert result.status is AnomalyStatus.NOT_COMPUTABLE


def test_stock_insufficient_history_is_explicit() -> None:
    result = detect_stock_anomaly(
        market_bar(TARGET_DATE), stock_history(9), as_of_time=AS_OF
    )
    assert result.history_observations == 9
    assert result.status is AnomalyStatus.INSUFFICIENT_HISTORY


@pytest.mark.parametrize(
    ("volume", "expected"),
    [(300, True), (299, False)],
    ids=["spike", "normal"],
)
def test_volume_anomaly_uses_median_ratio(volume: int, expected: bool) -> None:
    result = detect_stock_anomaly(
        market_bar(TARGET_DATE, volume=volume), stock_history(), as_of_time=AS_OF
    )
    assert result.volume_ratio == Decimal(volume) / Decimal("100")
    assert result.volume_anomaly is expected


def test_zero_median_volume_is_not_computable() -> None:
    result = detect_stock_anomaly(
        market_bar(TARGET_DATE, volume=300),
        stock_history(volume=0),
        as_of_time=AS_OF,
    )
    assert result.volume_ratio is None
    assert not result.volume_anomaly
    assert result.status is AnomalyStatus.NOT_COMPUTABLE


@pytest.mark.parametrize(
    ("amount", "expected"),
    [(Decimal("3000"), True), (Decimal("2999"), False)],
    ids=["spike", "normal"],
)
def test_amount_anomaly_uses_median_ratio(amount: Decimal, expected: bool) -> None:
    result = detect_stock_anomaly(
        market_bar(TARGET_DATE, amount=amount), stock_history(), as_of_time=AS_OF
    )
    assert result.amount_ratio == amount / Decimal("1000")
    assert result.amount_anomaly is expected


def test_stock_future_trade_date_never_enters_baseline() -> None:
    history = stock_history(10)
    history.append(
        market_bar(
            TARGET_DATE + timedelta(days=1),
            return_pct=Decimal("100"),
            volume=999999,
            amount=Decimal("999999999"),
        )
    )
    result = detect_stock_anomaly(
        market_bar(TARGET_DATE),
        history,
        as_of_time=datetime(2024, 2, 2, 20, 0, tzinfo=SHANGHAI),
    )
    assert result.history_observations == 10
    assert result.volume_median == Decimal("100")
    assert result.amount_median == Decimal("1000")


def test_stock_history_available_after_cutoff_cannot_leak() -> None:
    history = stock_history(10)
    invisible = replace(
        market_bar(TARGET_DATE - timedelta(days=15), return_pct=Decimal("50")),
        collected_at=datetime(2024, 2, 1, 19, 0, tzinfo=SHANGHAI),
        available_at=datetime(2024, 2, 1, 18, 0, tzinfo=SHANGHAI),
        source_record_id="future-revision",
    )
    result = detect_stock_anomaly(
        market_bar(TARGET_DATE), history + [invisible], as_of_time=AS_OF
    )
    assert result.history_observations == 10
    assert invisible.available_at > AS_OF


def test_stock_lookback_keeps_only_latest_observations() -> None:
    result = detect_stock_anomaly(
        market_bar(TARGET_DATE), stock_history(25), as_of_time=AS_OF
    )
    assert result.history_observations == 20


def test_stock_units_are_not_rescaled_in_anomaly_result() -> None:
    result = detect_stock_anomaly(
        market_bar(TARGET_DATE, volume=300, amount=Decimal("3000")),
        stock_history(),
        as_of_time=AS_OF,
    )
    assert result.volume == 300
    assert result.amount == Decimal("3000")
    assert result.volume_ratio == Decimal("3")
    assert result.amount_ratio == Decimal("3")


def test_sector_price_anomaly() -> None:
    result = detect_sector_anomaly(
        sector_snapshot(TARGET_DATE, return_pct=Decimal("10")),
        sector_history(),
        as_of_time=AS_OF,
    )
    assert result.return_zscore > 3
    assert result.price_anomaly


def test_sector_breadth_anomaly() -> None:
    result = detect_sector_anomaly(
        sector_snapshot(TARGET_DATE, advancer_ratio=Decimal("1")),
        sector_history(),
        as_of_time=AS_OF,
    )
    assert result.breadth_zscore > 3
    assert result.breadth_anomaly


def test_sector_amount_anomaly() -> None:
    result = detect_sector_anomaly(
        sector_snapshot(TARGET_DATE, total_amount=Decimal("3000")),
        sector_history(),
        as_of_time=AS_OF,
    )
    assert result.amount_ratio == Decimal("3")
    assert result.amount_anomaly


def test_normal_sector_is_not_anomalous() -> None:
    result = detect_sector_anomaly(
        sector_snapshot(TARGET_DATE), sector_history(), as_of_time=AS_OF
    )
    assert result.status is AnomalyStatus.OK
    assert not any(
        (result.price_anomaly, result.breadth_anomaly, result.amount_anomaly)
    )


def test_sector_insufficient_history_is_explicit() -> None:
    result = detect_sector_anomaly(
        sector_snapshot(TARGET_DATE), sector_history(9), as_of_time=AS_OF
    )
    assert result.status is AnomalyStatus.INSUFFICIENT_HISTORY
    assert result.history_observations == 9


def test_sector_zero_standard_deviation_is_not_computable() -> None:
    history = [
        sector_snapshot(TARGET_DATE - timedelta(days=index + 1))
        for index in range(20)
    ]
    result = detect_sector_anomaly(
        sector_snapshot(TARGET_DATE), history, as_of_time=AS_OF
    )
    assert result.return_zscore is None
    assert result.breadth_zscore is None
    assert result.status is AnomalyStatus.NOT_COMPUTABLE


def test_sector_future_trade_date_never_enters_baseline() -> None:
    history = sector_history(10) + [
        sector_snapshot(
            TARGET_DATE + timedelta(days=1),
            return_pct=Decimal("100"),
            total_amount=Decimal("999999999"),
        )
    ]
    result = detect_sector_anomaly(
        sector_snapshot(TARGET_DATE),
        history,
        as_of_time=datetime(2024, 2, 2, 20, 0, tzinfo=SHANGHAI),
    )
    assert result.history_observations == 10
    assert result.amount_median == Decimal("1000")


def test_sector_history_available_after_cutoff_cannot_leak() -> None:
    invisible = replace(
        sector_snapshot(TARGET_DATE - timedelta(days=15), return_pct=Decimal("50")),
        as_of_time=datetime(2024, 2, 1, 20, 0, tzinfo=SHANGHAI),
        available_at=datetime(2024, 2, 1, 18, 30, tzinfo=SHANGHAI),
    )
    result = detect_sector_anomaly(
        sector_snapshot(TARGET_DATE),
        sector_history(10) + [invisible],
        as_of_time=AS_OF,
    )
    assert result.history_observations == 10
    assert invisible.available_at > AS_OF


def test_anomaly_output_is_deterministic_for_history_order() -> None:
    history = stock_history()
    shuffled = list(history)
    Random(7).shuffle(shuffled)
    current = market_bar(TARGET_DATE, return_pct=Decimal("5"), volume=300)
    assert detect_stock_anomaly(current, history, as_of_time=AS_OF) == (
        detect_stock_anomaly(current, shuffled, as_of_time=AS_OF)
    )


def test_market_anomaly_does_not_change_system_health() -> None:
    health = RunHealth.HEALTHY
    anomaly = detect_stock_anomaly(
        market_bar(TARGET_DATE, return_pct=Decimal("10")),
        stock_history(),
        as_of_time=AS_OF,
    )
    assert anomaly.price_anomaly
    assert health is RunHealth.HEALTHY
    assert not hasattr(anomaly, "run_health")


def test_current_pit_invisible_inputs_are_schema_errors_not_anomalies() -> None:
    current = market_bar(TARGET_DATE)
    with pytest.raises(ValueError, match="PIT-visible"):
        detect_stock_anomaly(
            current,
            stock_history(),
            as_of_time=datetime(2024, 1, 31, 17, 0, tzinfo=SHANGHAI),
        )


def test_default_config_matches_v1_thresholds() -> None:
    config = AnomalyConfig()
    assert config.lookback_days == 20
    assert config.min_observations == 10
    assert config.return_z_threshold == Decimal("3.0")
    assert config.volume_ratio_threshold == Decimal("3.0")
    assert config.amount_ratio_threshold == Decimal("3.0")
    assert config.breadth_z_threshold == Decimal("3.0")
