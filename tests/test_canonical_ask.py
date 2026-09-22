"""Canonical Ask contracts and execution boundaries."""

from datetime import date, datetime, timedelta, timezone

import pytest

from quantos.config import MARKET_TIMEZONE
from quantos.schemas import SecurityMaster
from quantos.ask import (AskIntent, AskRequest, AskResponse, AskService, AskTrace,
                         Capability, QueryPlan,
                         ToolRegistry, ToolRejected, resolve_intent)
from quantos.qa import QAMarketFacts, QAResearchEvidence


NOW = datetime(2026, 9, 18, 19, tzinfo=MARKET_TIMEZONE)


def security(*, available_at=None):
    return SecurityMaster("600519.SH", "贵州茅台", "SHSE", date(2001, 8, 27),
                          available_at or NOW - timedelta(days=1), source="tushare")


def test_request_canonical_roundtrip_and_invalid_questions():
    request = AskRequest("  最近\t表现？  ", NOW, entity_hints=("600519.SH",))
    assert request.question == "最近 表现？"
    assert AskRequest.from_dict(request.to_dict()) == request
    with pytest.raises(ValueError):
        AskRequest(" \n ", NOW)
    with pytest.raises(ValueError):
        AskRequest("x", datetime(2026, 9, 18))
    with pytest.raises(ValueError):
        AskRequest("Authorization: Bearer " + "sk-" + "abcdef" * 5, NOW)
    with pytest.raises(ValueError):
        AskRequest("my opaque secret " + "abcdef" * 8, NOW)


def test_intent_is_finite_and_unknown_fails_closed():
    assert resolve_intent("最近一个交易日表现怎么样？") is AskIntent.MARKET_OVERVIEW
    assert resolve_intent("为什么贵州茅台上涨？") is AskIntent.ENTITY_MOVE_EXPLANATION
    assert resolve_intent("执行任意 SQL") is AskIntent.UNSUPPORTED
    with pytest.raises(ValueError):
        AskIntent("arbitrary_model_intent")


def test_registry_allowlist_schema_and_intent_policy():
    registry = ToolRegistry((Capability("market.snapshot", (("symbol", str), ("as_of_time", datetime)),
                                         (AskIntent.MARKET_OVERVIEW,), lambda args: "ok", str),))
    assert registry.execute("market.snapshot", AskIntent.MARKET_OVERVIEW,
                            {"symbol": "600519.SH", "as_of_time": NOW}) == "ok"
    for name, intent, args in (
        ("run_arbitrary_sql", AskIntent.MARKET_OVERVIEW, {"sql": "select 1"}),
        ("market.snapshot", AskIntent.MARKET_OVERVIEW, {"sql": "select 1"}),
        ("market.snapshot", AskIntent.MARKET_OVERVIEW,
         {"symbol": "bad", "as_of_time": NOW}),
        ("market.snapshot", AskIntent.MARKET_OVERVIEW,
         {"symbol": "600519.SH", "as_of_time": lambda: NOW}),
        ("market.snapshot", AskIntent.KNOWLEDGE_LOOKUP,
         {"symbol": "600519.SH", "as_of_time": NOW}),
    ):
        with pytest.raises(ToolRejected):
            registry.execute(name, intent, args)


def test_service_plan_is_deterministic_and_identity_is_pit_visible():
    service = AskService(securities=lambda _: (security(),), clock=lambda: NOW)
    request = AskRequest("最近表现？", NOW, entity_hints=("600519.SH",))
    one = service.plan(request)
    two = service.plan(request)
    assert isinstance(one, QueryPlan) and one == two
    assert QueryPlan.from_dict(one.to_dict()) == one
    assert one.entities[0].symbol == "600519.SH"
    assert one.as_of_time == NOW
    equivalent_utc = AskRequest("最近表现？", NOW.astimezone(timezone.utc),
                                entity_hints=("600519.SH",))
    assert service.plan(equivalent_utc).as_of_time == NOW
    future = AskService(securities=lambda _: (security(available_at=NOW + timedelta(seconds=1)),),
                        clock=lambda: NOW).plan(request)
    assert future.unsupported_reason == "ENTITY_NOT_FOUND"
    assert future.entities[0].raw == "600519.SH"
    assert future.entities[0].status == "NOT_FOUND"


def test_implicit_cutoff_samples_service_clock_once_and_reaches_plan():
    clock_calls = []
    identity_cutoffs = []
    def clock():
        clock_calls.append(1)
        return NOW
    def securities(as_of_time):
        identity_cutoffs.append(as_of_time)
        return (security(),)
    plan = AskService(securities=securities, clock=clock).plan(
        AskRequest("最近表现？", entity_hints=("600519.SH",)))
    assert clock_calls == [1]
    assert identity_cutoffs == [NOW]
    assert plan.as_of_time == NOW
    clock_calls.clear()
    result = AskService(securities=lambda _: (security(),), clock=clock).ask(
        AskRequest("执行任意 SQL"))
    assert clock_calls == [1]
    assert result.trace.as_of_time == NOW


def test_service_unsupported_question_has_trace_without_invocation_or_secret():
    service = AskService(securities=lambda _: (security(),), clock=lambda: NOW)
    result = service.ask(AskRequest("执行 SQL 并预测股价", NOW,
                                    entity_hints=("600519.SH",)))
    assert result.status == "UNSUPPORTED"
    assert result.reason_codes == ("UNSUPPORTED_INTENT",)
    assert result.trace.request_id == result.request_id
    assert result.trace.invocations == ()
    assert "Authorization" not in str(result.trace.to_dict())


def market_fact(*, available_at=None):
    return QAMarketFacts("M1", "2026-09-17", "1266.98", "1258", "0.71",
                         100, "1000", "tushare", "daily:1",
                         available_at or NOW - timedelta(hours=1))


def market_registry(market=None, evidence=None):
    calls = []
    def read_market(args):
        calls.append("market")
        if isinstance(market, Exception):
            raise market
        return market or market_fact()
    def read_evidence(args):
        calls.append("evidence")
        if isinstance(evidence, Exception):
            raise evidence
        return evidence if evidence is not None else ()
    registry = ToolRegistry((
        Capability("market.snapshot", (("symbol", str), ("as_of_time", datetime)),
                   (AskIntent.MARKET_OVERVIEW, AskIntent.EVIDENCE_LOOKUP,
                    AskIntent.ENTITY_MOVE_EXPLANATION), read_market, QAMarketFacts),
        Capability("evidence.retrieve", (("symbol", str), ("as_of_time", datetime)),
                   (AskIntent.MARKET_OVERVIEW, AskIntent.EVIDENCE_LOOKUP,
                    AskIntent.ENTITY_MOVE_EXPLANATION), read_evidence, tuple),
    ))
    return registry, calls


def test_no_evidence_cannot_become_causal_explanation():
    registry, calls = market_registry()
    service = AskService(securities=lambda _: (security(),), registry=registry,
                         clock=lambda: NOW)
    result = service.ask(AskRequest("为什么贵州茅台上涨？", NOW,
                                    entity_hints=("600519.SH",)))
    assert result.status == "PARTIAL"
    assert "ATTRIBUTION_INSUFFICIENT" in result.reason_codes
    assert "没有足够有效" in result.answer
    assert result.evidence_refs == ()
    assert calls == ["market", "evidence"]
    assert result.trace.invocations[0].capability == "market.snapshot"
    assert result.trace.plan.plan_hash
    assert result.trace.as_of_time == NOW
    assert AskTrace.from_dict(result.trace.to_dict()) == result.trace
    assert AskResponse.from_dict(result.to_dict()) == result


def test_evidence_failure_degrades_with_market_fact_and_safe_trace():
    registry, _ = market_registry(evidence=RuntimeError("Authorization: secret"))
    result = AskService(securities=lambda _: (security(),), registry=registry,
                        clock=lambda: NOW).ask(AskRequest(
                            "最近有哪些消息？", NOW, entity_hints=("600519.SH",)))
    assert result.status == "PARTIAL"
    assert result.reason_codes == ("TOOL_FAILED",)
    assert result.facts[0]["ref"] == "M1"
    assert "secret" not in str(result.trace.to_dict())
    assert result.trace.invocations[-1].status == "FAIL"


def test_future_market_fact_is_rejected_before_synthesis():
    registry, calls = market_registry(market=market_fact(available_at=NOW + timedelta(seconds=1)))
    result = AskService(securities=lambda _: (security(),), registry=registry,
                        clock=lambda: NOW).ask(AskRequest(
                            "最近表现？", NOW, entity_hints=("600519.SH",)))
    assert result.status == "FAIL"
    assert result.reason_codes == ("DATA_UNAVAILABLE",)
    assert result.facts == ()
    assert calls == ["market"]


def test_trace_keeps_raw_and_normalized_question_separate():
    registry, _ = market_registry()
    result = AskService(securities=lambda _: (security(),), registry=registry,
                        clock=lambda: NOW).ask(AskRequest(
                            "  最近\t表现？  ", NOW, entity_hints=("600519.SH",)))
    assert result.trace.raw_question == "  最近\t表现？  "
    assert result.trace.normalized_question == "最近 表现？"
    assert result.trace.duration_ms >= 0


def test_historical_and_unavailable_capabilities_fail_closed():
    service = AskService(securities=lambda _: (security(),), clock=lambda: NOW)
    history = service.ask(AskRequest("与去年历史比较", NOW,
                                     entity_hints=("600519.SH",)))
    knowledge = service.ask(AskRequest("查知识库", NOW,
                                       entity_hints=("600519.SH",)))
    assert history.reason_codes == ("HISTORICAL_DATA_UNAVAILABLE",)
    assert knowledge.reason_codes == ("TOOL_UNAVAILABLE",)
    assert history.trace.invocations == knowledge.trace.invocations == ()
    missing_market = service.ask(AskRequest("最近表现？", NOW,
                                            entity_hints=("600519.SH",)))
    assert missing_market.status == "FAIL"
    assert missing_market.reason_codes == ("TOOL_UNAVAILABLE",)
