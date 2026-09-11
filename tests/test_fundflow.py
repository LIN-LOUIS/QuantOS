from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantos.fundflow import (
    build_fund_flow_evidence,
    build_market_fund_flow,
    build_sector_fund_flows,
    enrich_candidates_with_fund_flow,
    rank_sector_fund_flows,
)
from quantos.schemas import AnomalyCandidate, MarketBar, MoneyFlowRecord, SectorMembership

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 8, 31)
AS_OF = datetime(2026, 8, 31, 20, 0, tzinfo=SHANGHAI)


def flow(symbol="600519.SH", net=Decimal("7000"), **overrides):
    values = {
        "symbol": symbol, "trade_date": TRADE_DATE,
        "buy_sm_volume": 100, "buy_sm_amount": Decimal("1000"), "sell_sm_volume": 50, "sell_sm_amount": Decimal("400"),
        "buy_md_volume": 200, "buy_md_amount": Decimal("2000"), "sell_md_volume": 100, "sell_md_amount": Decimal("800"),
        "buy_lg_volume": 300, "buy_lg_amount": Decimal("5000"), "sell_lg_volume": 100, "sell_lg_amount": Decimal("1000"),
        "buy_elg_volume": 400, "buy_elg_amount": Decimal("9000"), "sell_elg_volume": 100, "sell_elg_amount": Decimal("2000"),
        "net_mf_volume": 700, "net_mf_amount": net, "source": "tushare",
        "available_at": datetime(2026, 8, 31, 19, 0, tzinfo=SHANGHAI),
        "collected_at": datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
    }
    values.update(overrides)
    return MoneyFlowRecord(**values)


def bar(symbol="600519.SH", amount=Decimal("100000")):
    return MarketBar(
        symbol=symbol, timestamp=datetime(2026, 8, 31, 15, 0, tzinfo=SHANGHAI), frequency="1d",
        open=Decimal("100"), high=Decimal("110"), low=Decimal("90"), close=Decimal("105"),
        prev_close=Decimal("100"), volume=10000, amount=amount, source="tushare",
        source_record_id=f"tushare:{symbol}:1d:2026-08-31",
        collected_at=datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
        available_at=datetime(2026, 8, 31, 18, 0, tzinfo=SHANGHAI),
    )


def membership(symbol="600519.SH", code="C15"):
    return SectorMembership(
        symbol=symbol, company_name=symbol, exchange="SHSE" if symbol.endswith(".SH") else "SZSE",
        sector_code=code, sector_name=f"sector-{code}", classification="证监会行业分类",
        raw_industry=f"{code}sector", source="baostock", effective_from=date(2000, 1, 1),
        available_at=datetime(2026, 8, 31, 17, 0, tzinfo=SHANGHAI),
    )


def candidate(symbol="600519.SH"):
    return AnomalyCandidate(
        trade_date=TRADE_DATE, as_of_time=AS_OF, symbol=symbol,
        sector_code=None, sector_name=None, classification=None,
        return_pct=Decimal("10"), return_zscore=Decimal("4"), volume_ratio=Decimal("5"), amount_ratio=Decimal("5"),
        price_anomaly=True, volume_anomaly=True, amount_anomaly=True,
        signal_count=3, signal_type="price_volume_amount", sector_return_pct=None,
        sector_advancer_ratio=None, excess_return_pct=None, sector_context="unavailable",
        priority_tier=1, rank=7, history_observations=20,
        available_at=datetime(2026, 8, 31, 18, 0, tzinfo=SHANGHAI),
    )


def test_evidence_derived_amounts_and_ratios() -> None:
    result = build_fund_flow_evidence(flow(), bar(), as_of_time=AS_OF)
    assert result.large_net_amount == Decimal("4000")
    assert result.extra_large_net_amount == Decimal("7000")
    assert result.large_plus_extra_net_amount == Decimal("11000")
    assert result.small_net_amount == Decimal("600")
    assert result.medium_net_amount == Decimal("1200")
    assert result.net_mf_ratio == Decimal("0.07")
    assert result.large_plus_extra_ratio == Decimal("0.11")
    assert result.flow_direction == "inflow"


def test_zero_market_amount_has_no_ratios() -> None:
    result = build_fund_flow_evidence(flow(), bar(amount=Decimal("0")), as_of_time=AS_OF)
    assert result.net_mf_ratio is None
    assert result.large_plus_extra_ratio is None


def test_evidence_rejects_pit_invisible_moneyflow() -> None:
    future = replace(flow(), available_at=datetime(2026, 9, 1, 19, 0, tzinfo=SHANGHAI))
    with pytest.raises(ValueError, match="PIT-visible"):
        build_fund_flow_evidence(future, bar(), as_of_time=AS_OF)


def test_flow_direction_is_objective() -> None:
    assert build_fund_flow_evidence(flow(net=Decimal("-1")), bar(), as_of_time=AS_OF).flow_direction == "outflow"
    assert build_fund_flow_evidence(flow(net=Decimal("0")), bar(), as_of_time=AS_OF).flow_direction == "flat"


def test_candidate_enrichment_preserves_ranking_fields() -> None:
    source = candidate()
    enriched = enrich_candidates_with_fund_flow([source], [flow()], [bar()], as_of_time=AS_OF)[0]
    assert enriched.fund_flow is not None
    assert enriched.candidate.rank == source.rank
    assert enriched.candidate.priority_tier == source.priority_tier
    assert enriched.candidate.signal_count == source.signal_count
    assert enriched.candidate.signal_type == source.signal_type


def test_missing_or_future_moneyflow_keeps_candidate_without_evidence() -> None:
    future = replace(flow(), available_at=datetime(2026, 9, 1, 19, 0, tzinfo=SHANGHAI))
    assert enrich_candidates_with_fund_flow([candidate()], [], [bar()], as_of_time=AS_OF)[0].fund_flow is None
    assert enrich_candidates_with_fund_flow([candidate()], [future], [bar()], as_of_time=AS_OF)[0].fund_flow is None


def test_sector_aggregation_uses_sums_coverage_and_breadth() -> None:
    memberships = [membership(), membership("000001.SZ")]
    flows = [flow(), flow("000001.SZ", net=Decimal("-2000"))]
    result = build_sector_fund_flows(flows, memberships, trade_date=TRADE_DATE, as_of_time=AS_OF)[0]
    assert result.member_count == 2 and result.valid_moneyflow_count == 2
    assert result.coverage_ratio == Decimal("1")
    assert result.net_mf_amount == Decimal("5000")
    assert result.large_net_amount == Decimal("8000")
    assert result.extra_large_net_amount == Decimal("14000")
    assert result.large_plus_extra_net_amount == Decimal("22000")
    assert (result.inflow_stock_count, result.outflow_stock_count, result.flat_stock_count) == (1, 1, 0)


def test_sector_missing_moneyflow_is_data_quality_coverage() -> None:
    result = build_sector_fund_flows(
        [flow()], [membership(), membership("000001.SZ")],
        trade_date=TRADE_DATE, as_of_time=AS_OF,
    )[0]
    assert result.valid_moneyflow_count == 1
    assert result.coverage_ratio == Decimal("0.5")


def test_sector_future_membership_is_excluded() -> None:
    future = replace(
        membership(),
        available_at=datetime(2026, 9, 1, 17, 0, tzinfo=SHANGHAI),
    )
    assert build_sector_fund_flows(
        [flow()], [future], trade_date=TRADE_DATE, as_of_time=AS_OF
    ) == []


def test_candidate_can_receive_pit_visible_sector_net_amount() -> None:
    source = replace(
        candidate(),
        sector_code="C15",
        sector_name="sector-C15",
        classification="证监会行业分类",
        sector_return_pct=Decimal("2"),
        sector_advancer_ratio=Decimal("0.5"),
        excess_return_pct=Decimal("8"),
        sector_context="stock_specific",
    )
    sector = build_sector_fund_flows(
        [flow()], [membership()], trade_date=TRADE_DATE, as_of_time=AS_OF
    )[0]
    enriched = enrich_candidates_with_fund_flow(
        [source], [flow()], [bar()], as_of_time=AS_OF, sector_flows=[sector]
    )[0]
    assert enriched.sector_net_mf_amount == sector.net_mf_amount


def test_sector_ranking_is_deterministic_both_directions() -> None:
    memberships = [membership("600519.SH", "C15"), membership("000001.SZ", "J66")]
    sectors = build_sector_fund_flows(
        [flow("600519.SH", Decimal("100")), flow("000001.SZ", Decimal("-200"))],
        memberships, trade_date=TRADE_DATE, as_of_time=AS_OF,
    )
    assert [item.sector_code for item in rank_sector_fund_flows(sectors)] == ["C15", "J66"]
    assert [item.sector_code for item in rank_sector_fund_flows(sectors, descending=False)] == ["J66", "C15"]


def test_market_level_aggregation() -> None:
    result = build_market_fund_flow(
        [flow(net=Decimal("7000")), flow("000001.SZ", net=Decimal("-2000")), flow("300750.SZ", net=Decimal("0"))],
        trade_date=TRADE_DATE, as_of_time=AS_OF,
    )
    assert result.valid_moneyflow_count == 3
    assert result.net_mf_amount == Decimal("5000")
    assert result.large_plus_extra_net_amount == Decimal("33000")
    assert (result.inflow_stock_count, result.outflow_stock_count, result.flat_stock_count) == (1, 1, 1)


def test_fundflow_evidence_does_not_define_system_health() -> None:
    result = build_market_fund_flow([flow()], trade_date=TRADE_DATE, as_of_time=AS_OF)
    assert not hasattr(result, "overall_health")
    assert not hasattr(result, "status")
