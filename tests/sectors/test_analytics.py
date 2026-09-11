from __future__ import annotations

from dataclasses import fields, replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantos.schemas import MarketBar, SectorMembership
from quantos.sectors import (
    RETURN_EPSILON_PCT,
    SectorAnalyticsError,
    build_sector_snapshots,
    calculate_stock_return_pct,
    rank_sectors,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2024, 1, 2)
AS_OF = datetime(2024, 1, 2, 20, 0, tzinfo=SHANGHAI)


def membership(symbol: str = "600519.SH", **overrides: object) -> SectorMembership:
    values: dict[str, object] = {
        "symbol": symbol,
        "company_name": symbol,
        "exchange": "SHSE" if symbol.endswith(".SH") else "SZSE",
        "sector_code": "C15",
        "sector_name": "酒、饮料和精制茶制造业",
        "classification": "证监会行业分类",
        "raw_industry": "C15酒、饮料和精制茶制造业",
        "source": "baostock",
        "effective_from": date(2000, 1, 1),
        "available_at": datetime(2024, 1, 2, 17, 0, tzinfo=SHANGHAI),
    }
    values.update(overrides)
    return SectorMembership(**values)


def bar(
    symbol: str = "600519.SH",
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
        source_record_id=f"tushare:{symbol}:1d:2024-01-02",
        collected_at=datetime(2024, 1, 2, 19, 0, tzinfo=SHANGHAI),
        available_at=available_at
        or datetime(2024, 1, 2, 18, 0, tzinfo=SHANGHAI),
    )


def snapshot(bars=None, memberships=None, **kwargs):
    results = build_sector_snapshots(
        [bar()] if bars is None else bars,
        [membership()] if memberships is None else memberships,
        trade_date=TRADE_DATE,
        as_of_time=AS_OF,
        **kwargs,
    )
    return results[0] if results else None


def unsafe_membership(source: SectorMembership, **changes: object) -> SectorMembership:
    result = object.__new__(SectorMembership)
    for item in fields(SectorMembership):
        object.__setattr__(result, item.name, changes.get(item.name, getattr(source, item.name)))
    return result


def test_stock_return_uses_close_over_prev_close() -> None:
    assert calculate_stock_return_pct(bar(close=Decimal("110"))) == Decimal("10.0")


@pytest.mark.parametrize("prev_close", [None, Decimal("0"), Decimal("-1")])
def test_invalid_prev_close_has_no_valid_return(prev_close: Decimal | None) -> None:
    assert calculate_stock_return_pct(bar(prev_close=prev_close)) is None


def test_non_positive_close_has_no_valid_return() -> None:
    assert calculate_stock_return_pct(bar(close=Decimal("0"))) is None


def test_equal_weight_return_and_median_are_decimal_calculations() -> None:
    result = snapshot(
        bars=[
            bar("600519.SH", close=Decimal("110")),
            bar("000001.SZ", close=Decimal("100")),
            bar("300750.SZ", close=Decimal("80")),
        ],
        memberships=[
            membership("600519.SH"),
            membership("000001.SZ"),
            membership("300750.SZ"),
        ],
    )
    assert result.equal_weight_return_pct == Decimal("-3.333333333333333333333333333")
    assert result.median_return_pct == Decimal("0")


def test_even_count_median_is_midpoint() -> None:
    result = snapshot(
        bars=[bar("600519.SH", close=Decimal("110")), bar("000001.SZ", close=Decimal("120"))],
        memberships=[membership("600519.SH"), membership("000001.SZ")],
    )
    assert result.median_return_pct == Decimal("15.0")


def test_breadth_counts_and_advancer_ratio_use_explicit_epsilon() -> None:
    result = snapshot(
        bars=[
            bar("600519.SH", close=Decimal("101")),
            bar("000001.SZ", close=Decimal("99")),
            bar("300750.SZ", close=Decimal("100.000000001")),
        ],
        memberships=[
            membership("600519.SH"),
            membership("000001.SZ"),
            membership("300750.SZ"),
        ],
    )
    assert RETURN_EPSILON_PCT == Decimal("0.00000001")
    assert (result.advancer_count, result.decliner_count, result.flat_count) == (1, 1, 1)
    assert result.advancer_ratio == Decimal("1") / Decimal("3")


def test_volume_and_amount_are_summed_without_provider_rescaling() -> None:
    result = snapshot(
        bars=[
            bar("600519.SH", volume=100, amount=Decimal("1000")),
            bar("000001.SZ", volume=250, amount=Decimal("3500.50")),
        ],
        memberships=[membership("600519.SH"), membership("000001.SZ")],
    )
    assert result.total_volume == 350
    assert result.total_amount == Decimal("4500.50")
    assert result.total_volume != 35000
    assert result.total_amount != Decimal("4500500")


def test_member_valid_bar_and_coverage_counts_missing_bar() -> None:
    result = snapshot(
        bars=[bar("600519.SH")],
        memberships=[membership("600519.SH"), membership("000001.SZ")],
    )
    assert result.member_count == 2
    assert result.valid_bar_count == 1
    assert result.coverage_ratio == Decimal("0.5")


def test_invalid_prev_close_reduces_coverage_and_aggregates() -> None:
    result = snapshot(
        bars=[bar(prev_close=Decimal("0"), volume=999, amount=Decimal("9999"))]
    )
    assert result.valid_bar_count == 0
    assert result.coverage_ratio == 0
    assert result.total_volume == 0
    assert result.total_amount == 0


def test_pit_invisible_bar_is_not_joined() -> None:
    result = snapshot(
        bars=[bar(available_at=datetime(2024, 1, 2, 21, 0, tzinfo=SHANGHAI))]
    )
    assert result.valid_bar_count == 0


def test_pit_invisible_membership_produces_no_sector() -> None:
    result = snapshot(
        memberships=[
            membership(available_at=datetime(2024, 1, 2, 21, 0, tzinfo=SHANGHAI))
        ]
    )
    assert result is None


def test_membership_must_be_effective_on_trade_date() -> None:
    future = membership(effective_from=date(2024, 1, 3))
    expired = membership(effective_to=TRADE_DATE)
    assert snapshot(memberships=[future]) is None
    assert snapshot(memberships=[expired]) is None


def test_top_gainer_and_loser_are_close_to_prev_close_returns() -> None:
    result = snapshot(
        bars=[
            bar("600519.SH", close=Decimal("110")),
            bar("000001.SZ", close=Decimal("80")),
        ],
        memberships=[membership("600519.SH"), membership("000001.SZ")],
    )
    assert result.top_gainer_symbol == "600519.SH"
    assert result.top_gainer_return_pct == Decimal("10.0")
    assert result.top_loser_symbol == "000001.SZ"
    assert result.top_loser_return_pct == Decimal("-20.0")


def test_equal_return_ties_use_symbol_ascending() -> None:
    result = snapshot(
        bars=[bar("600519.SH"), bar("000001.SZ")],
        memberships=[membership("600519.SH"), membership("000001.SZ")],
    )
    assert result.top_gainer_symbol == "000001.SZ"
    assert result.top_loser_symbol == "000001.SZ"


def test_sector_ranking_is_return_descending_with_deterministic_ties() -> None:
    c15 = snapshot()
    i65 = replace(c15, sector_code="I65", sector_name="软件和信息技术服务业")
    c38 = replace(
        c15,
        sector_code="C38",
        sector_name="电气机械和器材制造业",
        equal_weight_return_pct=Decimal("20"),
    )
    ranks = rank_sectors([i65, c15, c38])
    assert [(item.rank, item.sector_code) for item in ranks] == [
        (1, "C38"),
        (2, "C15"),
        (3, "I65"),
    ]


def test_empty_sector_snapshot_is_retained_as_coverage_information() -> None:
    result = snapshot(bars=[])
    assert result.member_count == 1
    assert result.valid_bar_count == 0
    assert result.coverage_ratio == 0
    assert result.equal_weight_return_pct is None
    assert result.median_return_pct is None
    assert result.advancer_ratio is None
    assert result.top_gainer_symbol is None
    assert result.top_loser_symbol is None


def test_malformed_sector_membership_is_safely_excluded() -> None:
    malformed = unsafe_membership(membership(), sector_code="")
    assert snapshot(memberships=[malformed]) is None


def test_bse_membership_is_outside_v1_analytics_universe() -> None:
    bse = membership(
        "920001.BJ",
        exchange="BSE",
        sector_code="C39",
        sector_name="计算机、通信和其他电子设备制造业",
    )
    assert snapshot(bars=[], memberships=[bse]) is None


def test_snapshot_records_universe_and_pit_availability() -> None:
    result = snapshot()
    assert result.universe == "shse_szse_a_share"
    assert result.trade_date == TRADE_DATE
    assert result.available_at <= result.as_of_time


def test_duplicate_visible_daily_bars_are_rejected_instead_of_guessed() -> None:
    with pytest.raises(SectorAnalyticsError, match="multiple PIT-visible"):
        snapshot(bars=[bar(), replace(bar(), source_record_id="another")])


def test_ranking_places_empty_sector_after_valid_sectors() -> None:
    valid = snapshot()
    empty = replace(
        valid,
        sector_code="Z99",
        sector_name="无行情行业",
        valid_bar_count=0,
        coverage_ratio=Decimal("0"),
        equal_weight_return_pct=None,
        median_return_pct=None,
        advancer_count=0,
        decliner_count=0,
        flat_count=0,
        advancer_ratio=None,
        total_volume=0,
        total_amount=Decimal("0"),
        top_gainer_symbol=None,
        top_gainer_return_pct=None,
        top_loser_symbol=None,
        top_loser_return_pct=None,
    )
    assert [item.sector_code for item in rank_sectors([empty, valid])] == ["C15", "Z99"]


def test_sector_ranking_supports_deterministic_ascending_order() -> None:
    positive = snapshot()
    negative = replace(
        positive,
        sector_code="C38",
        sector_name="电气机械和器材制造业",
        equal_weight_return_pct=Decimal("-1"),
    )
    assert [
        item.sector_code
        for item in rank_sectors([positive, negative], descending=False)
    ] == ["C38", "C15"]


def test_proxy_historical_membership_mode_is_explicit_on_snapshot() -> None:
    result = snapshot(historical_membership_mode="current_membership_proxy")
    assert result.historical_membership_mode == "current_membership_proxy"
