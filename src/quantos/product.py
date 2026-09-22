"""Local-only integrated product startup and deterministic Demo foundation."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
import json
import socket
import subprocess
import threading
import time as wall_time
from pathlib import Path
from typing import Callable, Literal
from urllib.request import urlopen
import webbrowser

from quantos.collectors import RawMarketRecord
from quantos.config import MARKET_TIMEZONE, Settings
from quantos.replay import HistoricalDatasetManifest, ReplayCampaignService, ReplayEngine
from quantos.replay.service import ReplaySupplementalOutcome
from quantos.replay.storage import HistoricalDatasetRepository
from quantos.schemas import (
    BootstrapManifest,
    CandidateBrief,
    DailyIntelligenceReport,
    EvidenceStats,
    MarketBar,
    SecurityMaster,
    SynthesisBrief,
)
from quantos.storage import (
    BootstrapManifestRepository,
    DailyReportRepository,
    MarketDataRepository,
    SecurityMasterRepository,
)


DEMO_NOW = datetime(2026, 9, 19, 12, tzinfo=MARKET_TIMEZONE)
DEMO_DATES = (date(2026, 8, 20), date(2026, 9, 18))
RuntimeMode = Literal["LOCAL", "DEMO"]


class ProductStartError(RuntimeError):
    """Safe, user-facing product startup failure."""


def select_local_port(
    host: str, preferred: int, *, explicit: bool, scan_size: int = 20,
) -> int:
    """Select a bounded local port without interacting with its current owner."""

    candidates = (preferred,) if explicit else range(preferred, min(65_535, preferred + scan_size) + 1)
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    if explicit:
        raise ProductStartError(f"PORT_UNAVAILABLE: {host}:{preferred} is already in use")
    raise ProductStartError(
        f"NO_AVAILABLE_PORT: no free local port in {preferred}-{preferred + scan_size}"
    )


def resolve_workspace_assets(project_root: Path) -> Path:
    """Find a built Workspace in a source checkout or installed package."""

    candidates = (
        Path(project_root).resolve() / "web" / "dist",
        Path(__file__).resolve().parent / "workspace",
    )
    for candidate in candidates:
        if (candidate / "index.html").is_file() and (candidate / "assets").is_dir():
            return candidate
    raise ProductStartError(
        "WORKSPACE_ASSETS_MISSING: Workspace assets are not built. "
        "Run: cd web && npm ci && npm run build"
    )


def wait_for_health(
    url: str, *, timeout: float = 10.0,
    opener: Callable[..., object] = urlopen,
    sleeper: Callable[[float], None] = wall_time.sleep,
) -> None:
    """Wait for the local API with a bounded monotonic deadline."""

    deadline = wall_time.monotonic() + timeout
    while wall_time.monotonic() < deadline:
        try:
            response = opener(url, timeout=min(1.0, timeout))
            try:
                payload = json.loads(response.read().decode("utf-8"))
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if payload.get("status") == "ok":
                return
        except Exception:
            pass
        sleeper(0.05)
    raise ProductStartError("STARTUP_HEALTH_TIMEOUT: Research API did not become ready")


def local_data_notice(settings: Settings) -> str | None:
    """Return an actionable first-run message without network or writes."""

    security = SecurityMasterRepository(settings).is_initialized()
    try:
        market = BootstrapManifestRepository(settings).latest_success("market_recent") is not None
    except Exception:
        market = False
    if security and market:
        return None
    return (
        "No local research dataset is available.\n"
        "Option A: start the offline fixture with `quantos start --demo`.\n"
        "Option B: configure a provider, then run `quantos data bootstrap`."
    )


def git_commit(project_root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=project_root,
            check=True, capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except Exception:
        try:
            value = json.loads(
                (Path(project_root) / "release-manifest.json").read_text(encoding="utf-8")
            ).get("git_commit")
            if isinstance(value, str) and len(value) == 40:
                return value
        except (OSError, ValueError, TypeError):
            pass
        return "UNKNOWN"


def launch_product(
    *, settings: Settings, workspace_dir: Path, host: str, port: int,
    port_explicit: bool, runtime_mode: RuntimeMode, open_browser: bool,
    build_commit: str | None = None,
    health_timeout: float = 10.0,
) -> int:
    """Serve the API and built SPA in one local process until interrupted."""

    import uvicorn
    from quantos.api import create_app

    selected = select_local_port(host, port, explicit=port_explicit)
    if selected != port:
        print(f"Port {port} unavailable. Using {selected}.")
    app = create_app(
        settings=settings, runtime_mode=runtime_mode, workspace_dir=workspace_dir,
        build_commit=build_commit or git_commit(Path(settings.project_root)),
    )
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=selected, log_level="info", access_log=False,
    ))
    worker = threading.Thread(target=server.run, name="quantos-product", daemon=True)
    worker.start()
    url = f"http://{host}:{selected}"
    try:
        wait_for_health(f"{url}/v1/health", timeout=health_timeout)
    except BaseException:
        server.should_exit = True
        worker.join(timeout=5)
        raise
    print(f"QuantOS Workspace  {url}")
    print(f"Runtime mode       {runtime_mode}")
    if open_browser:
        webbrowser.open(url)
    try:
        while worker.is_alive():
            worker.join(timeout=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        worker.join(timeout=5)
    return 0


class _DemoSupplemental:
    def evaluate(self, *, symbol: str, cutoff: datetime) -> ReplaySupplementalOutcome:
        return ReplaySupplementalOutcome(
            False, False, False, False, False, (),
            ("ATTRIBUTION_INSUFFICIENT", "EVIDENCE_UNAVAILABLE",
             "KNOWLEDGE_TEMPORAL_UNVERIFIED", "REPORT_NOT_VISIBLE"),
            0,
        )

    def configuration_state(self) -> dict[str, str]:
        return {"mode": "DEMO", "network": "DISABLED"}


def prepare_demo_workspace(project_root: Path) -> Settings:
    """Create a small, deterministic, offline product demonstration dataset."""

    settings = Settings.from_project_root(project_root)
    identity_at = datetime(2026, 8, 2, 18, tzinfo=MARKET_TIMEZONE)
    snapshot = SecurityMasterRepository(settings).write_snapshot((SecurityMaster(
        "600519.SH", "贵州茅台（演示）", "SHSE", date(2001, 8, 27), identity_at,
        source="quantos_demo",
    ),), provider_id="quantos_demo", observed_at=identity_at)

    market = MarketDataRepository(settings)
    raws = tuple(_demo_raw(day, index) for index, day in enumerate(DEMO_DATES, 1))
    market.write_raw(raws)
    market.write_bars(tuple(_demo_bar(item) for item in raws))
    dataset = HistoricalDatasetManifest.build(
        provider="quantos_demo_fixture", market="A_SHARE",
        symbols=("600519.SH",), start_date=DEMO_DATES[0], end_date=DEMO_DATES[-1],
        trading_dates=DEMO_DATES,
        record_refs=tuple(item.provider_record_id for item in raws), imported_at=DEMO_NOW,
    )
    HistoricalDatasetRepository(settings).save(dataset)

    replay = ReplayCampaignService(
        settings=settings,
        engine=ReplayEngine(settings=settings, supplemental=_DemoSupplemental()),
        clock=lambda: DEMO_NOW,
        code_version=lambda: ("DEMO_FIXTURE", False),
    )
    replay.run(
        dataset_id=dataset.dataset_id, symbols=("600519.SH",),
        start_date=DEMO_DATES[0], end_date=DEMO_DATES[-1],
    )
    DailyReportRepository(settings).write(_demo_report())
    BootstrapManifestRepository(settings).write(BootstrapManifest(
        "b" * 32, "bootstrap", identity_at, DEMO_NOW,
        ("quantos_demo",), ("security_master", "market_recent"),
        3, 3, 0, (snapshot.snapshot.snapshot_id, dataset.dataset_id), "PASS", (),
    ))
    return settings


def _demo_raw(day: date, index: int) -> RawMarketRecord:
    available = datetime.combine(day, time(18), MARKET_TIMEZONE)
    close = Decimal("100") + Decimal(index * 2)
    return RawMarketRecord(
        "quantos_demo_fixture", "600519.SH", f"demo-daily-{index}", DEMO_NOW,
        {
            "timestamp": day.isoformat(), "frequency": "1d",
            "open": str(close - 1), "high": str(close + 1),
            "low": str(close - 2), "close": str(close),
            "prev_close": str(close - 2), "volume": str(1_000_000 + index),
            "amount": str(close * Decimal(1_000_000 + index)),
            "available_at": available.isoformat(),
        },
    )


def _demo_bar(value: RawMarketRecord) -> MarketBar:
    fields = value.fields
    return MarketBar(
        value.symbol,
        datetime.fromisoformat(str(fields["timestamp"])).replace(tzinfo=MARKET_TIMEZONE),
        str(fields["frequency"]), Decimal(str(fields["open"])),
        Decimal(str(fields["high"])), Decimal(str(fields["low"])),
        Decimal(str(fields["close"])), int(str(fields["volume"])),
        Decimal(str(fields["amount"])), value.provider, value.provider_record_id,
        value.received_at, datetime.fromisoformat(str(fields["available_at"])),
        Decimal(str(fields["prev_close"])),
    )


def _demo_report() -> DailyIntelligenceReport:
    brief = CandidateBrief(
        1, "600519.SH", "贵州茅台（演示）", 2, ("PRICE_MOVE",),
        {"trade_date": DEMO_DATES[-1].isoformat(), "close": Decimal("104"),
         "return_pct": Decimal("1.9608")},
        None, {"available": False}, EvidenceStats(0, 0, 0, 0, 0, 0),
        SynthesisBrief(
            "NO_EVIDENCE_FAST_PATH", None, None, None, None, True, (), (), (), (),
            ("Demo fixture contains no causal evidence.",), None, None,
        ),
    )
    return DailyIntelligenceReport(
        "daily-intelligence-v1", "d" * 64, DEMO_DATES[-1],
        datetime.combine(DEMO_DATES[-1], time(18), MARKET_TIMEZONE), DEMO_NOW,
        "strict_live", "quantos_demo", {
            "security_member_count": 1, "valid_bar_count": 1,
            "coverage_ratio": Decimal("1"), "advancer_count": 1,
            "decliner_count": 0, "flat_count": 0,
            "total_volume": 1_000_002, "total_amount": Decimal("104000208"),
        },
        {"classification_system": "演示", "sector_count": 0,
         "complete_sector_count": 0, "incomplete_sector_count": 0,
         "top_sectors": (), "bottom_sectors": ()},
        {"stocks_scanned": 1, "stocks_analyzed": 1,
         "insufficient_history": 0, "price_anomaly_count": 1,
         "volume_anomaly_count": 0, "amount_anomaly_count": 0,
         "candidate_count": 1, "tier_distribution": {"2": 1}},
        {"total_candidates": 1, "selected_for_report": 1,
         "not_selected": 0, "requested_top_n": 1},
        (brief,), {"discovery_evidence_count": 0, "event_evidence_count": 0,
                   "research_attribution_count": 0, "strict_attribution_count": 0,
                   "post_event_context_count": 0},
        {"evidence": {"historical_proxy_used": False}},
        {"cache_hits": 0, "generated": 0, "no_evidence_fast_paths": 1,
         "failures": 0, "planned_llm_requests": 0, "actual_llm_requests": 0,
         "total_tokens": 0},
        {"quantos_git_commit": "DEMO_FIXTURE"},
    )
