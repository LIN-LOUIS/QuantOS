from dataclasses import fields
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantos.observability import (
    ResultStatus,
    ValidationCategory,
    validate_market_bars,
)
from quantos.schemas import MarketBar

SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_bar(
    *,
    source_record_id: str = "600519.SH:1d:2024-01-02",
    available_at: datetime | None = None,
    volume: int = 100,
    amount: Decimal = Decimal("168500"),
) -> MarketBar:
    timestamp = datetime(2024, 1, 2, 15, 0, tzinfo=SHANGHAI)
    return MarketBar(
        symbol="600519.SH",
        timestamp=timestamp,
        frequency="1d",
        open=Decimal("1715"),
        high=Decimal("1718.19"),
        low=Decimal("1678.10"),
        close=Decimal("1685.01"),
        volume=volume,
        amount=amount,
        source="eastmoney",
        source_record_id=source_record_id,
        collected_at=datetime(2026, 8, 26, 15, 0, tzinfo=SHANGHAI),
        available_at=available_at or timestamp,
    )


def result_by_name(results, name: str):
    return next(result for result in results if result.rule_name == name)


def unsafe_copy(bar: MarketBar, **changes: object) -> MarketBar:
    """Simulate corrupted input that bypassed the domain constructor."""

    result = object.__new__(MarketBar)
    for item in fields(MarketBar):
        object.__setattr__(result, item.name, changes.get(item.name, getattr(bar, item.name)))
    return result


def test_valid_market_bars_pass_all_integrity_rules() -> None:
    results = validate_market_bars(
        [make_bar()],
        as_of_time=datetime(2024, 1, 2, 15, 0, tzinfo=SHANGHAI),
    )

    integrity = [
        result
        for result in results
        if result.category is ValidationCategory.SYSTEM_INTEGRITY
    ]
    assert integrity
    assert all(result.status is ResultStatus.SUCCESS for result in integrity)


def test_ohlc_corruption_is_system_integrity_failure() -> None:
    bar = unsafe_copy(make_bar(), high=Decimal("1600"))
    results = validate_market_bars(
        [bar], as_of_time=datetime(2024, 1, 3, 15, 0, tzinfo=SHANGHAI)
    )

    result = result_by_name(results, "market_bar.ohlc")
    assert result.status is ResultStatus.FAILED
    assert result.failed_count == 1


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"volume": -1}, "market_bar.volume_non_negative"),
        ({"amount": Decimal("-1")}, "market_bar.amount_non_negative"),
        (
            {"timestamp": datetime(2024, 1, 2, 15, 0)},
            "market_bar.timestamp",
        ),
    ],
)
def test_corrupted_market_bar_fails_relevant_integrity_rule(change, rule) -> None:
    results = validate_market_bars(
        [unsafe_copy(make_bar(), **change)],
        as_of_time=datetime(2024, 1, 3, 15, 0, tzinfo=SHANGHAI),
    )
    assert result_by_name(results, rule).status is ResultStatus.FAILED


def test_future_available_data_fails_point_in_time_rule() -> None:
    future = make_bar(
        available_at=datetime(2024, 1, 3, 15, 0, tzinfo=SHANGHAI)
    )
    results = validate_market_bars(
        [future], as_of_time=datetime(2024, 1, 2, 15, 0, tzinfo=SHANGHAI)
    )

    pit = result_by_name(results, "market_bar.point_in_time")
    assert pit.status is ResultStatus.FAILED
    assert pit.failed_count == 1


def test_duplicate_source_identity_is_integrity_failure() -> None:
    bar = make_bar()
    results = validate_market_bars(
        [bar, bar], as_of_time=datetime(2024, 1, 3, 15, 0, tzinfo=SHANGHAI)
    )
    assert (
        result_by_name(results, "market_bar.source_record_identity").status
        is ResultStatus.FAILED
    )


def test_empty_market_data_is_warning_not_failure_or_skip() -> None:
    results = validate_market_bars(
        [], as_of_time=datetime(2024, 1, 3, 15, 0, tzinfo=SHANGHAI)
    )
    activity = result_by_name(results, "market_bar.market_activity")

    assert activity.category is ValidationCategory.MARKET_OBSERVATION
    assert activity.status is ResultStatus.WARNING
    assert all(result.status is not ResultStatus.SKIPPED for result in results)


def test_zero_activity_is_market_warning_not_system_failure() -> None:
    results = validate_market_bars(
        [make_bar(volume=0, amount=Decimal("0"))],
        as_of_time=datetime(2024, 1, 3, 15, 0, tzinfo=SHANGHAI),
    )
    activity = result_by_name(results, "market_bar.market_activity")

    assert activity.category is ValidationCategory.MARKET_OBSERVATION
    assert activity.status is ResultStatus.WARNING
    assert all(
        result.status is ResultStatus.SUCCESS
        for result in results
        if result.category is ValidationCategory.SYSTEM_INTEGRITY
    )


def test_validation_requires_aware_as_of_time() -> None:
    with pytest.raises(ValueError, match="as_of_time"):
        validate_market_bars([make_bar()], as_of_time=datetime(2024, 1, 3, 15, 0))
