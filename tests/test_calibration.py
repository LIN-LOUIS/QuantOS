from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from quantos.calibration import (
    audit_cross_provider_dedup, calibrate_attribution_evidence,
    calibrate_event_evidence,
)
from quantos.config import MARKET_TIMEZONE
from quantos.schemas import AnomalyCandidate, DiscoveryMention, FundFlowEvidence, WebSearchResult

EVENT = datetime(2026, 8, 31, 15, tzinfo=MARKET_TIMEZONE)
COLLECTED = datetime(2026, 9, 2, 10, tzinfo=MARKET_TIMEZONE)


def result(identity, *, provider="tavily", published=EVENT, url=None, title="公司公告",
           pit_mode="source_timestamp_proxy", collected=COLLECTED, available=None, score=None):
    available = collected if available is None else available
    return WebSearchResult(
        result_id=identity, query="测试公司", query_symbol="000001.SZ", title=title,
        snippet="", url=url or f"https://{provider}.test/{identity}", domain=f"{provider}.test",
        published_at=published, timestamp_basis="provider_reported" if published else "unknown",
        collected_at=collected, available_at=available, provider=provider,
        provider_record_id=identity, pit_mode=pit_mode, provider_score=score,
    )


def mention(item, method, tier):
    return DiscoveryMention(item.result_id, item.query_symbol, item.provider, item.query,
                            method, item.query, tier, item.collected_at)


@pytest.mark.parametrize("delta,inside", [(-86400, True), (43200, True), (-86401, False), (43201, False)])
def test_event_window_boundaries(delta, inside):
    item = result(str(delta), published=EVENT + timedelta(seconds=delta))
    selection = calibrate_event_evidence([item], [mention(item, "title_company_name", 2)], market_event_time=EVENT)
    flags = selection.quality_flags[0]
    assert flags.time_delta_seconds == delta and flags.inside_event_window is inside
    assert flags.eligible_for_event_evidence is inside
    assert flags.eligibility_reason == ("title_entity_in_window" if inside else "outside_event_window")


def test_title_snippet_and_query_only_known_time_are_eligible():
    items = [result("title"), result("snippet"), result("query")]
    methods = ["title_company_name", "snippet_alias", "provider_exact_company_query"]
    tiers = [2, 3, 3]
    selected = calibrate_event_evidence(items, [mention(i, m, t) for i, m, t in zip(items, methods, tiers)], market_event_time=EVENT)
    assert len(selected.discovery_evidence) == len(selected.event_evidence) == 3
    assert [item.eligibility_reason for item in selected.quality_flags] == [
        "query_only_in_window", "snippet_entity_in_window", "title_entity_in_window"
    ]
    query = next(item for item in selected.quality_flags if item.query_only_match)
    assert query.entity_evidence_basis == "provider_query_only" and query.evidence_tier == 3


def test_unknown_timestamp_is_preserved_but_not_event_eligible():
    item = result("unknown", published=None)
    selected = calibrate_event_evidence([item], [mention(item, "provider_exact_company_query", 3)], market_event_time=EVENT)
    flags = selected.quality_flags[0]
    assert selected.discovery_evidence == (item,) and selected.event_evidence == ()
    assert flags.timestamp_quality == "unknown" and flags.inside_event_window is None
    assert flags.eligibility_reason == "timestamp_unknown"


def test_unlinked_known_record_is_entity_unconfirmed():
    item = result("unlinked")
    flags = calibrate_event_evidence([item], [], market_event_time=EVENT).quality_flags[0]
    assert not flags.eligible_for_event_evidence and flags.eligibility_reason == "entity_unconfirmed"


def test_provider_proxy_metrics_are_objective_ratios():
    known = result("known")
    unknown = result("unknown", published=None)
    selected = calibrate_event_evidence(
        [known, unknown], [mention(known, "title_company_name", 2), mention(unknown, "provider_exact_company_query", 3)],
        market_event_time=EVENT,
    )
    metrics = selected.provider_metrics[0]
    assert metrics.discovery_count == 2 and metrics.event_eligible_count == 1
    assert metrics.title_exact_ratio == metrics.query_only_ratio == metrics.known_timestamp_ratio == 0.5
    assert metrics.inside_window_ratio == 1.0 and metrics.event_eligible_ratio == 0.5
    assert not hasattr(metrics, "quality_score")


def test_cross_provider_dedup_audit_is_deterministic_and_title_only_does_not_merge():
    records = [
        result("a", provider="tavily", url="https://EXAMPLE.com/a?utm_source=x", title="共同标题"),
        result("b", provider="exa", url="https://example.com/a", title="共同标题"),
        result("c", provider="tavily", url="https://x.test/c", title="仅标题相同"),
        result("d", provider="exa", url="https://y.test/d", title="仅标题相同"),
    ]
    audit = audit_cross_provider_dedup(records)
    assert audit.exact_url_shared == 0 and audit.normalized_url_shared == 1
    assert audit.title_domain_shared == 1 and audit.title_only_shared == 2


def test_event_ranking_ignores_provider_score():
    near = result("near", published=EVENT + timedelta(seconds=1))
    far = result("far", published=EVENT + timedelta(seconds=100))
    near = WebSearchResult(**{name: getattr(near, name) for name in near.__dataclass_fields__ if name != "provider_score"}, provider_score=0.01)
    far = WebSearchResult(**{name: getattr(far, name) for name in far.__dataclass_fields__ if name != "provider_score"}, provider_score=0.99)
    selected = calibrate_event_evidence([far, near], [mention(far, "title_company_name", 2), mention(near, "title_company_name", 2)], market_event_time=EVENT)
    assert [item.result_id for item in selected.event_evidence] == ["near", "far"]


def candidate():
    return AnomalyCandidate(
        date(2026, 8, 31), COLLECTED, "000001.SZ", None, None, None,
        Decimal("10"), Decimal("4"), Decimal("3"), Decimal("3"), True, True, True,
        3, "price_volume_amount", None, None, None, "unavailable", 1, 1, 20, COLLECTED,
    )


def flow():
    return FundFlowEvidence(
        date(2026, 8, 31), COLLECTED, "000001.SZ", Decimal("1"), Decimal("0.1"),
        Decimal("2"), Decimal("3"), Decimal("5"), Decimal("0.5"), Decimal("-1"),
        Decimal("-2"), "inflow", COLLECTED,
    )


def attribution(items, methods, *, as_of=COLLECTED):
    events = calibrate_event_evidence(
        items, [mention(item, method, 3 if method in {"snippet_alias", "provider_exact_company_query"} else 2)
                for item, method in zip(items, methods)], market_event_time=EVENT,
    )
    return calibrate_attribution_evidence(events, [candidate()], [flow()],
                                          market_event_time=EVENT, as_of_time=as_of)


@pytest.mark.parametrize("method,strength", [
    ("title_company_name", "strong"), ("title_alias", "strong"),
    ("title_symbol", "strong"), ("snippet_company_name", "strong"),
    ("snippet_alias", "strong"), ("provider_exact_company_query", "weak"),
])
def test_entity_strength_is_discrete(method, strength):
    item = result(method, published=EVENT - timedelta(hours=1))
    assert attribution([item], [method]).attribution_facts[0].entity_strength == strength


@pytest.mark.parametrize("published,relation", [
    (EVENT - timedelta(seconds=1), "pre_event"), (EVENT, "at_event"),
    (EVENT + timedelta(seconds=1), "post_event"), (None, "unknown"),
])
def test_temporal_relation_is_timestamp_only(published, relation):
    item = result(str(relation), published=published)
    fact = attribution([item], ["title_company_name"]).attribution_facts[0]
    assert fact.source_temporal_relation == relation
    assert fact.knowledge_temporal_relation == "known_after_event"


@pytest.mark.parametrize("delta,eligible,reason", [
    (-86400, True, "eligible_strong_entity_pre_event"),
    (0, True, "eligible_strong_entity_pre_event"),
    (1, False, "post_event_followup"),
    (-86401, False, "outside_attribution_window"),
])
def test_attribution_window_boundaries(delta, eligible, reason):
    item = result(str(delta), published=EVENT + timedelta(seconds=delta))
    fact = attribution([item], ["title_company_name"]).attribution_facts[0]
    assert fact.attribution_eligible is eligible and fact.attribution_reason == reason


def test_query_only_unknown_and_post_event_never_attribution_eligible():
    items = [result("query", published=EVENT - timedelta(seconds=1)),
             result("unknown", published=None), result("post", published=EVENT + timedelta(seconds=1))]
    facts = attribution(items, ["provider_exact_company_query", "title_company_name", "title_company_name"]).attribution_facts
    assert not any(item.attribution_eligible for item in facts)
    assert {item.attribution_reason for item in facts} == {
        "query_only_weak_entity", "timestamp_unknown", "post_event_followup"
    }


def test_proxy_research_can_be_eligible_but_never_strict():
    item = result("proxy", published=EVENT - timedelta(seconds=1))
    fact = attribution([item], ["title_company_name"]).attribution_facts[0]
    assert fact.attribution_eligible and not fact.strict_attribution_eligible


def test_strict_live_requires_availability_cutoff():
    item = result("strict", published=EVENT - timedelta(seconds=1), pit_mode="strict_live")
    before = attribution([item], ["title_company_name"], as_of=COLLECTED - timedelta(seconds=1)).attribution_facts[0]
    visible = attribution([item], ["title_company_name"], as_of=COLLECTED).attribution_facts[0]
    assert before.attribution_eligible and not before.strict_attribution_eligible
    assert not visible.strict_attribution_eligible
    assert visible.source_temporal_relation == "pre_event"
    assert visible.knowledge_temporal_relation == "known_after_event"
    assert visible.attribution_reason == "known_after_market_event"


def test_strict_source_pre_knowledge_pre_is_eligible():
    observed = EVENT - timedelta(hours=4)
    item = result("strict-pre", published=EVENT - timedelta(hours=5), pit_mode="strict_live",
                  collected=observed)
    fact = attribution([item], ["title_company_name"], as_of=EVENT - timedelta(hours=3)).attribution_facts[0]
    assert fact.source_temporal_relation == "pre_event"
    assert fact.knowledge_temporal_relation == "known_before_event"
    assert fact.research_attribution_eligible and fact.strict_attribution_eligible


def test_exact_event_knowledge_boundary_is_strict_eligible():
    item = result("strict-at", published=EVENT - timedelta(hours=1), pit_mode="strict_live",
                  collected=EVENT)
    fact = attribution([item], ["title_company_name"], as_of=EVENT).attribution_facts[0]
    assert fact.knowledge_temporal_relation == "known_at_event"
    assert fact.strict_attribution_eligible


def test_as_of_after_event_does_not_retroactively_make_strict():
    observed = EVENT + timedelta(minutes=1)
    item = result("strict-late", published=EVENT - timedelta(hours=5), pit_mode="strict_live",
                  collected=observed)
    fact = attribution([item], ["title_company_name"], as_of=EVENT + timedelta(hours=1)).attribution_facts[0]
    assert fact.research_attribution_eligible and not fact.strict_attribution_eligible
    assert fact.knowledge_temporal_relation == "known_after_event"


def test_later_calendar_strict_live_cannot_explain_historical_event():
    item = result("strict-days-late", published=EVENT - timedelta(hours=5), pit_mode="strict_live",
                  collected=EVENT + timedelta(days=3))
    fact = attribution([item], ["title_company_name"], as_of=EVENT + timedelta(days=4)).attribution_facts[0]
    assert fact.research_attribution_eligible and not fact.strict_attribution_eligible
    assert fact.attribution_reason == "known_after_market_event"


def test_query_as_of_and_market_event_are_independent():
    observed = EVENT - timedelta(hours=1)
    item = result("future-target", published=EVENT - timedelta(hours=5), pit_mode="strict_live",
                  collected=observed)
    fact = attribution([item], ["title_company_name"], as_of=EVENT - timedelta(minutes=30)).attribution_facts[0]
    assert fact.strict_attribution_eligible


def test_bundle_separates_attribution_from_post_event_and_preserves_market_inputs():
    pre = result("pre", published=EVENT - timedelta(seconds=1))
    post = result("post", published=EVENT + timedelta(seconds=1))
    output = attribution([post, pre], ["title_company_name", "title_company_name"])
    bundle = output.bundles[0]
    assert bundle.candidate.rank == 1 and bundle.fund_flow_evidence == flow()
    assert bundle.research_attribution_evidence == (pre,)
    assert bundle.strict_attribution_evidence == () and bundle.post_event_context == (post,)
    assert not hasattr(output, "run_health")


def test_attribution_ranking_is_deterministic_and_ignores_provider_score():
    near = result("near-attribution", published=EVENT - timedelta(seconds=1), score=0.01)
    far = result("far-attribution", published=EVENT - timedelta(seconds=100), score=0.99)
    output = attribution([far, near], ["title_company_name", "title_company_name"])
    assert output.bundles[0].attribution_evidence == (near, far)
