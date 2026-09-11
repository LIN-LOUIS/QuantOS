from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantos.schemas import MarketBar, SectorSnapshot
from quantos.snapshots import build_market_snapshot

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2024, 1, 2)
AS_OF = datetime(2024, 1, 2, 20, 0, tzinfo=SHANGHAI)


def bar(
    symbol: str,
    *,
    close: Decimal = Decimal("110"),
    prev_close: Decimal | None = Decimal("100"),
    volume: int = 100,
    amount: Decimal = Decimal("1000"),
    available_at: datetime | None = None,
) -> MarketBar:
    base = prev_close if prev_close is not None and prev_close > 0 else Decimal("100")
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2024, 1, 2, 15, 0, tzinfo=SHANGHAI),
        frequency="1d",
        open=base,
        high=max(base, close),
        low=min(base, close),
        close=close,
        prev_close=prev_close,
        volume=volume,
        amount=amount,
        source="tushare",
        source_record_id=f"tushare:{symbol}:2024-01-02",
        collected_at=datetime(2024, 1, 2, 19, 0, tzinfo=SHANGHAI),
        available_at=available_at
        or datetime(2024, 1, 2, 18, 0, tzinfo=SHANGHAI),
    )


def sector(
    code: str,
    return_pct: Decimal | None,
    *,
    member_count: int = 2,
    valid_bar_count: int = 2,
    available_at: datetime | None = None,
) -> SectorSnapshot:
    advancers = valid_bar_count if return_pct is not None and return_pct > 0 else 0
    decliners = valid_bar_count if return_pct is not None and return_pct < 0 else 0
    flats = valid_bar_count - advancers - decliners
    return SectorSnapshot(
        trade_date=TRADE_DATE,
        as_of_time=AS_OF,
        sector_code=code,
        sector_name=f"行业{code}",
        classification="证监会行业分类",
        member_count=member_count,
        valid_bar_count=valid_bar_count,
        coverage_ratio=Decimal(valid_bar_count) / Decimal(member_count),
        equal_weight_return_pct=return_pct,
        median_return_pct=return_pct,
        advancer_count=advancers,
        decliner_count=decliners,
        flat_count=flats,
        advancer_ratio=(
            Decimal(advancers) / Decimal(valid_bar_count)
            if valid_bar_count
            else None
        ),
        total_volume=valid_bar_count * 100,
        total_amount=Decimal(valid_bar_count * 1000),
        top_gainer_symbol="600519.SH" if valid_bar_count else None,
        top_gainer_return_pct=return_pct if valid_bar_count else None,
        top_loser_symbol="600519.SH" if valid_bar_count else None,
        top_loser_return_pct=return_pct if valid_bar_count else None,
        available_at=available_at
        or datetime(2024, 1, 2, 18, 30, tzinfo=SHANGHAI),
    )


def build(*, bars=None, sectors=None, members=None, **kwargs):
    return build_market_snapshot(
        [
            bar("600519.SH", close=Decimal("110")),
            bar("000001.SZ", close=Decimal("90")),
            bar("300750.SZ", close=Decimal("100")),
        ]
        if bars is None
        else bars,
        [sector("C15", Decimal("1"))] if sectors is None else sectors,
        security_member_symbols=(
            ["600519.SH", "000001.SZ", "300750.SZ"]
            if members is None
            else members
        ),
        trade_date=TRADE_DATE,
        as_of_time=AS_OF,
        **kwargs,
    )


def test_market_breadth_counts_advancers_decliners_and_flats() -> None:
    result = build()
    assert (result.advancer_count, result.decliner_count, result.flat_count) == (1, 1, 1)


def test_market_advancer_ratio_uses_valid_bars() -> None:
    assert build().advancer_ratio == Decimal("1") / Decimal("3")


def test_market_volume_and_amount_use_canonical_units_without_rescaling() -> None:
    result = build(
        bars=[
            bar("600519.SH", volume=100, amount=Decimal("1000")),
            bar("000001.SZ", volume=250, amount=Decimal("3500.50")),
        ],
        members=["600519.SH", "000001.SZ"],
    )
    assert result.total_volume == 350
    assert result.total_amount == Decimal("4500.50")
    assert result.total_volume != 35000
    assert result.total_amount != Decimal("4500500")


def test_market_member_valid_and_coverage_counts() -> None:
    result = build(bars=[bar("600519.SH")])
    assert result.security_member_count == 3
    assert result.valid_bar_count == 1
    assert result.coverage_ratio == Decimal("1") / Decimal("3")


def test_missing_bars_are_sorted_deterministically() -> None:
    result = build(bars=[bar("600519.SH")])
    assert result.missing_bar_count == 2
    assert result.missing_bar_symbols == ("000001.SZ", "300750.SZ")


def test_invalid_prev_close_is_missing_from_valid_coverage() -> None:
    result = build(
        bars=[bar("600519.SH", prev_close=Decimal("0"))],
        members=["600519.SH"],
    )
    assert result.valid_bar_count == 0
    assert result.missing_bar_symbols == ("600519.SH",)
    assert result.total_volume == 0
    assert result.total_amount == 0


def test_sector_breadth_counts_positive_negative_and_flat() -> None:
    result = build(
        sectors=[
            sector("C15", Decimal("1")),
            sector("C38", Decimal("-1")),
            sector("I65", Decimal("0")),
        ]
    )
    assert result.sector_count == 3
    assert (
        result.sector_advancer_count,
        result.sector_decliner_count,
        result.sector_flat_count,
    ) == (1, 1, 1)


def test_sector_return_within_epsilon_is_flat() -> None:
    result = build(sectors=[sector("C15", Decimal("0.000000001"))])
    assert result.sector_flat_count == 1


def test_top_and_bottom_sector_codes_use_deterministic_ranking() -> None:
    result = build(
        sectors=[
            sector("C15", Decimal("1")),
            sector("C38", Decimal("3")),
            sector("I65", Decimal("-2")),
        ],
        top_n=2,
    )
    assert result.top_sector_codes == ("C38", "C15")
    assert result.bottom_sector_codes == ("I65", "C15")


def test_sector_ranking_ties_use_sector_code() -> None:
    result = build(
        sectors=[sector("I65", Decimal("1")), sector("C15", Decimal("1"))]
    )
    assert result.top_sector_codes == ("C15", "I65")
    assert result.bottom_sector_codes == ("C15", "I65")


def test_top_n_does_not_store_full_sector_snapshots() -> None:
    result = build(sectors=[sector("C15", Decimal("1"))])
    assert result.top_sector_codes == ("C15",)
    assert not hasattr(result, "sector_snapshots")


def test_incomplete_sector_coverage_is_counted() -> None:
    result = build(
        sectors=[
            sector("C15", Decimal("1"), member_count=3, valid_bar_count=2),
            sector("C38", Decimal("1")),
        ]
    )
    assert result.incomplete_coverage_sector_count == 1


def test_pit_invisible_bar_is_excluded_and_marked_missing() -> None:
    result = build(
        bars=[
            bar(
                "600519.SH",
                available_at=datetime(2024, 1, 2, 21, 0, tzinfo=SHANGHAI),
            )
        ],
        members=["600519.SH"],
    )
    assert result.valid_bar_count == 0
    assert result.missing_bar_symbols == ("600519.SH",)


def test_pit_invisible_sector_snapshot_is_excluded() -> None:
    future = replace(
        sector("C15", Decimal("1")),
        as_of_time=datetime(2024, 1, 2, 22, 0, tzinfo=SHANGHAI),
        available_at=datetime(2024, 1, 2, 21, 0, tzinfo=SHANGHAI),
    )
    assert build(sectors=[future]).sector_count == 0


def test_available_at_is_maximum_visible_input_availability() -> None:
    result = build(
        bars=[
            bar(
                "600519.SH",
                available_at=datetime(2024, 1, 2, 18, 45, tzinfo=SHANGHAI),
            )
        ],
        members=["600519.SH"],
        sectors=[
            sector(
                "C15",
                Decimal("1"),
                available_at=datetime(2024, 1, 2, 19, 30, tzinfo=SHANGHAI),
            )
        ],
    )
    assert result.available_at == datetime(2024, 1, 2, 19, 30, tzinfo=SHANGHAI)


def test_empty_bars_retains_membership_coverage_information() -> None:
    result = build(bars=[])
    assert result.valid_bar_count == 0
    assert result.coverage_ratio == 0
    assert result.advancer_ratio is None
    assert result.missing_bar_count == 3


def test_empty_sector_snapshots_are_supported() -> None:
    result = build(sectors=[])
    assert result.sector_count == 0
    assert result.top_sector_codes == ()
    assert result.bottom_sector_codes == ()


def test_completely_empty_inputs_are_deterministic() -> None:
    result = build(bars=[], sectors=[], members=[])
    assert result.security_member_count == 0
    assert result.valid_bar_count == 0
    assert result.coverage_ratio == 0
    assert result.available_at == AS_OF


def test_non_member_bar_is_not_aggregated() -> None:
    result = build(
        bars=[bar("600519.SH"), bar("000001.SZ", amount=Decimal("999999"))],
        members=["600519.SH"],
    )
    assert result.valid_bar_count == 1
    assert result.total_amount == Decimal("1000")


def test_member_symbols_reject_bse_in_v1() -> None:
    with pytest.raises(ValueError, match="SH/SZ"):
        build(members=["920001.BJ"])


def test_member_symbols_must_be_unique() -> None:
    with pytest.raises(ValueError, match="unique"):
        build(members=["600519.SH", "600519.SH"])


def test_top_n_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        build(top_n=0)


def test_snapshot_universe_is_explicitly_shse_szse() -> None:
    assert build().universe == "shse_szse_a_share"
