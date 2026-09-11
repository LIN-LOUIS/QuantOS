from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantos.schemas import SectorMembership, SectorSnapshot, StockAnomaly
from quantos.triage import AnomalyTriageConfig, rank_anomaly_candidates

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 8, 31)
AS_OF = datetime(2026, 8, 31, 20, 0, tzinfo=SHANGHAI)


def anomaly(symbol: str = "600519.SH", **overrides: object) -> StockAnomaly:
    values: dict[str, object] = {
        "trade_date": TRADE_DATE,
        "as_of_time": AS_OF,
        "symbol": symbol,
        "return_pct": Decimal("10"),
        "return_zscore": Decimal("4"),
        "price_anomaly": True,
        "volume": 100,
        "volume_median": Decimal("20"),
        "volume_ratio": Decimal("5"),
        "volume_anomaly": True,
        "amount": Decimal("1000"),
        "amount_median": Decimal("200"),
        "amount_ratio": Decimal("5"),
        "amount_anomaly": True,
        "history_observations": 20,
        "status": "ok",
        "available_at": datetime(2026, 8, 31, 18, 0, tzinfo=SHANGHAI),
    }
    values.update(overrides)
    return StockAnomaly(**values)


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
        "available_at": datetime(2026, 8, 31, 17, 0, tzinfo=SHANGHAI),
    }
    values.update(overrides)
    return SectorMembership(**values)


def snapshot(**overrides: object) -> SectorSnapshot:
    values: dict[str, object] = {
        "trade_date": TRADE_DATE,
        "as_of_time": AS_OF,
        "sector_code": "C15",
        "sector_name": "酒、饮料和精制茶制造业",
        "classification": "证监会行业分类",
        "member_count": 10,
        "valid_bar_count": 10,
        "coverage_ratio": Decimal("1"),
        "equal_weight_return_pct": Decimal("2"),
        "median_return_pct": Decimal("1"),
        "advancer_count": 7,
        "decliner_count": 2,
        "flat_count": 1,
        "advancer_ratio": Decimal("0.7"),
        "total_volume": 1000,
        "total_amount": Decimal("10000"),
        "top_gainer_symbol": "600519.SH",
        "top_gainer_return_pct": Decimal("10"),
        "top_loser_symbol": "600000.SH",
        "top_loser_return_pct": Decimal("-2"),
        "available_at": datetime(2026, 8, 31, 18, 30, tzinfo=SHANGHAI),
    }
    values.update(overrides)
    return SectorSnapshot(**values)


def ranked(
    anomalies=None, memberships=None, snapshots=None, *, top_n=None, as_of=AS_OF
):
    return rank_anomaly_candidates(
        [anomaly()] if anomalies is None else anomalies,
        [membership()] if memberships is None else memberships,
        [snapshot()] if snapshots is None else snapshots,
        as_of_time=as_of,
        top_n=top_n,
    )


@pytest.mark.parametrize(
    ("signals", "signal_type", "tier"),
    [
        ((True, True, True), "price_volume_amount", 1),
        ((True, True, False), "price_volume", 2),
        ((True, False, True), "price_amount", 2),
        ((True, False, False), "price_only", 3),
        ((False, True, True), "volume_amount", 4),
        ((False, True, False), "volume_only", 5),
        ((False, False, True), "amount_only", 5),
    ],
)
def test_signal_type_count_and_priority_tier(signals, signal_type, tier) -> None:
    item = anomaly(
        price_anomaly=signals[0],
        volume_anomaly=signals[1],
        amount_anomaly=signals[2],
    )
    candidate = ranked(anomalies=[item])[0]
    assert candidate.signal_count == sum(signals)
    assert candidate.signal_type == signal_type
    assert candidate.priority_tier == tier


def test_sector_join_return_breadth_and_excess_return() -> None:
    candidate = ranked()[0]
    assert (candidate.sector_code, candidate.sector_name) == (
        "C15",
        "酒、饮料和精制茶制造业",
    )
    assert candidate.sector_return_pct == Decimal("2")
    assert candidate.sector_advancer_ratio == Decimal("0.7")
    assert candidate.excess_return_pct == Decimal("8")
    assert candidate.sector_context == "stock_specific"


def test_sector_confirmed_and_mixed_context_are_objective() -> None:
    confirmed = ranked(
        anomalies=[anomaly(return_pct=Decimal("4"))],
        snapshots=[snapshot(equal_weight_return_pct=Decimal("2"))],
    )[0]
    mixed = ranked(
        anomalies=[anomaly(return_pct=Decimal("1"))],
        snapshots=[snapshot(equal_weight_return_pct=Decimal("-1"))],
    )[0]
    assert confirmed.sector_context == "sector_confirmed"
    assert mixed.sector_context == "mixed"


@pytest.mark.parametrize("missing", ["membership", "snapshot"])
def test_missing_sector_enrichment_preserves_candidate(missing: str) -> None:
    candidate = ranked(
        memberships=[] if missing == "membership" else None,
        snapshots=[] if missing == "snapshot" else None,
    )[0]
    assert candidate.symbol == "600519.SH"
    assert candidate.sector_code is None
    assert candidate.sector_return_pct is None
    assert candidate.excess_return_pct is None
    assert candidate.sector_context == "unavailable"


def test_pit_invisible_anomaly_is_excluded() -> None:
    future_as_of = datetime(2026, 9, 1, 20, 0, tzinfo=SHANGHAI)
    future = anomaly(as_of_time=future_as_of, available_at=future_as_of)
    assert ranked(anomalies=[future]) == []


def test_pit_invisible_membership_is_not_joined() -> None:
    future = membership(available_at=datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI))
    assert ranked(memberships=[future])[0].sector_code is None


def test_pit_invisible_sector_snapshot_is_not_joined() -> None:
    future_as_of = datetime(2026, 9, 1, 20, 0, tzinfo=SHANGHAI)
    future = snapshot(as_of_time=future_as_of, available_at=future_as_of)
    assert ranked(snapshots=[future])[0].sector_code is None


def test_ranking_uses_priority_tier_first() -> None:
    tier_1 = anomaly("600001.SH")
    tier_2 = anomaly("600002.SH", amount_anomaly=False, return_zscore=Decimal("99"))
    assert [item.symbol for item in ranked(anomalies=[tier_2, tier_1])] == [
        "600001.SH",
        "600002.SH",
    ]


def test_ranking_uses_absolute_zscore_within_tier() -> None:
    low = anomaly("600001.SH", return_zscore=Decimal("-3"))
    high = anomaly("600002.SH", return_zscore=Decimal("4"))
    assert ranked(anomalies=[low, high])[0].symbol == "600002.SH"


def test_ranking_uses_max_ratio_after_zscore() -> None:
    low = anomaly("600001.SH", volume_ratio=Decimal("4"), amount_ratio=Decimal("3"))
    high = anomaly("600002.SH", volume_ratio=Decimal("5"), amount_ratio=Decimal("2"))
    assert ranked(anomalies=[low, high])[0].symbol == "600002.SH"


def test_ranking_uses_absolute_excess_return_after_ratio() -> None:
    low = anomaly("600001.SH", return_pct=Decimal("3"))
    high = anomaly("600002.SH", return_pct=Decimal("10"))
    memberships = [membership("600001.SH"), membership("600002.SH")]
    assert ranked(anomalies=[low, high], memberships=memberships)[0].symbol == "600002.SH"


def test_ranking_symbol_tie_is_deterministic() -> None:
    items = [anomaly("600002.SH"), anomaly("600001.SH")]
    memberships = [membership("600002.SH"), membership("600001.SH")]
    first = ranked(anomalies=items, memberships=memberships)
    second = ranked(anomalies=list(reversed(items)), memberships=memberships)
    assert [item.symbol for item in first] == ["600001.SH", "600002.SH"]
    assert first == second


def test_top_n_limits_and_reassigns_rank() -> None:
    items = [anomaly(f"60000{index}.SH") for index in range(1, 4)]
    result = ranked(anomalies=items, memberships=[], snapshots=[], top_n=2)
    assert len(result) == 2
    assert [item.rank for item in result] == [1, 2]


def test_candidate_available_at_is_maximum_of_inputs_actually_used() -> None:
    candidate = ranked()[0]
    assert candidate.available_at == datetime(
        2026, 8, 31, 18, 30, tzinfo=SHANGHAI
    )


def test_no_anomaly_is_excluded() -> None:
    normal = anomaly(
        price_anomaly=False, volume_anomaly=False, amount_anomaly=False
    )
    assert ranked(anomalies=[normal]) == []


def test_candidate_is_market_intelligence_not_system_health() -> None:
    candidate = ranked()[0]
    assert not hasattr(candidate, "status")
    assert not hasattr(candidate, "overall_health")


def test_triage_config_rejects_non_positive_threshold() -> None:
    with pytest.raises(ValueError):
        AnomalyTriageConfig(excess_return_threshold_pct=Decimal("0"))
