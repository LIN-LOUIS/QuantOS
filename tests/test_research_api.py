"""Read-only Research API and semantic transport acceptance."""

from dataclasses import replace
from datetime import datetime
import json

from fastapi.testclient import TestClient
import pytest

from quantos.api import create_app
from quantos.api.resources import ResearchResources, ResourceConflict, _availability
from quantos.commands import ask as ask_command
from quantos.cli import main
from quantos.config import MARKET_TIMEZONE, Settings
from quantos.replay import HistoricalIdentityAuthority, ReplayMode
from quantos.replay.identity import HistoricalIdentityArtifactRepository
from quantos.semantic import SemanticService
from quantos.storage import DailyReportRepository
from tests.test_replay_engine import (
    DAY1, DAY2, Supplemental, complete_outcome, foundation, service,
)
from tests.test_reporting import build_report


NOW = datetime(2026, 8, 5, 20, tzinfo=MARKET_TIMEZONE)


def analytics_payload(**overrides):
    value = {
        "metrics": ["close"], "dimensions": ["trading_date"],
        "entities": ["600519.SH"],
        "time_range": {"start": DAY1.isoformat(), "end": DAY2.isoformat()},
        "filters": [],
        "sort": [{"field": "trading_date", "direction": "ASC"}],
        "limit": 20, "as_of_time": NOW.isoformat(),
    }
    value.update(overrides)
    return value


def api_foundation(tmp_path):
    settings, dataset = foundation(tmp_path)
    source = __import__(
        "quantos.storage", fromlist=["SecurityMasterRepository"]
    ).SecurityMasterRepository(settings).list_snapshots()[0]
    authority = HistoricalIdentityAuthority().derive(
        source, generated_at=NOW,
    )
    HistoricalIdentityArtifactRepository(settings).save(authority)
    campaign = service(settings, Supplemental({
        DAY1: complete_outcome(), DAY2: complete_outcome(),
    })).run(
        dataset_id=dataset.dataset_id, symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY2,
        replay_mode=ReplayMode.RETROSPECTIVE_RECONSTRUCTED,
        identity_authority_id=authority.artifact_id,
    )
    report, _, _ = build_report(tmp_path / "report-input", no_llm=True)
    DailyReportRepository(settings).write(report)
    return settings, campaign, report


def test_health_and_status_are_distinct_read_only_and_path_safe(tmp_path):
    settings, _campaign, _report = api_foundation(tmp_path)
    client = TestClient(create_app(settings=settings, clock=lambda: NOW))

    health = client.get("/v1/health")
    status = client.get("/v1/status")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok", "service": "quantos-research-api", "api_version": "v1",
        "runtime_mode": "LOCAL", "data_label": "LOCAL_PERSISTED_DATA",
        "quantos_version": "0.3.0", "build_commit": "UNKNOWN",
    }
    assert status.status_code == 200
    payload = status.json()
    assert set(payload) == {
        "providers", "data_availability", "research_availability",
        "replay_availability",
    }
    encoded = json.dumps(payload).lower()
    assert "project_root" not in encoded and "scheduler_runtime" not in encoded
    assert "token" not in encoded and str(tmp_path).lower() not in encoded


def test_ask_reuses_canonical_service_persists_trace_and_never_calls_provider(tmp_path):
    settings, _campaign, _report = api_foundation(tmp_path)
    client = TestClient(create_app(settings=settings, clock=lambda: NOW))

    response = client.post("/v1/ask", json={
        "symbol": "600519.SH", "question": "最近一个交易日表现怎么样？",
        "as_of_time": "2026-08-04T18:00:00+08:00", "no_research": True,
    })

    assert response.status_code == 200
    value = response.json()
    assert value["status"] == "PASS"
    assert value["facts"][0]["trade_date"] == "2026-08-04"
    assert value["reason_codes"] == ["OK"]
    assert value["trace_id"]
    trace = client.get(f"/v1/traces/{value['trace_id']}")
    assert trace.status_code == 200
    assert trace.json()["trace_id"] == value["trace_id"]
    assert trace.json()["query_plan"]["as_of_time"] == "2026-08-04T18:00:00+08:00"


def test_offline_ask_uses_injected_settings_and_clock_without_network_clients(
    tmp_path, monkeypatch,
):
    settings, _campaign, _report = api_foundation(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("network client constructed")

    monkeypatch.setattr(ask_command, "TavilyWebSearchProvider", forbidden)
    monkeypatch.setattr(ask_command, "DeepSeekLLMClient", forbidden)
    session = ask_command.make_session(
        "600519.SH", None, offline=True, no_research=True,
        settings=settings, clock=lambda: NOW,
    )

    result = session.ask("最近一个交易日表现怎么样？")

    assert result.status == "PASS"
    assert result.as_of_time == NOW
    assert result.facts[0]["trade_date"] == DAY2.isoformat()


def test_trace_rejects_invalid_id_and_returns_404_for_missing(tmp_path):
    settings, _campaign, _report = api_foundation(tmp_path)
    client = TestClient(create_app(settings=settings, clock=lambda: NOW))

    invalid = client.get("/v1/traces/not-a-trace")
    missing = client.get(f"/v1/traces/{'f' * 32}")

    assert invalid.status_code == 400
    assert invalid.json()["code"] == "INVALID_TRACE_ID"
    assert missing.status_code == 404
    assert missing.json()["code"] == "TRACE_NOT_FOUND"


def test_resource_ids_reject_traversal_encoded_separators_and_oversize(tmp_path):
    settings, _campaign, _report = api_foundation(tmp_path)
    client = TestClient(create_app(settings=settings, clock=lambda: NOW))

    for path in (
        "/v1/traces/%2E%2E%5Csecret", "/v1/reports/%2E%2E%5Csecret",
        "/v1/replay/campaigns/%2E%2E%5Csecret", "/v1/traces/" + "a" * 2048,
    ):
        response = client.get(path)
        assert response.status_code in {400, 404}


def test_report_list_detail_filters_and_bounded_pagination(tmp_path):
    settings, _campaign, report = api_foundation(tmp_path)
    client = TestClient(create_app(settings=settings, clock=lambda: NOW))

    listed = client.get(
        "/v1/reports", params={"type": "daily", "date": report.trade_date.isoformat(),
                                "limit": 1, "offset": 0},
    )
    detail = client.get(f"/v1/reports/{report.report_id}")

    assert listed.status_code == 200
    assert listed.json()["items"][0]["report_id"] == report.report_id
    assert listed.json()["pagination"] == {"limit": 1, "offset": 0, "total": 1}
    assert detail.status_code == 200
    assert detail.json()["report_id"] == report.report_id
    assert client.get(f"/v1/reports/{'f' * 64}").status_code == 404
    assert client.get(
        "/v1/reports", params={"symbol": report.candidate_briefs[0].symbol},
    ).json()["pagination"]["total"] == 1
    assert client.get(
        "/v1/reports", params={"symbol": "999999.SH"},
    ).json()["pagination"]["total"] == 0


def test_report_discovery_skips_corruption_and_conflicting_ids_fail_closed(
    tmp_path, monkeypatch,
):
    settings, _campaign, report = api_foundation(tmp_path)
    broken = settings.report_dir / "trade_date=2026-08-05" / "broken.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("not-json", encoding="utf-8")
    resources = ResearchResources(settings)
    assert resources.list_reports(
        report_type="daily", trade_date=None, symbol=None, limit=20, offset=0,
    )["pagination"]["total"] == 1

    duplicate = broken.with_name("duplicate.json")
    duplicate.write_text("{}", encoding="utf-8")
    original_read = resources.reports.read_json
    conflicting = replace(report, market_overview={"conflict": True})

    def read(path):
        if path == duplicate:
            return conflicting
        return original_read(path)

    monkeypatch.setattr(resources.reports, "read_json", read)
    with pytest.raises(ResourceConflict, match="REPORT_ID_CONFLICT"):
        resources.list_reports(
            report_type="daily", trade_date=None, symbol=None, limit=20, offset=0,
        )


def test_replay_endpoints_expose_retrospective_semantics_and_failures(tmp_path):
    settings, campaign, _report = api_foundation(tmp_path)
    client = TestClient(create_app(settings=settings, clock=lambda: NOW))

    result = client.get(f"/v1/replay/campaigns/{campaign.campaign_id}")
    failures = client.get(f"/v1/replay/campaigns/{campaign.campaign_id}/failures")

    assert result.status_code == 200
    assert result.json()["replay_mode"] == "RETROSPECTIVE_RECONSTRUCTED"
    assert result.json()["identity_temporal_semantics"] == "RETROSPECTIVE_EFFECTIVE_TRUTH"
    assert all(item["identity_temporal_semantics"] == "RETROSPECTIVE_EFFECTIVE_TRUTH"
               for item in result.json()["results"])
    assert failures.status_code == 200
    assert failures.json()["replay_mode"] == "RETROSPECTIVE_RECONSTRUCTED"
    assert failures.json()["identity_temporal_semantics"] == "RETROSPECTIVE_EFFECTIVE_TRUTH"


def test_replay_campaign_discovery_is_bounded_filterable_and_temporally_explicit(tmp_path):
    settings, campaign, _report = api_foundation(tmp_path)
    broken = settings.data_root / "derived" / "replay_campaigns" / "campaign_id=bad"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("not-json", encoding="utf-8")
    client = TestClient(create_app(settings=settings, clock=lambda: NOW))

    response = client.get(
        "/v1/replay/campaigns",
        params={"mode": "RETROSPECTIVE_RECONSTRUCTED", "limit": 1, "offset": 0},
    )
    empty = client.get(
        "/v1/replay/campaigns", params={"mode": "STRICT_OPERATIONAL_PIT"},
    )

    assert response.status_code == 200
    assert response.json()["pagination"] == {"limit": 1, "offset": 0, "total": 1}
    assert response.json()["items"] == [{
        "campaign_id": campaign.campaign_id,
        "created_at": campaign.created_at.isoformat(),
        "replay_mode": "RETROSPECTIVE_RECONSTRUCTED",
        "identity_temporal_semantics": "RETROSPECTIVE_EFFECTIVE_TRUTH",
        "universe": campaign.universe.to_dict(),
        "summary": campaign.summary.to_dict(),
    }]
    assert empty.status_code == 200
    assert empty.json()["pagination"]["total"] == 0


def test_replay_endpoint_exposes_strict_operational_semantics(tmp_path):
    settings, dataset = foundation(tmp_path)
    campaign = service(settings, Supplemental({
        DAY1: complete_outcome(), DAY2: complete_outcome(),
    })).run(
        dataset_id=dataset.dataset_id, symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY2,
    )
    client = TestClient(create_app(settings=settings, clock=lambda: NOW))

    response = client.get(f"/v1/replay/campaigns/{campaign.campaign_id}")

    assert response.status_code == 200
    assert response.json()["replay_mode"] == "STRICT_OPERATIONAL_PIT"
    assert response.json()["identity_temporal_semantics"] == "OBSERVED_KNOWLEDGE"


def test_analytics_endpoint_returns_typed_result_provenance_insight_and_chart(tmp_path):
    settings, _campaign, _report = api_foundation(tmp_path)
    client = TestClient(create_app(settings=settings, clock=lambda: NOW))

    response = client.post("/v1/analytics/query", json=analytics_payload())

    assert response.status_code == 200
    value = response.json()
    assert value["status"] == "PASS"
    assert len(value["rows"]) == 2
    assert value["provenance"]["providers"] == ["tushare"]
    assert value["chart"]["chart_type"] == "line"
    assert value["insight"]["metrics"] == ["close"]
    assert value["availability"]["market_data"]["status"] == "READY"
    assert value["plan_hash"] and value["query_id"] and value["trace_id"]


def test_analytics_schema_discovers_closed_registries_and_bounded_limits(tmp_path):
    settings = Settings.from_project_root(tmp_path)
    client = TestClient(create_app(settings=settings, clock=lambda: NOW))

    response = client.get("/v1/analytics/schema")

    assert response.status_code == 200
    value = response.json()
    assert [item["metric_id"] for item in value["metrics"]] == [
        "close", "high", "low", "open", "price_change", "return", "volume",
    ]
    assert [item["dimension_id"] for item in value["dimensions"]] == [
        "market", "provider", "security", "trading_date",
    ]
    assert value["query_limits"] == {
        "max_metrics": 5, "max_dimensions": 4, "max_entities": 10,
        "max_filters": 10, "max_sorts": 5, "max_rows": 1000,
        "max_date_range_days": 366,
    }
    assert value["metric_registry_version"] == "market-metrics:v1"
    assert value["dimension_registry_version"] == "market-dimensions:v1"
    encoded = json.dumps(value).lower()
    assert "sql" not in encoded and "table_name" not in encoded


def test_analytics_rejects_unknown_or_control_plane_fields_without_execution(tmp_path):
    settings, _campaign, _report = api_foundation(tmp_path)
    client = TestClient(create_app(settings=settings, clock=lambda: NOW))

    unknown = client.post("/v1/analytics/query", json=analytics_payload(
        metrics=["unknown_metric"],
    ))
    injected = client.post("/v1/analytics/query", json={
        **analytics_payload(), "sql": "SELECT * FROM secrets",
    })

    assert unknown.status_code == 422
    assert unknown.json()["code"] == "UNKNOWN_METRIC"
    assert injected.status_code == 422
    assert injected.json()["code"] == "VALIDATION_ERROR"
    assert "SELECT" not in json.dumps(injected.json())


@pytest.mark.parametrize("field", ["sql", "python", "shell", "table", "join"])
def test_analytics_rejects_all_control_plane_fields(tmp_path, field):
    settings, _campaign, _report = api_foundation(tmp_path)
    client = TestClient(create_app(settings=settings, clock=lambda: NOW))

    response = client.post("/v1/analytics/query", json={
        **analytics_payload(), field: "sensitive-value",
    })

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "sensitive-value" not in json.dumps(response.json())


def test_api_preserves_missing_versus_available_zero_rows(tmp_path):
    settings = Settings.from_project_root(tmp_path)

    class Empty:
        def read_by_symbol(self, *_args, **_kwargs):
            return []

    unavailable = SemanticService(
        settings=settings, market_repository=Empty(), data_available=lambda: False,
        clock=lambda: NOW,
    )
    available = SemanticService(
        settings=settings, market_repository=Empty(), data_available=lambda: True,
        clock=lambda: NOW,
    )
    first = TestClient(create_app(
        settings=settings, semantic_service=unavailable, clock=lambda: NOW,
    )).post("/v1/analytics/query", json=analytics_payload()).json()
    second = TestClient(create_app(
        settings=settings, semantic_service=available, clock=lambda: NOW,
    )).post("/v1/analytics/query", json=analytics_payload()).json()

    assert first["availability"]["market_data"] == {
        "status": "UNAVAILABLE", "record_count": None,
        "reason_code": "MARKET_DATA_UNAVAILABLE",
    }
    assert second["availability"]["market_data"] == {
        "status": "READY", "record_count": 0, "reason_code": None,
    }


def test_errors_are_secret_safe_and_audit_contains_only_request_metadata(tmp_path):
    settings = Settings.from_project_root(tmp_path)
    audit = []

    def broken_factory(*_args, **_kwargs):
        raise RuntimeError("token=top-secret /home/private/provider.json")

    client = TestClient(create_app(
        settings=settings, ask_session_factory=broken_factory,
        audit_sink=audit.append, clock=lambda: NOW,
    ), raise_server_exceptions=False)
    response = client.post("/v1/ask", json={
        "symbol": "600519.SH", "question": "表现？", "no_research": True,
    })

    assert response.status_code == 500
    encoded = json.dumps(response.json()).lower()
    assert "top-secret" not in encoded and "/home/" not in encoded
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert audit[-1].endpoint == "/v1/ask"
    assert audit[-1].status_code == 500
    assert audit[-1].correlation_id == response.headers["x-correlation-id"]
    assert not hasattr(audit[-1], "payload")

    missing = client.get("/top-secret-path")
    assert missing.status_code == 404
    assert audit[-1].endpoint == "UNMATCHED"


def test_openapi_contains_eleven_versioned_paths_and_typed_query_schema(tmp_path):
    app = create_app(settings=Settings.from_project_root(tmp_path), clock=lambda: NOW)
    schema = app.openapi()
    expected = {
        "/v1/health", "/v1/status", "/v1/ask", "/v1/traces/{trace_id}",
        "/v1/reports", "/v1/reports/{report_id}",
        "/v1/replay/campaigns", "/v1/analytics/schema",
        "/v1/replay/campaigns/{campaign_id}",
        "/v1/replay/campaigns/{campaign_id}/failures", "/v1/analytics/query",
    }

    assert set(schema["paths"]) == expected
    request_schema = schema["paths"]["/v1/analytics/query"]["post"]["requestBody"]
    assert request_schema["content"]["application/json"]["schema"]["$ref"].endswith(
        "/SemanticQueryBody"
    )
    analytics_response = schema["paths"]["/v1/analytics/query"]["post"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    ask_response = schema["paths"]["/v1/ask"]["post"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert analytics_response["$ref"].endswith("/SemanticResultResponse")
    assert ask_response["$ref"].endswith("/AskResponseBody")
    assert not any("sql" in json.dumps(item).lower()
                   for item in schema["components"]["schemas"].values())


def test_app_construction_is_read_only_for_fresh_project_root(tmp_path):
    settings = Settings.from_project_root(tmp_path)

    app = create_app(settings=settings, clock=lambda: NOW)

    assert app.openapi()["info"]["title"] == "QuantOS Research API"
    assert not settings.data_root.exists()


def test_availability_mapping_preserves_non_ready_states():
    assert _availability("PARTIAL") == "PARTIAL"
    assert _availability("DEGRADED") == "DEGRADED"
    assert _availability("DISABLED") == "DISABLED"
    assert _availability("UNCONFIGURED") == "UNCONFIGURED"
    assert _availability("UNKNOWN_NEW_STATE") == "UNKNOWN"


def test_serve_defaults_to_loopback_and_rejects_wildcard(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs)))

    assert main(["serve", "--project-root", str(tmp_path)]) == 0
    assert calls[0][1] == {"host": "127.0.0.1", "port": 8000}
    assert not Settings.from_project_root(tmp_path).data_root.exists()
    with pytest.raises(SystemExit):
        main(["serve", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        main(["serve", "--port", "65536"])
