"""Small deterministic checks for canonical bars from different providers."""

from __future__ import annotations

from decimal import Decimal

from quantos.schemas import MarketBar


def compare_market_bars(
    reference: MarketBar,
    candidate: MarketBar,
    *,
    price_tolerance: Decimal = Decimal("0.01"),
    volume_relative_tolerance: Decimal = Decimal("0.02"),
    amount_relative_tolerance: Decimal = Decimal("0.02"),
) -> tuple[str, ...]:
    """Report semantic mismatches without requiring bit-identical decimals."""

    issues: list[str] = []
    if reference.symbol != candidate.symbol:
        issues.append("symbol_mismatch")
    if reference.timestamp.date() != candidate.timestamp.date():
        issues.append("trade_date_mismatch")
    for field_name in ("open", "high", "low", "close"):
        if abs(getattr(reference, field_name) - getattr(candidate, field_name)) > price_tolerance:
            issues.append(f"{field_name}_mismatch")
    if _relative_difference(
        Decimal(reference.volume), Decimal(candidate.volume)
    ) > volume_relative_tolerance:
        issues.append("volume_mismatch")
    if _relative_difference(reference.amount, candidate.amount) > amount_relative_tolerance:
        issues.append("amount_mismatch")
    return tuple(issues)


def _relative_difference(left: Decimal, right: Decimal) -> Decimal:
    denominator = max(abs(left), abs(right), Decimal("1"))
    return abs(left - right) / denominator
