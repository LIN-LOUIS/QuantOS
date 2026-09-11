from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from quantos.news import (
    announcement_timing_label,
    build_candidate_evidence_bundle,
    enrich_candidates_with_news,
    enrich_candidates_with_announcements,
    link_news_entities,
    normalize_company_alias,
    rank_news_mentions,
    stable_news_id,
    tag_news_events,
)
from quantos.schemas import (
    AnnouncementRecord, AnomalyCandidate, FundFlowEvidence, NewsRecord, SecurityMaster,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
EVENT = datetime(2026, 8, 31, 15, 0, tzinfo=SHANGHAI)
AS_OF = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)


def news(title="贵州茅台发布重大合同公告", content="正文", **overrides):
    published = overrides.pop("published_at", datetime(2026, 8, 31, 14, 0, tzinfo=SHANGHAI))
    values = {
        "news_id": stable_news_id("sina", published, title, content), "source": "sina",
        "source_type": "news", "published_at": published, "collected_at": AS_OF,
        "available_at": AS_OF, "title": title, "content": content, "url": None,
        "channels": (), "provider": "tushare", "provider_record_id": "provider-id",
        "pit_mode": "source_timestamp_proxy",
    }
    values.update(overrides)
    return NewsRecord(**values)


def announcement(**overrides):
    values = {
        "announcement_id": "ann-1", "symbol": "600519.SH", "company_name": "贵州茅台",
        "title": "关于回购的公告", "url": None,
        "published_at": datetime(2026, 8, 31, 13, 0, tzinfo=SHANGHAI),
        "collected_at": AS_OF, "available_at": AS_OF, "source": "tushare_anns_d",
        "provider": "tushare", "provider_record_id": "ann-provider-1",
        "pit_mode": "source_timestamp_proxy",
    }
    values.update(overrides)
    return AnnouncementRecord(**values)


def security(symbol="600519.SH", name="贵州茅台股份有限公司"):
    return SecurityMaster(
        symbol=symbol, company_name=name, exchange="SHSE" if symbol.endswith(".SH") else "SZSE",
        effective_from=date(2000, 1, 1), available_at=datetime(2026, 8, 31, 10, 0, tzinfo=SHANGHAI),
        is_active=True, source="baostock", asset_type="stock", status="listed",
    )


def candidate():
    return AnomalyCandidate(
        trade_date=date(2026, 8, 31), as_of_time=AS_OF, symbol="600519.SH",
        sector_code=None, sector_name=None, classification=None,
        return_pct=Decimal("10"), return_zscore=Decimal("4"), volume_ratio=Decimal("5"), amount_ratio=Decimal("5"),
        price_anomaly=True, volume_anomaly=True, amount_anomaly=True,
        signal_count=3, signal_type="price_volume_amount", sector_return_pct=None,
        sector_advancer_ratio=None, excess_return_pct=None, sector_context="unavailable",
        priority_tier=1, rank=7, history_observations=20,
        available_at=datetime(2026, 8, 31, 18, 0, tzinfo=SHANGHAI),
    )


def test_news_and_announcement_schema_pit_modes() -> None:
    proxy = news()
    strict = replace(proxy, news_id="strict", pit_mode="strict_live")
    assert proxy.strict_pit is False
    assert strict.strict_pit is True
    assert announcement().strict_pit is False
    assert proxy.available_at == proxy.collected_at > proxy.published_at


def test_stable_news_id_normalizes_whitespace_and_is_deterministic() -> None:
    published = datetime(2026, 8, 31, 10, 0, tzinfo=SHANGHAI)
    assert stable_news_id("SINA", published, "标题 A", "正文") == stable_news_id(" sina ", published, " 标题A ", "正 文")


def test_announcement_direct_symbol_link_is_tier_one() -> None:
    mention = link_news_entities([], [announcement()], [], as_of_time=AS_OF)[0]
    assert mention.match_method == "announcement_symbol"
    assert mention.direct_symbol_link and mention.evidence_tier == 1


def test_title_company_alias_exact_link_is_tier_two() -> None:
    mention = link_news_entities([news(title="贵州茅台发布业绩预告")], [], [security()], as_of_time=AS_OF)[0]
    assert mention.match_method == "title_company_name"
    assert mention.matched_term == "贵州茅台"
    assert mention.evidence_tier == 2


def test_title_symbol_link_is_tier_two() -> None:
    mention = link_news_entities([news(title="600519发布公告", content="")], [], [security()], as_of_time=AS_OF)[0]
    assert mention.match_method == "title_symbol" and mention.evidence_tier == 2


def test_body_company_link_is_tier_three() -> None:
    mention = link_news_entities([news(title="市场信息", content="贵州茅台披露公告")], [], [security()], as_of_time=AS_OF)[0]
    assert mention.match_method == "body_company_name" and mention.evidence_tier == 3


def test_unrelated_company_is_not_linked() -> None:
    assert link_news_entities([news(title="宁德时代发布公告", content="")], [], [security()], as_of_time=AS_OF) == []


def test_alias_normalization_only_removes_explicit_company_suffix() -> None:
    assert normalize_company_alias(" 贵州茅台股份有限公司 ") == "贵州茅台"
    assert normalize_company_alias("贵州茅台") == "贵州茅台"


def test_pit_invisible_news_is_excluded_from_linking() -> None:
    future = news(available_at=AS_OF + timedelta(hours=1), collected_at=AS_OF + timedelta(hours=1))
    assert link_news_entities([future], [], [security()], as_of_time=AS_OF) == []


def test_news_ranking_uses_tier_then_time_distance_deterministically() -> None:
    records = [news(title="贵州茅台A", published_at=EVENT - timedelta(hours=3)), news(title="贵州茅台B", published_at=EVENT - timedelta(hours=1))]
    mentions = link_news_entities(records, [announcement()], [security()], as_of_time=AS_OF)
    ranked = rank_news_mentions(mentions, market_event_time=EVENT)
    assert ranked[0].evidence_tier == 1
    assert ranked[1].published_at == EVENT - timedelta(hours=1)


def test_event_tags_are_keyword_only_without_sentiment() -> None:
    tags = tag_news_events("公司发布业绩预告及回购计划", "涉及重大合同")
    assert tags == ("业绩", "预告", "回购", "重大合同")
    assert "positive" not in tags and "negative" not in tags


def test_candidate_enrichment_preserves_rank_and_tier() -> None:
    record = news()
    mentions = link_news_entities([record], [], [security()], as_of_time=AS_OF)
    result = enrich_candidates_with_news(
        [candidate()], mentions, [record], [], market_event_time=EVENT, as_of_time=AS_OF
    )["600519.SH"][0]
    assert result.candidate.rank == 7
    assert result.candidate.priority_tier == 1
    assert result.candidate.signal_type == "price_volume_amount"


def test_future_outside_retrieval_window_is_excluded_and_missing_news_allowed() -> None:
    future = news(published_at=EVENT + timedelta(hours=13), collected_at=AS_OF + timedelta(days=1), available_at=AS_OF + timedelta(days=1))
    result = enrich_candidates_with_news(
        [candidate()], [], [future], [], market_event_time=EVENT, as_of_time=AS_OF
    )
    assert result == {"600519.SH": ()}


def test_evidence_bundle_combines_without_copying_or_causality() -> None:
    source = candidate()
    record = news()
    mention = link_news_entities([record], [], [security()], as_of_time=AS_OF)[0]
    evidence = enrich_candidates_with_news(
        [source], [mention], [record], [], market_event_time=EVENT, as_of_time=AS_OF
    )[source.symbol]
    flow = FundFlowEvidence(
        trade_date=source.trade_date, as_of_time=AS_OF, symbol=source.symbol,
        net_mf_amount=Decimal("1"), net_mf_ratio=Decimal("0.1"), large_net_amount=Decimal("1"),
        extra_large_net_amount=Decimal("0"), large_plus_extra_net_amount=Decimal("1"),
        large_plus_extra_ratio=Decimal("0.1"), small_net_amount=Decimal("0"), medium_net_amount=Decimal("0"),
        flow_direction="inflow", available_at=datetime(2026, 8, 31, 19, 0, tzinfo=SHANGHAI),
    )
    bundle = build_candidate_evidence_bundle(source, fund_flow_evidence=flow, news_evidence=evidence)
    assert bundle.candidate is source and bundle.fund_flow_evidence is flow
    assert bundle.news_evidence == evidence
    assert not hasattr(bundle, "causal_attribution")


def test_news_evidence_does_not_define_system_health() -> None:
    bundle = build_candidate_evidence_bundle(candidate(), fund_flow_evidence=None, news_evidence=[])
    assert not hasattr(bundle, "status") and not hasattr(bundle, "overall_health")


def test_announcement_timing_labels_are_deterministic() -> None:
    assert announcement_timing_label(datetime(2026, 8, 31, 9, 29, tzinfo=SHANGHAI)) == "pre_market"
    assert announcement_timing_label(datetime(2026, 8, 31, 9, 30, tzinfo=SHANGHAI)) == "intraday"
    assert announcement_timing_label(datetime(2026, 8, 31, 15, 0, tzinfo=SHANGHAI)) == "intraday"
    assert announcement_timing_label(datetime(2026, 8, 31, 15, 1, tzinfo=SHANGHAI)) == "post_market"


def test_announcement_enrichment_applies_window_and_max_five_without_reranking() -> None:
    records = [
        announcement(
            announcement_id=f"ann-{index}", provider_record_id=f"ann-{index}",
            published_at=EVENT + timedelta(minutes=index),
        )
        for index in range(7)
    ]
    result = enrich_candidates_with_announcements(
        [candidate()], records, market_event_time=EVENT, as_of_time=AS_OF,
        max_per_candidate=5,
    )["600519.SH"]
    assert len(result) == 5
    assert all(item.mention.evidence_tier == 1 for item in result)
    assert all(item.candidate.rank == 7 for item in result)


def test_announcement_outside_before_after_window_is_excluded() -> None:
    old = announcement(
        announcement_id="old", provider_record_id="old",
        published_at=EVENT - timedelta(hours=25),
    )
    result = enrich_candidates_with_announcements(
        [candidate()], [old], market_event_time=EVENT, as_of_time=AS_OF
    )
    assert result == {"600519.SH": ()}
