"""Integrated local product startup and deterministic Demo acceptance."""

from __future__ import annotations

from contextlib import closing
import json
import socket
import zipfile

from fastapi.testclient import TestClient
import pytest

from quantos.api import create_app
from quantos.product import (
    ProductStartError,
    git_commit,
    local_data_notice,
    prepare_demo_workspace,
    resolve_workspace_assets,
    select_local_port,
    wait_for_health,
)
from quantos.release import build_release_bundle
from quantos.config import Settings
from quantos.replay.storage import ReplayCampaignRepository
from quantos.storage import SecurityMasterRepository


def _listener() -> socket.socket:
    value = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    value.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    value.bind(("127.0.0.1", 0))
    value.listen(1)
    return value


def test_port_discovery_skips_occupied_default_without_touching_owner():
    with closing(_listener()) as occupied:
        port = occupied.getsockname()[1]
        selected = select_local_port("127.0.0.1", port, explicit=False, scan_size=20)

        assert selected != port
        assert port < selected <= port + 20
        assert occupied.getsockname()[1] == port


def test_explicit_occupied_port_fails_closed():
    with closing(_listener()) as occupied:
        port = occupied.getsockname()[1]
        with pytest.raises(ProductStartError, match="PORT_UNAVAILABLE"):
            select_local_port("127.0.0.1", port, explicit=True)


def test_missing_workspace_assets_has_actionable_build_instruction(tmp_path):
    with pytest.raises(ProductStartError, match="npm run build"):
        resolve_workspace_assets(tmp_path)


def test_health_gate_is_bounded_and_reports_safe_failure():
    calls = []

    def unavailable(*_args, **_kwargs):
        calls.append(1)
        raise OSError("offline")

    with pytest.raises(ProductStartError, match="STARTUP_HEALTH_TIMEOUT"):
        wait_for_health(
            "http://127.0.0.1:8010/v1/health", timeout=0.01,
            opener=unavailable, sleeper=lambda _value: None,
        )
    assert calls


def test_empty_local_workspace_has_actionable_first_run_notice(tmp_path):
    message = local_data_notice(Settings.from_project_root(tmp_path))

    assert "No local research dataset is available" in message
    assert "quantos start --demo" in message
    assert "quantos data bootstrap" in message


def test_demo_workspace_is_complete_offline_and_labeled(tmp_path, monkeypatch):
    for key in ("TUSHARE_TOKEN", "TAVILY_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("Demo mode initialized a live provider")
    for provider_path in (
        "quantos.commands.ask.TavilyWebSearchProvider",
        "quantos.commands.ask.DeepSeekLLMClient",
        "quantos.commands.data.TushareMarketCollector",
        "quantos.commands.data.BaoStockSecurityMasterCollector",
        "quantos.commands.replay.TushareMarketCollector",
        "quantos.cli.TushareMarketCollector",
    ):
        monkeypatch.setattr(provider_path, forbidden_provider)

    settings = prepare_demo_workspace(tmp_path)
    assert SecurityMasterRepository(settings).is_initialized()
    assert len(tuple(ReplayCampaignRepository(settings).root.glob("campaign_id=*"))) == 1

    client = TestClient(create_app(
        settings=settings,
        runtime_mode="DEMO",
        build_commit="demo-test",
    ))
    health = client.get("/v1/health").json()
    assert health["runtime_mode"] == "DEMO"
    assert health["data_label"] == "SYNTHETIC_FIXTURE"

    ask = client.post("/v1/ask", json={
        "symbol": "600519.SH",
        "question": "最近一个交易日表现怎么样？",
        "as_of_time": "2026-09-18T18:00:00+08:00",
        "no_research": True,
    })
    assert ask.status_code == 200
    assert ask.json()["facts"]
    assert client.get(f"/v1/traces/{ask.json()['trace_id']}").status_code == 200

    analytics = client.post("/v1/analytics/query", json={
        "metrics": ["close"],
        "dimensions": ["trading_date"],
        "entities": ["600519.SH"],
        "time_range": {"start": "2026-08-20", "end": "2026-09-18"},
        "filters": [],
        "sort": [{"field": "trading_date", "direction": "ASC"}],
        "limit": 20,
        "as_of_time": "2026-09-19T12:00:00+08:00",
    })
    assert analytics.status_code == 200
    assert len(analytics.json()["rows"]) == 2
    assert client.get("/v1/reports").json()["pagination"]["total"] == 1
    assert client.get("/v1/replay/campaigns").json()["pagination"]["total"] == 1


def test_integrated_app_serves_spa_and_keeps_api_boundary(tmp_path):
    workspace = tmp_path / "web" / "dist"
    (workspace / "assets").mkdir(parents=True)
    (workspace / "index.html").write_text("<main>QuantOS Workspace</main>", encoding="utf-8")
    (workspace / "assets" / "app.js").write_text("export {};", encoding="utf-8")
    client = TestClient(create_app(
        settings=Settings.from_project_root(tmp_path), workspace_dir=workspace,
    ))

    assert client.get("/").text == "<main>QuantOS Workspace</main>"
    assert client.get("/analytics").text == "<main>QuantOS Workspace</main>"
    assert client.get("/assets/app.js").text == "export {};"
    assert client.get("/v1/health").json()["service"] == "quantos-research-api"
    assert client.get("/v1/unknown").status_code == 404


def test_release_bundle_is_allowlisted_reproducible_and_secret_free(tmp_path):
    repository = tmp_path / "repository"
    for path, content in {
        "README.md": "# QuantOS\n",
        "pyproject.toml": '[project]\nname="quantos"\nversion="0.1.0"\n',
        "src/quantos/__init__.py": '__version__ = "0.1.0"\n',
        "web/package.json": '{"name":"workspace"}\n',
        "web/package-lock.json": '{}\n',
        "web/dist/index.html": "<main>QuantOS</main>",
        "web/dist/assets/app.js": "export {};",
        "data/market.parquet": "private runtime data",
        ".env": "TOKEN=secret",
        "研发日报/private.md": "private",
    }.items():
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    first = build_release_bundle(
        repository, tmp_path / "out-one", git_commit="a" * 40,
        build_timestamp="2026-09-21T00:00:00Z",
    )
    second = build_release_bundle(
        repository, tmp_path / "out-two", git_commit="a" * 40,
        build_timestamp="2026-09-21T00:00:00Z",
    )

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("release-manifest.json"))
    assert "web/dist/index.html" in names
    assert "data/market.parquet" not in names
    assert ".env" not in names
    assert not any("研发日报" in name for name in names)
    assert manifest["git_commit"] == "a" * 40
    assert manifest["workspace"] == "web/dist"


def test_release_bundle_commit_is_visible_without_git_metadata(tmp_path):
    (tmp_path / "release-manifest.json").write_text(json.dumps({
        "git_commit": "a" * 40,
    }), encoding="utf-8")

    assert git_commit(tmp_path) == "a" * 40


def test_local_runtime_metadata_does_not_claim_demo_data(tmp_path):
    client = TestClient(create_app(
        settings=prepare_demo_workspace(tmp_path),
        runtime_mode="LOCAL",
        build_commit="abc123",
    ))

    health = client.get("/v1/health").json()
    assert health["runtime_mode"] == "LOCAL"
    assert health["data_label"] == "LOCAL_PERSISTED_DATA"
    assert health["build_commit"] == "abc123"
