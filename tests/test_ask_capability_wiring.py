"""Phase 6A.2 bounded capability wiring and persistent audit tests."""

from datetime import date, datetime, timedelta

import pytest

from quantos.ask import (
    AskIntent,
    AskRequest,
    AskService,
    AttributionLookupResult,
    Capability,
    CapabilityAvailability,
    KnowledgeQueryResult,
    ReportLookupResult,
    SystemStatusResult,
    ToolRegistry,
    ToolRejected,
)
from quantos.ask.adapters import LocalAskCapabilities
from quantos.config import MARKET_TIMEZONE, KnowledgeIntegrationSettings, Settings
from quantos.qa import QAMarketFacts, QAResearchEvidence
from quantos.schemas import SecurityMaster
from quantos.storage import (
    AskTraceRepository, AskTraceStorageError, DailyReportRepository,
)
from tests.test_reporting import build_report


NOW = datetime(2026, 9, 18, 19, tzinfo=MARKET_TIMEZONE)


def _security():
    return SecurityMaster(
        "600519.SH", "贵州茅台", "SHSE", date(2001, 8, 27),
        NOW - timedelta(days=1), source="tushare",
    )


def _market():
    return QAMarketFacts(
        "M1", "2026-09-17", "1266.98", "1258", "0.71", 100, "1000",
        "tushare", "daily:1", NOW - timedelta(hours=1),
    )


def test_registry_availability_prevents_unavailable_handler_execution():
    calls = []
    registry = ToolRegistry((Capability(
        "knowledge.query", (("query", str), ("symbol", str), ("as_of_time", datetime)),
        (AskIntent.KNOWLEDGE_LOOKUP,), lambda args: calls.append(args), tuple,
        availability=CapabilityAvailability.UNAVAILABLE,
    ),))

    assert registry.availability("knowledge.query") is CapabilityAvailability.UNAVAILABLE
    assert registry.availability("missing.tool") is CapabilityAvailability.UNAVAILABLE
    with pytest.raises(ToolRejected, match="TOOL_UNAVAILABLE"):
        registry.execute("knowledge.query", AskIntent.KNOWLEDGE_LOOKUP, {
            "query": "知识", "symbol": "600519.SH", "as_of_time": NOW,
        })
    assert calls == []


def test_semantic_plan_hash_ignores_trace_identity_but_changes_with_cutoff():
    registry = ToolRegistry((Capability(
        "market.snapshot", (("symbol", str), ("as_of_time", datetime)),
        (AskIntent.MARKET_OVERVIEW,), lambda _: _market(), QAMarketFacts,
    ),))
    service = AskService(securities=lambda _: (_security(),), registry=registry,
                         clock=lambda: NOW)
    request = AskRequest("最近表现？", NOW, entity_hints=("600519.SH",), no_research=True)

    one = service.plan(request)
    two = service.plan(request)
    later = service.plan(AskRequest(
        "最近表现？", NOW + timedelta(minutes=1), entity_hints=("600519.SH",),
        no_research=True,
    ))

    assert one.plan_hash == two.plan_hash
    assert one.plan_hash != later.plan_hash
    assert one.capability_availability == (("market.snapshot", "READY"),)


def test_entity_move_executes_fixed_market_evidence_attribution_plan():
    calls = []
    evidence = (QAResearchEvidence(
        "E1", "公告", "摘要", "https://example.test/e1", NOW - timedelta(hours=2),
        "tavily", False,
    ),)
    registry = ToolRegistry((
        Capability(
            "market.snapshot", (("symbol", str), ("as_of_time", datetime)),
            (AskIntent.ENTITY_MOVE_EXPLANATION,),
            lambda args: calls.append("market") or _market(), QAMarketFacts,
        ),
        Capability(
            "evidence.retrieve", (("symbol", str), ("as_of_time", datetime)),
            (AskIntent.ENTITY_MOVE_EXPLANATION,),
            lambda args: calls.append("evidence") or evidence, tuple,
        ),
        Capability(
            "attribution.lookup", (("symbol", str), ("as_of_time", datetime)),
            (AskIntent.ENTITY_MOVE_EXPLANATION,),
            lambda args: calls.append("attribution") or AttributionLookupResult(
                evidence_refs=("E1",), eligible_refs=("E1",), strict_refs=("E1",),
                causal_allowed=True, report_refs=("R1",),
            ), AttributionLookupResult,
        ),
    ))

    result = AskService(securities=lambda _: (_security(),), registry=registry,
                        clock=lambda: NOW).ask(AskRequest(
                            "为什么贵州茅台上涨？", NOW,
                            entity_hints=("600519.SH",),
                        ))

    assert calls == ["market", "evidence", "attribution"]
    assert result.status == "PASS"
    assert result.reason_codes == ("OK",)
    assert result.report_refs == ("R1",)
    assert "既有 Attribution Policy" in result.answer
    assert tuple(item.capability for item in result.trace.invocations) == (
        "market.snapshot", "evidence.retrieve", "attribution.lookup",
    )


def test_non_strict_tavily_cannot_enable_causal_attribution():
    evidence = (QAResearchEvidence(
        "E1", "新闻", "摘要", "https://example.test/e1", NOW - timedelta(hours=1),
        "tavily", False,
    ),)
    registry = ToolRegistry((
        Capability("market.snapshot", (("symbol", str), ("as_of_time", datetime)),
                   (AskIntent.ENTITY_MOVE_EXPLANATION,), lambda _: _market(), QAMarketFacts),
        Capability("evidence.retrieve", (("symbol", str), ("as_of_time", datetime)),
                   (AskIntent.ENTITY_MOVE_EXPLANATION,), lambda _: evidence, tuple),
        Capability("attribution.lookup", (("symbol", str), ("as_of_time", datetime)),
                   (AskIntent.ENTITY_MOVE_EXPLANATION,), lambda _: AttributionLookupResult(
                       evidence_refs=("E1",), eligible_refs=("E1",), strict_refs=(),
                       causal_allowed=False, report_refs=("R1",),
                       limitation="NON_STRICT_TEMPORAL_EVIDENCE",
                   ), AttributionLookupResult),
    ))

    result = AskService(securities=lambda _: (_security(),), registry=registry,
                        clock=lambda: NOW).ask(AskRequest(
                            "为什么贵州茅台上涨？", NOW,
                            entity_hints=("600519.SH",),
                        ))

    assert result.status == "PARTIAL"
    assert "ATTRIBUTION_INSUFFICIENT" in result.reason_codes
    assert "NON_STRICT_TEMPORAL_EVIDENCE" in result.limitations
    assert "因果归因" in result.answer


def test_knowledge_report_and_status_results_are_grounded_and_referenced():
    cases = (
        ("查知识库里的贵州茅台", "knowledge.query", AskIntent.KNOWLEDGE_LOOKUP,
         KnowledgeQueryResult(("K1",), ({"ref": "K1", "title": "制度文档"},)), "K1"),
        ("查已有报告", "report.lookup", AskIntent.REPORT_LOOKUP,
         ReportLookupResult(("R1",), ({"ref": "R1", "trade_date": "2026-09-17"},),
                            run_id="run-1"), "R1"),
        ("系统状态", "system.status", AskIntent.SYSTEM_STATUS,
         SystemStatusResult(({"capability": "market", "availability": "READY"},)), "market"),
    )
    for question, name, intent, output, expected in cases:
        schema = (("as_of_time", datetime),) if intent is AskIntent.SYSTEM_STATUS else (
            ("query", str), ("symbol", str), ("as_of_time", datetime),
        )
        registry = ToolRegistry((Capability(name, schema, (intent,), lambda _, o=output: o,
                                                  type(output)),))
        response = AskService(securities=lambda _: (_security(),), registry=registry,
                              clock=lambda: NOW).ask(AskRequest(
                                  question, NOW, entity_hints=("600519.SH",),
                              ))
        assert response.status == "PASS"
        assert expected in response.answer
        if intent is AskIntent.REPORT_LOOKUP:
            assert response.trace.run_id == "run-1"


def test_trace_repository_round_trip_request_lookup_recent_and_secret_rejection(tmp_path):
    registry = ToolRegistry((Capability(
        "market.snapshot", (("symbol", str), ("as_of_time", datetime)),
        (AskIntent.MARKET_OVERVIEW,), lambda _: _market(), QAMarketFacts,
    ),))
    repository = AskTraceRepository(tmp_path / "ask-traces")
    service = AskService(securities=lambda _: (_security(),), registry=registry,
                         trace_repository=repository, clock=lambda: NOW)

    response = service.ask(AskRequest(
        "最近表现？", NOW, entity_hints=("600519.SH",), no_research=True,
    ))

    assert repository.load(response.trace_id) == response.trace
    assert repository.find_by_request_id(response.request_id) == (response.trace,)
    assert repository.recent(limit=1) == (response.trace,)
    persisted = next((tmp_path / "ask-traces").rglob("*.json")).read_text()
    assert "Authorization" not in persisted
    unsafe = response.trace.to_dict()
    unsafe["raw_question"] = "Authorization: Bearer secret"
    with pytest.raises(AskTraceStorageError, match="unsafe"):
        repository.save_record(unsafe)
    unsafe_id = response.trace.to_dict()
    unsafe_id["trace_id"] = "../../outside"
    with pytest.raises(AskTraceStorageError, match="invalid"):
        repository.save_record(unsafe_id)
    with pytest.raises(AskTraceStorageError, match="invalid"):
        repository.load("*")


def test_repeated_request_has_stable_plan_hash_and_distinct_persisted_traces(tmp_path):
    registry = ToolRegistry((Capability(
        "market.snapshot", (("symbol", str), ("as_of_time", datetime)),
        (AskIntent.MARKET_OVERVIEW,), lambda _: _market(), QAMarketFacts,
    ),))
    repository = AskTraceRepository(tmp_path / "ask-traces")
    service = AskService(securities=lambda _: (_security(),), registry=registry,
                         trace_repository=repository, clock=lambda: NOW)
    request = AskRequest("最近表现？", NOW, entity_hints=("600519.SH",), no_research=True)

    one, two = service.ask(request), service.ask(request)

    assert one.trace.plan.plan_hash == two.trace.plan.plan_hash
    assert one.trace_id != two.trace_id
    assert repository.find_by_request_id(one.request_id) == tuple(sorted(
        (one.trace, two.trace), key=lambda item: (item.created_at, item.trace_id),
    ))


def test_tool_failure_is_persisted_without_raw_exception_secret(tmp_path):
    registry = ToolRegistry((Capability(
        "market.snapshot", (("symbol", str), ("as_of_time", datetime)),
        (AskIntent.MARKET_OVERVIEW,),
        lambda _: (_ for _ in ()).throw(RuntimeError("token=top-secret")),
        QAMarketFacts,
    ),))
    repository = AskTraceRepository(tmp_path / "ask-traces")

    response = AskService(securities=lambda _: (_security(),), registry=registry,
                          trace_repository=repository, clock=lambda: NOW).ask(AskRequest(
                              "最近表现？", NOW, entity_hints=("600519.SH",),
                          ))

    assert response.status == "FAIL"
    loaded = repository.load(response.trace_id)
    assert loaded.reason_codes == ("DATA_UNAVAILABLE",)
    assert "top-secret" not in str(loaded.to_dict())


def test_local_report_and_attribution_adapters_reject_future_artifacts(tmp_path):
    settings = Settings.from_project_root(tmp_path / "project")
    report, _, _ = build_report(tmp_path / "fixtures", no_llm=True)
    DailyReportRepository(settings).write(report)
    local = LocalAskCapabilities(
        settings=settings, knowledge_settings=KnowledgeIntegrationSettings(),
    )
    args = {
        "query": "查已有报告", "symbol": report.candidate_briefs[0].symbol,
        "as_of_time": report.generated_at + timedelta(seconds=1),
    }

    found = local.report_lookup(args)
    attribution = local.attribution_lookup({
        "symbol": args["symbol"], "as_of_time": args["as_of_time"],
    })

    assert found.refs == (f"R:{report.report_id}",)
    assert found.facts[0]["trade_date"] == report.trade_date.isoformat()
    assert attribution.report_refs == found.refs
    assert attribution.causal_allowed is False
    assert attribution.limitation == "NON_STRICT_TEMPORAL_EVIDENCE"
    assert local.report_lookup({**args, "as_of_time": report.generated_at - timedelta(seconds=1)}).refs == ()


def test_local_status_is_sanitized_and_knowledge_is_explicitly_unavailable(tmp_path):
    settings = Settings.from_project_root(tmp_path / "project")
    local = LocalAskCapabilities(
        settings=settings, knowledge_settings=KnowledgeIntegrationSettings(),
    )

    status = local.system_status({"as_of_time": NOW})
    capability = {item["capability"]: item["availability"] for item in status.facts}

    assert capability["historical.query"] == "UNAVAILABLE"
    assert capability["knowledge.query"] == "DISABLED"
    assert str(settings.project_root) not in str(status.facts)
    assert local.availability("knowledge.query") is CapabilityAvailability.DISABLED


def test_empty_knowledge_result_is_controlled_partial():
    output = KnowledgeQueryResult((), ())
    registry = ToolRegistry((Capability(
        "knowledge.query", (("query", str), ("symbol", str), ("as_of_time", datetime)),
        (AskIntent.KNOWLEDGE_LOOKUP,), lambda _: output, KnowledgeQueryResult,
    ),))

    response = AskService(securities=lambda _: (_security(),), registry=registry,
                          clock=lambda: NOW).ask(AskRequest(
                              "查知识库", NOW, entity_hints=("600519.SH",),
                          ))

    assert response.status == "PARTIAL"
    assert response.reason_codes == ("NO_EVIDENCE",)


def test_future_evidence_is_not_exposed_by_ask_layer():
    future = (QAResearchEvidence(
        "E1", "未来消息", "摘要", "https://example.test/future",
        NOW + timedelta(seconds=1), "tavily", False,
    ),)
    registry = ToolRegistry((
        Capability("market.snapshot", (("symbol", str), ("as_of_time", datetime)),
                   (AskIntent.EVIDENCE_LOOKUP,), lambda _: _market(), QAMarketFacts),
        Capability("evidence.retrieve", (("symbol", str), ("as_of_time", datetime)),
                   (AskIntent.EVIDENCE_LOOKUP,), lambda _: future, tuple),
    ))

    response = AskService(securities=lambda _: (_security(),), registry=registry,
                          clock=lambda: NOW).ask(AskRequest(
                              "最近消息？", NOW, entity_hints=("600519.SH",),
                          ))

    assert response.evidence_refs == ()
    assert response.sources == ()
    assert response.trace.invocations[-1].result_count == 0


def test_attribution_failure_degrades_without_causal_claim():
    registry = ToolRegistry((
        Capability("market.snapshot", (("symbol", str), ("as_of_time", datetime)),
                   (AskIntent.ENTITY_MOVE_EXPLANATION,), lambda _: _market(), QAMarketFacts),
        Capability("evidence.retrieve", (("symbol", str), ("as_of_time", datetime)),
                   (AskIntent.ENTITY_MOVE_EXPLANATION,), lambda _: (), tuple),
        Capability("attribution.lookup", (("symbol", str), ("as_of_time", datetime)),
                   (AskIntent.ENTITY_MOVE_EXPLANATION,),
                   lambda _: (_ for _ in ()).throw(RuntimeError("Authorization: secret")),
                   AttributionLookupResult),
    ))

    response = AskService(securities=lambda _: (_security(),), registry=registry,
                          clock=lambda: NOW).ask(AskRequest(
                              "为什么贵州茅台上涨？", NOW,
                              entity_hints=("600519.SH",),
                          ))

    assert response.status == "PARTIAL"
    assert "TOOL_FAILED" in response.reason_codes
    assert "ATTRIBUTION_INSUFFICIENT" in response.reason_codes
    assert "secret" not in str(response.trace.to_dict())


def test_sector_without_existing_report_is_controlled_unavailable(tmp_path):
    settings = Settings.from_project_root(tmp_path / "project")
    local = LocalAskCapabilities(
        settings=settings, knowledge_settings=KnowledgeIntegrationSettings(),
    )
    registry = ToolRegistry(local.capabilities())

    response = AskService(securities=lambda _: (), registry=registry,
                          clock=lambda: NOW).ask(AskRequest("行业表现", NOW))

    assert response.status == "FAIL"
    assert response.reason_codes == ("TOOL_UNAVAILABLE",)
    assert response.trace.invocations == ()
