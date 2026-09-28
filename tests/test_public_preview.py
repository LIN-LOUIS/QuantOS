"""Public Preview deployment boundary acceptance."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import asyncio
import threading
import time

from fastapi.testclient import TestClient

from quantos.api import create_app
from quantos.api.protection import (
    DeploymentMode, PublicPreviewPolicy, PublicPreviewTimeoutMiddleware,
)
from quantos.cli import main
from quantos.config import MARKET_TIMEZONE, Settings
from quantos.product import prepare_demo_workspace


ASK = {
    "symbol": "600519.SH",
    "question": "最近一个交易日表现怎么样？",
    "as_of_time": "2026-09-18T18:00:00+08:00",
    "no_research": True,
}


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "web" / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<main>QuantOS Preview</main>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("export {};", encoding="utf-8")
    return root


def test_local_start_remains_loopback_and_preview_may_bind_externally(
    tmp_path, monkeypatch,
):
    workspace = _workspace(tmp_path)
    captured = []

    monkeypatch.setattr("quantos.product.resolve_workspace_assets", lambda _root: workspace)
    monkeypatch.setattr(
        "quantos.product.prepare_demo_workspace",
        lambda root: Settings.from_project_root(root),
    )
    monkeypatch.setattr(
        "quantos.product.launch_product",
        lambda **kwargs: captured.append(kwargs) or 0,
    )

    assert main(["start", "--demo", "--no-browser", "--project-root", str(tmp_path)]) == 0
    assert captured[-1]["host"] == "127.0.0.1"
    assert captured[-1]["deployment_mode"] is DeploymentMode.LOCAL

    assert main([
        "start", "--demo", "--public-preview", "--host", "0.0.0.0",
        "--no-browser", "--project-root", str(tmp_path),
    ]) == 0
    assert captured[-1]["host"] == "0.0.0.0"
    assert captured[-1]["deployment_mode"] is DeploymentMode.PUBLIC_PREVIEW
    assert not captured[-1]["settings"].project_root.exists()


def test_external_binding_and_non_demo_preview_fail_closed(tmp_path, capsys):
    assert main([
        "start", "--demo", "--host", "0.0.0.0", "--no-browser",
        "--project-root", str(tmp_path),
    ]) == 2
    assert "PUBLIC_PREVIEW_REQUIRED" in capsys.readouterr().err

    assert main([
        "start", "--public-preview", "--no-browser", "--project-root", str(tmp_path),
    ]) == 2
    assert "PUBLIC_PREVIEW_REQUIRES_DEMO" in capsys.readouterr().err


def test_preview_demo_does_not_initialize_providers_and_closes_api_docs(
    tmp_path, monkeypatch,
):
    for provider_path in (
        "quantos.commands.ask.TavilyWebSearchProvider",
        "quantos.commands.ask.DeepSeekLLMClient",
        "quantos.commands.data.TushareMarketCollector",
        "quantos.commands.data.BaoStockSecurityMasterCollector",
        "quantos.commands.replay.TushareMarketCollector",
    ):
        monkeypatch.setattr(
            provider_path,
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("public preview initialized a provider")
            ),
        )

    settings = prepare_demo_workspace(tmp_path)
    client = TestClient(create_app(
        settings=settings,
        runtime_mode="DEMO",
        deployment_mode=DeploymentMode.PUBLIC_PREVIEW,
        workspace_dir=_workspace(tmp_path),
    ))

    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    health = client.get("/v1/health")
    assert health.status_code == 200
    assert health.json()["runtime_mode"] == "DEMO"
    assert health.json()["deployment_mode"] == "public_preview"


def test_local_docs_remain_available(tmp_path):
    client = TestClient(create_app(settings=Settings.from_project_root(tmp_path)))
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/v1/health").json()["deployment_mode"] == "local"


def test_preview_rejects_oversized_request_body(tmp_path):
    app = create_app(
        settings=Settings.from_project_root(tmp_path),
        runtime_mode="DEMO",
        deployment_mode=DeploymentMode.PUBLIC_PREVIEW,
        preview_policy=PublicPreviewPolicy(max_body_bytes=32),
    )
    response = TestClient(app).post("/v1/ask", content=b"x" * 33)
    assert response.status_code == 413
    assert response.json()["code"] == "REQUEST_BODY_TOO_LARGE"

    received = iter((
        {"type": "http.request", "body": b"x" * 20, "more_body": True},
        {"type": "http.request", "body": b"y" * 20, "more_body": False},
    ))
    sent = []

    async def drain_body(_scope, receive, send):
        more = True
        while more:
            message = await receive()
            more = message.get("more_body", False)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    boundary = PublicPreviewTimeoutMiddleware(
        drain_body, timeout_seconds=1, max_body_bytes=32,
    )

    async def invoke_chunked():
        async def receive():
            return next(received)

        async def send(message):
            sent.append(message)

        await boundary({
            "type": "http", "asgi": {"version": "3.0"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/v1/ask", "raw_path": b"/v1/ask", "query_string": b"",
            "root_path": "", "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 10000), "server": ("testserver", 80),
        }, receive, send)

    asyncio.run(invoke_chunked())
    assert next(item for item in sent if item["type"] == "http.response.start")["status"] == 413
    assert b"REQUEST_BODY_TOO_LARGE" in b"".join(
        item.get("body", b"") for item in sent if item["type"] == "http.response.body"
    )


def test_preview_rate_limits_expensive_ask_endpoint(tmp_path):
    settings = prepare_demo_workspace(tmp_path)
    app = create_app(
        settings=settings,
        runtime_mode="DEMO",
        deployment_mode=DeploymentMode.PUBLIC_PREVIEW,
        preview_policy=PublicPreviewPolicy(ask_rate_limit=1),
    )
    client = TestClient(app)
    assert client.post("/v1/ask", json=ASK).status_code == 200
    limited = client.post("/v1/ask", json={**ASK, "question": "收盘价是多少？"})
    assert limited.status_code == 429
    assert limited.json()["code"] == "RATE_LIMITED"


def test_localized_preview_examples_are_supported_by_the_real_demo(tmp_path):
    settings = prepare_demo_workspace(tmp_path)
    client = TestClient(create_app(
        settings=settings,
        runtime_mode="DEMO",
        deployment_mode=DeploymentMode.PUBLIC_PREVIEW,
    ))
    questions = (
        "Show the latest market performance.",
        "What is the latest market closing price?",
        "Show the latest market price and trading volume.",
        "最近一个交易日表现怎么样？",
        "最近一个交易日的收盘价是多少？",
        "最近一个交易日的价格和成交量是多少？",
    )
    for question in questions:
        response = client.post("/v1/ask", json={**ASK, "question": question})
        assert response.status_code == 200
        assert response.json()["status"] in {"PASS", "PARTIAL"}


def test_preview_concurrency_limit_returns_429(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    app = create_app(
        settings=Settings.from_project_root(tmp_path),
        runtime_mode="DEMO",
        deployment_mode=DeploymentMode.PUBLIC_PREVIEW,
        preview_policy=PublicPreviewPolicy(max_concurrency=1, request_timeout_seconds=2),
    )

    @app.get("/v1/test-slow")
    def slow():
        entered.set()
        release.wait(timeout=1)
        return {"ok": True}

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.get, "/v1/test-slow")
        assert entered.wait(timeout=1)
        limited = client.get("/v1/status")
        release.set()
        assert pending.result(timeout=2).status_code == 200
    assert limited.status_code == 429
    assert limited.json()["code"] == "CONCURRENCY_LIMITED"


def test_preview_timeout_is_bounded(tmp_path):
    app = create_app(
        settings=Settings.from_project_root(tmp_path),
        runtime_mode="DEMO",
        deployment_mode=DeploymentMode.PUBLIC_PREVIEW,
        preview_policy=PublicPreviewPolicy(request_timeout_seconds=0.01),
    )

    @app.get("/v1/test-timeout")
    async def slow():
        await asyncio.sleep(0.1)
        return {"ok": True}

    started = time.monotonic()
    response = TestClient(app).get("/v1/test-timeout")
    assert time.monotonic() - started < 0.08
    assert response.status_code == 504
    assert response.json()["code"] == "REQUEST_TIMEOUT"


def test_preview_traces_remain_usable_and_are_count_bounded(tmp_path):
    settings = prepare_demo_workspace(tmp_path)
    client = TestClient(create_app(
        settings=settings,
        runtime_mode="DEMO",
        deployment_mode=DeploymentMode.PUBLIC_PREVIEW,
        preview_policy=PublicPreviewPolicy(trace_max_records=2),
    ))
    trace_ids = []
    for index in range(3):
        response = client.post(
            "/v1/ask", json={**ASK, "question": f"演示问题 {index}"},
        )
        assert response.status_code == 200
        trace_ids.append(response.json()["trace_id"])

    assert client.get(f"/v1/traces/{trace_ids[0]}").status_code == 404
    assert client.get(f"/v1/traces/{trace_ids[-1]}").status_code == 200
    paths = tuple((settings.data_root / "derived" / "ask_traces").glob("created_date=*/*.json"))
    assert len(paths) == 2


def test_preview_trace_ttl_expires_records(tmp_path):
    now = datetime(2026, 9, 19, 12, tzinfo=MARKET_TIMEZONE)
    current = [now]
    settings = prepare_demo_workspace(tmp_path)
    client = TestClient(create_app(
        settings=settings,
        runtime_mode="DEMO",
        deployment_mode=DeploymentMode.PUBLIC_PREVIEW,
        preview_policy=PublicPreviewPolicy(trace_ttl_seconds=1),
        clock=lambda: current[0],
    ))
    result = client.post("/v1/ask", json={**ASK, "as_of_time": None})
    assert result.status_code == 200
    trace_id = result.json()["trace_id"]
    current[0] += timedelta(seconds=2)
    assert client.get(f"/v1/traces/{trace_id}").status_code == 404


def test_preview_spa_fallback_and_health_remain_available(tmp_path):
    workspace = _workspace(tmp_path)
    client = TestClient(create_app(
        settings=Settings.from_project_root(tmp_path),
        runtime_mode="DEMO",
        deployment_mode=DeploymentMode.PUBLIC_PREVIEW,
        workspace_dir=workspace,
    ))
    assert client.get("/analytics").text == "<main>QuantOS Preview</main>"
    assert client.get("/v1/health").json()["status"] == "ok"
    assert client.get("/v1/missing").status_code == 404


def test_container_contract_builds_assets_and_runs_non_root():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    ignored = (root / ".dockerignore").read_text(encoding="utf-8")

    assert "npm ci" in dockerfile and "npm run build" in dockerfile
    assert "COPY --from=frontend" in dockerfile
    assert "USER quantos" in dockerfile
    assert "--public-preview" in dockerfile and "--demo" in dockerfile
    assert "HEALTHCHECK" in dockerfile and "/v1/health" in dockerfile
    for forbidden in (".env", "data", "*.parquet", "*.duckdb", "node_modules"):
        assert forbidden in ignored
