from datetime import date, datetime
from decimal import Decimal
from threading import Barrier, Event, Lock

import pytest

from quantos.config import MARKET_TIMEZONE
from quantos.discovery import (
    ProviderPayload,
    RegisteredEvidenceProvider,
    deduplicate_cross_provider_news,
    discover_candidate_evidence,
    build_provider_benchmark_reports,
    collect_strict_live_candidate_evidence,
    web_search_runner,
)
from quantos.schemas import (
    AnnouncementRecord,
    AnomalyCandidate,
    DiscoveryMention,
    EvidenceDiscoveryRequest,
    FundFlowEvidence,
    NewsRecord,
    WebSearchResult,
    SecurityMaster,
)


def _dt(day=1, hour=12):
    return datetime(2026, 9, day, hour, tzinfo=MARKET_TIMEZONE)


def _request(mode="proxy_research", symbols=("000001.SZ",), names=("平安银行股份有限公司",)):
    return EvidenceDiscoveryRequest(
        target_trade_date=date(2026, 8, 31), market_event_time=_dt(1, 15),
        as_of_time=_dt(2, 23), symbols=symbols, company_names=names,
        mode=mode, top_n=max(20, len(symbols)), max_results_per_candidate=10,
    )


def _candidate(symbol="000001.SZ", rank=1):
    return AnomalyCandidate(
        trade_date=date(2026, 8, 31), as_of_time=_dt(2, 23), symbol=symbol,
        sector_code="J66", sector_name="货币金融服务", classification="证监会行业分类",
        return_pct=Decimal("5"), return_zscore=Decimal("4"),
        volume_ratio=Decimal("3"), amount_ratio=Decimal("3"),
        price_anomaly=True, volume_anomaly=True, amount_anomaly=True,
        signal_count=3, signal_type="price_volume_amount",
        sector_return_pct=Decimal("1"), sector_advancer_ratio=Decimal("0.5"),
        excess_return_pct=Decimal("4"), sector_context="mixed",
        priority_tier=1, rank=rank, history_observations=20, available_at=_dt(1, 19),
    )


def _flow(symbol="000001.SZ"):
    return FundFlowEvidence(
        date(2026, 8, 31), _dt(2, 23), symbol, Decimal("1"), Decimal("0.1"),
        Decimal("2"), Decimal("3"), Decimal("5"), Decimal("0.5"),
        Decimal("-1"), Decimal("-2"), "inflow", _dt(1, 19),
    )


def _news(news_id="n1", provider="gdelt", *, pit_mode="source_timestamp_proxy", url="https://x.test/a", title="平安银行新闻", domain="x.test"):
    return NewsRecord(
        news_id, domain, "news", _dt(1, 14), _dt(1, 20), _dt(1, 20), title,
        None, url, (), provider, news_id, pit_mode, domain=domain,
        timestamp_basis="provider_reported", evidence_basis="structured_news_metadata",
    )


def _search(result_id="s1", *, pit_mode="source_timestamp_proxy", url="https://x.test/a", title="平安银行新闻", domain="x.test"):
    return WebSearchResult(
        result_id, "平安银行", "000001.SZ", title, "snippet", url, domain, _dt(1, 14),
        "provider_reported", _dt(1, 21), _dt(1, 21), "search_api", result_id,
        pit_mode=pit_mode,
    )


def _announcement(pit_mode="source_timestamp_proxy"):
    return AnnouncementRecord(
        "a1", "000001.SZ", "平安银行", "平安银行公告", "https://cninfo.test/a.pdf",
        _dt(1, 8), _dt(1, 20), _dt(1, 20), "cninfo", "cninfo", "a1", pit_mode,
    )


def _mention(evidence_id, provider="gdelt"):
    return DiscoveryMention(
        evidence_id, "000001.SZ", provider, "平安银行", "title_company_name",
        "平安银行", 2 if provider != "cninfo" else 1, _dt(1, 20),
    )


def _payload(records=(), mentions=(), successful=("000001.SZ",), failed=()):
    return ProviderPayload(tuple(records), tuple(mentions), len(records), successful, failed)


def test_request_and_web_search_metadata_schema():
    request = _request()
    result = _search()
    assert request.mode == "proxy_research"
    assert result.fetch_status == "metadata_only"
    assert result.evidence_basis == "search_result_metadata"
    assert result.strict_pit is False


def test_provider_timeout_and_failure_are_isolated_and_sorted():
    providers = [
        RegisteredEvidenceProvider("z_timeout", lambda _: (_ for _ in ()).throw(TimeoutError())),
        RegisteredEvidenceProvider("a_pass", lambda _: _payload()),
        RegisteredEvidenceProvider("m_failed", lambda _: (_ for _ in ()).throw(RuntimeError("sensitive"))),
    ]
    output = discover_candidate_evidence(_request(), [_candidate()], [_flow()], providers)
    assert [(item.provider_name, item.status) for item in output.provider_results] == [
        ("a_pass", "pass"), ("m_failed", "failed"), ("z_timeout", "unavailable")
    ]
    assert output.provider_results[1].safe_error_type == "RuntimeError"
    assert "sensitive" not in repr(output.provider_results)


def test_all_candidate_failures_are_unavailable_and_partial_is_preserved():
    unavailable = RegisteredEvidenceProvider(
        "all_failed", lambda _: _payload(successful=(), failed=("000001.SZ",))
    )
    partial = RegisteredEvidenceProvider(
        "partial", lambda _: _payload(successful=("000001.SZ",), failed=("600519.SH",))
    )
    request = _request(symbols=("000001.SZ", "600519.SH"), names=("平安银行", "贵州茅台"))
    output = discover_candidate_evidence(request, [_candidate()], [], [unavailable, partial])
    assert [item.status for item in output.provider_results] == ["unavailable", "partial"]


def test_different_completion_order_has_deterministic_provider_order():
    b_done = Event()

    def slow(_):
        assert b_done.wait(timeout=1)
        return _payload()

    def fast(_):
        b_done.set()
        return _payload()

    output = discover_candidate_evidence(
        _request(), [_candidate()], [],
        [RegisteredEvidenceProvider("a_slow", slow), RegisteredEvidenceProvider("b_fast", fast)],
    )
    assert [item.provider_name for item in output.provider_results] == ["a_slow", "b_fast"]


def test_max_provider_workers_is_enforced():
    lock = Lock()
    gate = Barrier(2)
    state = {"started": 0, "active": 0, "maximum": 0}

    def runner(_):
        with lock:
            state["started"] += 1
            number = state["started"]
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        if number <= 2:
            gate.wait(timeout=1)
        with lock:
            state["active"] -= 1
        return _payload()

    providers = [RegisteredEvidenceProvider(str(i), runner) for i in range(3)]
    discover_candidate_evidence(_request(), [_candidate()], [], providers, max_provider_workers=2)
    assert state["maximum"] == 2


def test_cross_provider_url_dedup_preserves_provenance_and_queries():
    canonical, mapping = deduplicate_cross_provider_news(
        [_news()], [_search()], [_mention("n1"), _mention("s1", "search_api")]
    )
    assert len(canonical) == 1
    assert canonical[0].discovered_by == ("gdelt", "search_api")
    assert canonical[0].discovery_queries == ("平安银行",)
    assert mapping["n1"] == mapping["s1"]


def test_title_domain_and_near_time_dedup_rules_are_deterministic():
    title_domain, _ = deduplicate_cross_provider_news(
        [_news(url="https://x.test/a")],
        [_search(url="https://x.test/b")], [],
    )
    near_time, _ = deduplicate_cross_provider_news(
        [_news(url="https://x.test/a", domain="x.test")],
        [_search(url="https://y.test/b", domain="y.test")], [],
    )
    assert len(title_domain) == 1
    assert len(near_time) == 1


def test_announcement_is_not_deduplicated_into_news_and_bundle_keeps_inputs():
    records = (_announcement(), _news())
    mentions = (_mention("a1", "cninfo"), _mention("n1"))
    output = discover_candidate_evidence(
        _request(), [_candidate()], [_flow()],
        [RegisteredEvidenceProvider("mixed", lambda _: _payload(records, mentions))],
    )
    assert len(output.announcements) == 1
    assert len(output.canonical_news) == 1
    bundle = output.bundles[0]
    assert bundle.candidate.rank == 1
    assert bundle.fund_flow_evidence == _flow()
    assert len(bundle.announcement_evidence) == 1
    assert output.coverage.candidates[0].total_evidence_count == 2


def test_strict_mode_never_falls_back_to_proxy_evidence():
    proxy_records = (_announcement(), _news(), _search())
    proxy_mentions = (_mention("a1", "cninfo"), _mention("n1"), _mention("s1", "search_api"))
    provider = RegisteredEvidenceProvider("mixed", lambda _: _payload(proxy_records, proxy_mentions))
    strict = discover_candidate_evidence(_request("strict"), [_candidate()], [], [provider])
    proxy = discover_candidate_evidence(_request(), [_candidate()], [], [provider])
    assert strict.announcements == strict.canonical_news == strict.search_results == ()
    assert strict.coverage.candidates[0].total_evidence_count == 0
    assert strict.coverage.providers[0].evidence_count == 0
    assert proxy.coverage.candidates[0].total_evidence_count == 2


def test_strict_visible_records_are_included_and_disabled_provider_is_allowed():
    records = (_announcement("strict_live"), _news(pit_mode="strict_live"), _search(pit_mode="strict_live"))
    mentions = (_mention("a1", "cninfo"), _mention("n1"), _mention("s1", "search_api"))
    output = discover_candidate_evidence(
        _request("strict"), [_candidate()], [],
        [RegisteredEvidenceProvider("enabled", lambda _: _payload(records, mentions)),
         RegisteredEvidenceProvider("web_search", lambda _: _payload(), enabled=False)],
    )
    assert output.coverage.candidates[0].total_evidence_count == 2
    assert {item.status for item in output.provider_results} == {"pass", "unavailable"}


class _FakeWebProvider:
    def __init__(self, result_factory=None, fail=False):
        self.queries = []
        self.result_factory = result_factory
        self.fail = fail

    def search(self, **kwargs):
        self.queries.append((kwargs["query_symbol"], kwargs["query"]))
        if self.fail:
            raise RuntimeError("safe isolation")
        return [self.result_factory(kwargs)] if self.result_factory else []


def _result_for(kwargs, *, title="无关标题", snippet="无关摘要", provider="tavily", url="https://x.test/a"):
    return WebSearchResult(
        result_id=url, query=kwargs["query"], query_symbol=kwargs["query_symbol"],
        title=title, snippet=snippet, url=url, domain="x.test", published_at=None,
        timestamp_basis="unknown", collected_at=_dt(2, 10), available_at=_dt(2, 10),
        provider=provider, provider_record_id=url,
    )


@pytest.mark.parametrize("title,snippet,expected", [
    ("平安银行股份有限公司公告", "x", "title_company_name"),
    ("平安银行公告", "x", "title_alias"),
    ("000001 今日交易", "x", "title_symbol"),
    ("无关", "平安银行股份有限公司披露", "snippet_company_name"),
    ("无关", "平安银行披露", "snippet_alias"),
    ("无关", "无关", "provider_exact_company_query"),
])
def test_deterministic_web_entity_linking(title, snippet, expected):
    provider = _FakeWebProvider(lambda kw: _result_for(kw, title=title, snippet=snippet))
    request = _request()
    request = EvidenceDiscoveryRequest(
        request.target_trade_date, request.market_event_time, request.as_of_time,
        request.symbols, request.company_names, (("平安银行",),),
        mode=request.mode, max_results_per_candidate=10,
    )
    payload = web_search_runner(provider, "tavily")(request)
    assert payload.mentions[0].match_method == expected
    assert payload.mentions[0].evidence_tier == (2 if expected.startswith("title_") else 3)


def test_unsafe_alias_is_suppressed_and_query_b_not_sent():
    provider = _FakeWebProvider(lambda kw: _result_for(kw))
    base = _request()
    request = EvidenceDiscoveryRequest(base.target_trade_date, base.market_event_time, base.as_of_time,
        base.symbols, base.company_names, (("银行",),), mode=base.mode, max_results_per_candidate=10)
    payload = web_search_runner(provider, "tavily", include_safe_alias_query=True)(request)
    assert provider.queries == [("000001.SZ", "平安银行股份有限公司")]
    assert payload.mentions[0].match_method == "provider_exact_company_query"


def test_tavily_failure_exa_pass_and_inputs_unchanged():
    tavily = _FakeWebProvider(fail=True)
    exa = _FakeWebProvider(lambda kw: _result_for(kw, title="平安银行股份有限公司", provider="exa"))
    candidate, flow = _candidate(), _flow()
    output = discover_candidate_evidence(_request(), [candidate], [flow], [
        RegisteredEvidenceProvider("tavily", web_search_runner(tavily, "tavily")),
        RegisteredEvidenceProvider("exa", web_search_runner(exa, "exa")),
    ])
    assert [(row.provider_name, row.status) for row in output.provider_results] == [("exa", "pass"), ("tavily", "unavailable")]
    assert output.bundles[0].candidate == candidate
    assert output.bundles[0].fund_flow_evidence == flow
    assert not hasattr(output, "run_health")


def test_provider_benchmark_report_is_deterministic_and_has_no_score():
    provider = _FakeWebProvider(lambda kw: _result_for(kw, title="平安银行股份有限公司"))
    output = discover_candidate_evidence(_request(), [_candidate()], [_flow()], [
        RegisteredEvidenceProvider("tavily", web_search_runner(provider, "tavily")),
    ])
    report = build_provider_benchmark_reports(output)[0]
    assert report.request_count == 1 and report.coverage_ratio == 1.0
    assert report.title_exact_matches == 1 and report.query_only_matches == 0
    assert not hasattr(report, "provider_score")


class _StrictProvider:
    def __init__(self, collected_at):
        self.collected_at = collected_at

    def search(self, **kwargs):
        symbol = kwargs["query_symbol"]
        return [WebSearchResult(
            result_id=f"strict-{symbol}", query=kwargs["query"], query_symbol=symbol,
            title=kwargs["query"], snippet="", url=f"https://strict.test/{symbol}",
            domain="strict.test", published_at=kwargs["start_time"],
            timestamp_basis="provider_reported", collected_at=self.collected_at,
            available_at=self.collected_at, provider="tavily", provider_record_id=symbol,
            pit_mode=kwargs["pit_mode"],
        )]


def _security(symbol, name, available):
    return SecurityMaster(symbol, name, "SHSE" if symbol.endswith(".SH") else "SZSE",
                          date(2020, 1, 1), available)


def test_strict_live_callable_marks_records_and_preserves_inputs():
    cutoff = _dt(2, 10)
    candidate, flow = _candidate(), _flow()
    output = collect_strict_live_candidate_evidence(
        trade_date=date(2026, 8, 31), as_of_time=cutoff, candidates=[candidate],
        securities=[_security(candidate.symbol, "平安银行", _dt(1, 9))],
        providers={"tavily": _StrictProvider(cutoff)}, fund_flows=[flow],
    )
    assert len(output.search_results) == 1 and output.search_results[0].strict_pit
    assert output.search_results[0].available_at == cutoff
    assert output.bundles[0].candidate == candidate and output.bundles[0].fund_flow_evidence == flow
    assert not hasattr(output, "run_health")


def test_query_budget_makes_provider_partial_without_query_b():
    cutoff = _dt(2, 10)
    candidates = [_candidate("000001.SZ", 1), _candidate("600519.SH", 2)]
    output = collect_strict_live_candidate_evidence(
        trade_date=date(2026, 8, 31), as_of_time=cutoff, candidates=candidates,
        securities=[_security("000001.SZ", "平安银行", _dt(1, 9)),
                    _security("600519.SH", "贵州茅台", _dt(1, 9))],
        providers={"tavily": _StrictProvider(cutoff)}, max_queries_per_provider_per_run=1,
    )
    result = output.provider_results[0]
    assert result.status == "partial" and result.request_count == 1
    assert result.safe_error_type == "QueryBudgetReached"
    assert result.failed_candidates == ("600519.SH",)
