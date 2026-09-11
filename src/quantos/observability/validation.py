"""Pure, deterministic validation of normalized market data."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable

from quantos.schemas import MarketBar
from quantos.schemas._validation import require_aware

from .models import ResultStatus, ValidationCategory, ValidationResult


def validate_market_bars(
    bars: Iterable[MarketBar], *, as_of_time: datetime
) -> tuple[ValidationResult, ...]:
    """Validate system invariants without treating market behavior as failure."""

    require_aware(as_of_time, "as_of_time")
    materialized = tuple(bars)
    count = len(materialized)

    ohlc_failures = sum(
        not _finite_prices(bar)
        or bar.high < max(bar.open, bar.high, bar.low, bar.close)
        or bar.low > min(bar.open, bar.high, bar.low, bar.close)
        for bar in materialized
    )
    volume_failures = sum(bar.volume < 0 for bar in materialized)
    amount_failures = sum(
        not bar.amount.is_finite() or bar.amount < 0 for bar in materialized
    )
    timestamp_failures = sum(
        not _aware(bar.timestamp)
        or not _aware(bar.collected_at)
        or bar.collected_at < bar.timestamp
        for bar in materialized
    )
    availability_failures = sum(
        not _aware(bar.available_at)
        or not _aware(bar.timestamp)
        or bar.available_at < bar.timestamp
        for bar in materialized
    )
    pit_failures = sum(
        not _aware(bar.available_at) or bar.available_at > as_of_time
        for bar in materialized
    )
    identities = [bar.source_record_id for bar in materialized]
    duplicate_count = len(identities) - len(set(identities))

    results = [
        _integrity("market_bar.ohlc", count, ohlc_failures),
        _integrity("market_bar.volume_non_negative", count, volume_failures),
        _integrity("market_bar.amount_non_negative", count, amount_failures),
        _integrity("market_bar.timestamp", count, timestamp_failures),
        _integrity("market_bar.available_at", count, availability_failures),
        _integrity("market_bar.point_in_time", count, pit_failures),
        _integrity("market_bar.source_record_identity", count, duplicate_count),
    ]

    inactive_count = sum(bar.volume == 0 or bar.amount == 0 for bar in materialized)
    if count == 0:
        results.append(
            ValidationResult(
                rule_name="market_bar.market_activity",
                category=ValidationCategory.MARKET_OBSERVATION,
                status=ResultStatus.WARNING,
                checked_count=0,
                message="no market bars were observed; this may be a valid market closure",
                metrics={"empty": True},
            )
        )
    else:
        results.append(
            ValidationResult(
                rule_name="market_bar.market_activity",
                category=ValidationCategory.MARKET_OBSERVATION,
                status=(
                    ResultStatus.WARNING if inactive_count else ResultStatus.SUCCESS
                ),
                checked_count=count,
                failed_count=inactive_count,
                message=(
                    "zero volume or amount observed; this may be valid market behavior"
                    if inactive_count
                    else None
                ),
                metrics={"inactive_count": inactive_count},
            )
        )
    return tuple(results)


def _integrity(rule_name: str, checked: int, failed: int) -> ValidationResult:
    return ValidationResult(
        rule_name=rule_name,
        category=ValidationCategory.SYSTEM_INTEGRITY,
        status=ResultStatus.FAILED if failed else ResultStatus.SUCCESS,
        checked_count=checked,
        failed_count=failed,
        message=f"{failed} of {checked} records violated the invariant" if failed else None,
    )


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _finite_prices(bar: MarketBar) -> bool:
    values: tuple[Decimal, ...] = (bar.open, bar.high, bar.low, bar.close)
    return all(value.is_finite() for value in values)
