"""Unified user-facing command-line entry points for QuantOS."""

from __future__ import annotations

import argparse
from importlib.util import find_spec
import json
import logging
import sys
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from platform import python_version
from typing import Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from quantos import __version__
from quantos.commands import CommandFailure, render_json
from quantos.commands import report as report_command
from quantos.commands import ask as ask_command
from quantos.commands import scheduler as scheduler_command
from quantos.commands import data as data_command
from quantos.commands import replay as replay_command
from quantos.commands.status import inspect_status, render_status

from quantos.collectors import (
    EastmoneyMarketCollector,
    MarketDataError,
    MarketDataProvider,
    TushareMarketCollector,
)
from quantos.config import MARKET_TIMEZONE, Settings
from quantos.normalizers import MarketNormalizer
from quantos.observability.models import RunHealth
from quantos.observability.events import ConsoleEventSink, StructuredLoggingEventSink
from quantos.observability.reporting import (
    health_report_to_dict,
    render_health_report,
    save_health_report,
)
from quantos.observability.runner import HealthPipelineRunner
from quantos.schemas import JobContext, JobType
from quantos.storage import MarketDataRepository
from quantos.data import ProviderRegistry
from quantos.storage import (
    BootstrapManifestRepository, SecurityMasterRepository, StorageError,
)


_EXIT_CODES = {
    RunHealth.HEALTHY: 0,
    RunHealth.DEGRADED: 1,
    RunHealth.UNHEALTHY: 2,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2

    if args.command == "doctor":
        return _run_doctor(json_output=args.json_output, project_root=args.project_root)
    if args.command == "demo":
        return _run_demo(json_output=args.json_output)
    if args.command == "status":
        return _run_status(args)
    if args.command == "report":
        return _run_report(args, parser)
    if args.command == "scheduler":
        return _run_scheduler(args, parser)
    if args.command == "ask":
        return _run_ask(args)
    if args.command == "data":
        return _run_data(args)
    if args.command == "replay":
        return _run_replay(args)
    if args.command == "serve":
        return _run_serve(args)
    if args.command == "start":
        return _run_start(args)
    return _run_health(args, parser)


def _run_health(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Run the existing market-data health command without changing its contract."""

    try:
        start_date = date.fromisoformat(args.start_date)
        end_date = date.fromisoformat(args.end_date)
        if start_date > end_date:
            raise ValueError("start-date cannot be after end-date")
        as_of_time = (
            _aware_datetime(args.as_of_time)
            if args.as_of_time
            else datetime.now(MARKET_TIMEZONE)
        )
        start_time = datetime.combine(start_date, time.min, tzinfo=MARKET_TIMEZONE)
        end_time = datetime.combine(
            end_date, time(23, 59, 59), tzinfo=MARKET_TIMEZONE
        )
        if end_time > as_of_time:
            raise ValueError("end-date cannot be after as-of-time")
    except ValueError as exc:
        parser.error(str(exc))

    context = JobContext(
        run_id=args.run_id or uuid4().hex,
        job_type=JobType.MARKET_INGESTION,
        trade_date=end_date,
        as_of_time=as_of_time,
    )
    settings = Settings.from_project_root(Path(args.project_root))
    event_sinks = [StructuredLoggingEventSink(logging.getLogger("quantos.runtime"))]
    if not args.json_output:
        event_sinks.insert(0, ConsoleEventSink())
    try:
        provider = _build_market_provider(args.provider)
    except MarketDataError as exc:
        print(f"Provider configuration failed: {exc}", file=sys.stderr)
        return 2
    runner = HealthPipelineRunner(
        collector=provider,
        normalizer=MarketNormalizer(),
        repository=MarketDataRepository(settings),
        wall_clock=lambda: datetime.now(MARKET_TIMEZONE),
        event_sinks=event_sinks,
    )
    report = runner.run(
        job_context=context,
        symbols=args.symbol,
        frequency=args.frequency,
        start_time=start_time,
        end_time=end_time,
    )
    if args.json_output:
        print(
            json.dumps(
                health_report_to_dict(report), ensure_ascii=False, indent=2
            )
        )
    else:
        print(render_health_report(report))
    if args.save_report:
        report_dir = (
            Path(args.report_dir)
            if args.report_dir
            else settings.data_root / "observability" / "runs"
        )
        saved_path = save_health_report(report, report_dir)
        print(f"Report Saved    {saved_path}", file=sys.stderr)
    return _EXIT_CODES[report.status]


def _run_doctor(*, json_output: bool, project_root: Path | str = ".") -> int:
    """Check the installed local runtime without credentials, writes, or network."""
    from quantos.product import (
        ProductStartError, resolve_workspace_assets, select_local_port,
    )
    checks = [
        {
            "name": "python",
            "status": "PASS" if sys.version_info >= (3, 10) else "FAIL",
            "detail": python_version(),
        },
        {
            "name": "package",
            "status": "PASS",
            "detail": f"quantos {__version__}",
        },
    ]
    for module_name in ("duckdb", "pytz", "tushare", "baostock"):
        checks.append({
            "name": f"dependency:{module_name}",
            "status": "PASS" if find_spec(module_name) is not None else "FAIL",
            "detail": "available" if find_spec(module_name) is not None else "missing",
        })
    try:
        ZoneInfo("Asia/Shanghai")
        timezone_status = "PASS"
        timezone_detail = "Asia/Shanghai available"
    except ZoneInfoNotFoundError:
        timezone_status = "FAIL"
        timezone_detail = "Asia/Shanghai unavailable"
    checks.append({
        "name": "timezone",
        "status": timezone_status,
        "detail": timezone_detail,
    })
    try:
        try:
            assets = resolve_workspace_assets(Path(project_root))
        except ProductStartError:
            assets = resolve_workspace_assets(Path(__file__).resolve().parents[2])
        workspace_status, workspace_detail = "PASS", f"built assets: {assets.name}"
    except ProductStartError:
        workspace_status, workspace_detail = "FAIL", "run: cd web && npm ci && npm run build"
    checks.append({
        "name": "workspace_assets", "status": workspace_status,
        "detail": workspace_detail,
    })
    checks.append({
        "name": "demo_mode", "status": "PASS",
        "detail": "offline deterministic fixture available",
    })
    try:
        selected_port = select_local_port("127.0.0.1", 8000, explicit=False)
        port_detail = (
            "127.0.0.1:8000 available" if selected_port == 8000
            else f"8000 occupied; automatic fallback available ({selected_port})"
        )
        port_status = "PASS"
    except ProductStartError as error:
        port_status, port_detail = "FAIL", str(error)
    checks.append({
        "name": "port_strategy", "status": port_status, "detail": port_detail,
    })
    status = "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL"
    settings = Settings.from_project_root(project_root)
    providers = [item.to_dict() for item in ProviderRegistry(settings=settings).inspect()]
    security_ready = SecurityMasterRepository(settings).is_initialized()
    try:
        market_ready = BootstrapManifestRepository(settings).latest_success("market_recent") is not None
    except StorageError:
        market_ready = False
    payload = {
        "command": "doctor",
        "status": status,
        "offline": True,
        "read_only": True,
        "credential_required": False,
        "checks": checks,
        "project_root": str(settings.project_root),
        "providers": providers,
        "data": {
            "security_master": "READY" if security_ready else "UNAVAILABLE",
            "recent_market": "READY" if market_ready else "UNAVAILABLE",
            "historical_market": "UNAVAILABLE",
        },
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"QuantOS doctor  {status}")
        for item in checks:
            print(f"{item['name']:<24} {item['status']:<4}  {item['detail']}")
        print(f"Security Master          {payload['data']['security_master']}")
        print(f"Recent Market            {payload['data']['recent_market']}")
        print("Offline          YES")
        print("Read only        YES")
        print("Credential required NO")
    return 0 if status == "PASS" else 1


def _run_demo(*, json_output: bool) -> int:
    """Run the audited deterministic synthetic evaluation in temporary storage."""
    from quantos.evaluation import (
        EvaluationDataSource,
        run_synthetic_strict_evaluation,
    )

    with tempfile.TemporaryDirectory(prefix="quantos-demo-") as temporary:
        result = run_synthetic_strict_evaluation(
            Path(temporary) / "evaluation",
            trading_days=2,
            candidate_count=2,
        )
    summary = result.summary
    payload = {
        "command": "demo",
        "status": "PASS",
        "data_source": summary["data_source"],
        "synthetic_disclaimer": "PRESENT",
        "credential_required": False,
        "real_provider_requests": summary["provider_network_request_count"],
        "real_llm_requests": summary["real_deepseek_request_count"],
        "embedding_requests": summary["embedding_provider_request_count"],
        "pit_violations": summary["pit_violation_count"],
        "trading_days_evaluated": summary["trading_days_evaluated"],
        "candidate_count": summary["candidate_count"],
        "evaluation_id": summary["evaluation_id"],
    }
    expected = {
        "data_source": EvaluationDataSource.SYNTHETIC_FIXTURE.value,
        "real_provider_requests": 0,
        "real_llm_requests": 0,
        "embedding_requests": 0,
        "pit_violations": 0,
    }
    if any(payload[name] != value for name, value in expected.items()):
        payload["status"] = "FAIL"
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"QuantOS synthetic demo  {payload['status']}")
        print("SYNTHETIC FIXTURE — NOT REAL-HISTORICAL PERFORMANCE")
        print(f"Trading days evaluated  {payload['trading_days_evaluated']}")
        print(f"Candidates              {payload['candidate_count']}")
        print("Credential required     NO")
        print(f"Real provider requests  {payload['real_provider_requests']}")
        print(f"Real LLM requests       {payload['real_llm_requests']}")
        print(f"Embedding requests      {payload['embedding_requests']}")
        print(f"PIT violations          {payload['pit_violations']}")
        print(f"Synthetic disclaimer    {payload['synthetic_disclaimer']}")
    return 0 if payload["status"] == "PASS" else 1


def _run_status(args: argparse.Namespace) -> int:
    result = inspect_status(args.project_root)
    print(render_json(result) if args.json_output else render_status(result))
    return 0


def _run_data(args: argparse.Namespace) -> int:
    if args.data_action is None or getattr(args, "dataset", None) is None:
        args._command_parser.print_help()
        return 2
    try:
        result = data_command.execute(args)
    except ValueError as error:
        print(render_json({"status": "FAIL", "error_code": str(error)}), file=sys.stderr)
        return 2
    print(render_json(result) if args.json_output else data_command.render(result))
    return 0 if result["status"] == "PASS" else 2


def _run_replay(args: argparse.Namespace) -> int:
    if args.replay_command is None:
        args._command_parser.print_help()
        return 2
    try:
        result = replay_command.execute(args)
    except Exception as error:
        from quantos.replay.identity import HistoricalIdentityUnavailableError
        from quantos.replay.importer import HistoricalImportError
        from quantos.replay.storage import ReplayStorageError
        from quantos.storage import SecurityMasterUnavailableError
        if not isinstance(error, (
            HistoricalIdentityUnavailableError, HistoricalImportError,
            ReplayStorageError, SecurityMasterUnavailableError, ValueError,
        )):
            raise
        print(render_json({"status": "FAIL", "error_code": str(error)}), file=sys.stderr)
        return 2
    print(render_json(result) if args.json_output else replay_command.render(result))
    if args.replay_command == "run":
        return 1 if result["summary"]["failed_points"] else 0
    return 0


def _run_serve(args: argparse.Namespace) -> int:
    """Run the local-only read Research API."""

    import uvicorn
    from quantos.api import create_app

    settings = Settings.from_project_root(args.project_root)
    uvicorn.run(create_app(settings=settings), host=args.host, port=args.port)
    return 0


def _run_start(args: argparse.Namespace) -> int:
    """Start the integrated local Research API and built Workspace."""

    from quantos.product import (
        ProductStartError, git_commit, launch_product, local_data_notice,
        prepare_demo_workspace, resolve_workspace_assets,
    )

    project_root = Path(args.project_root).resolve()
    source_root = Path(__file__).resolve().parents[2]
    product_commit = git_commit(project_root)
    try:
        try:
            workspace = resolve_workspace_assets(project_root)
        except ProductStartError:
            workspace = resolve_workspace_assets(source_root)
        port = args.port if args.port is not None else 8000
        if args.demo:
            with tempfile.TemporaryDirectory(prefix="quantos-product-demo-") as temporary:
                settings = prepare_demo_workspace(Path(temporary))
                print("DEMO DATA — SYNTHETIC / FIXTURE DATA")
                print("For research/product demonstration. Not investment advice.")
                return launch_product(
                    settings=settings, workspace_dir=workspace, host=args.host,
                    port=port, port_explicit=args.port is not None,
                    runtime_mode="DEMO", open_browser=not args.no_browser,
                    build_commit=product_commit,
                )
        settings = Settings.from_project_root(project_root)
        notice = local_data_notice(settings)
        if notice:
            print(notice, file=sys.stderr)
        return launch_product(
            settings=settings, workspace_dir=workspace, host=args.host,
            port=port, port_explicit=args.port is not None,
            runtime_mode="LOCAL", open_browser=not args.no_browser,
            build_commit=product_commit,
        )
    except ProductStartError as error:
        print(f"QuantOS start failed: {error}", file=sys.stderr)
        return 2


def _port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _run_report(
    args: argparse.Namespace, parser: argparse.ArgumentParser,
) -> int:
    if args.report_command is None:
        args._command_parser.print_help()
        return 2
    settings = Settings.from_project_root(args.project_root)
    try:
        if args.report_command == "daily":
            top_n = report_command.validate_common(args, parser)
            result = report_command.execute_daily(
                report_command.DailyOptions(
                    args.trade_date, args.as_of_time, args.mode,
                    top_n, args.no_llm,
                ),
                settings=settings,
            )
            rendered = report_command.render_daily(result)
        else:
            run_type = (
                report_command.RunType.PRE_OPEN
                if args.report_command == "pre-open"
                else report_command.RunType.POST_CLOSE
            )
            top_n = report_command.validate_time_slice(args, parser, run_type)
            result = report_command.execute_time_slice(
                report_command.TimeSliceOptions(
                    args.trade_date, args.as_of_time, run_type, args.mode,
                    top_n, args.no_llm, args.plan_only,
                ),
                settings=settings,
            )
            rendered = report_command.render_time_slice(result)
    except CommandFailure as error:
        failure = error.record()
        print(render_json(failure) if args.json_output else (
            f"Command failed     {failure['error_code']}\n"
            f"Remediation        {failure['remediation']}"
        ))
        return error.exit_code
    print(render_json(result) if args.json_output else rendered)
    return 0 if result["status"] == "PASS" else 1


def _run_scheduler(
    args: argparse.Namespace, parser: argparse.ArgumentParser,
) -> int:
    if args.scheduler_command is None:
        args._command_parser.print_help()
        return 2
    try:
        top_n = scheduler_command.validate(args, parser)
        exit_code, result = scheduler_command.execute(args, top_n=top_n)
    except CommandFailure as error:
        print(render_json(error.record()), file=sys.stderr)
        return error.exit_code
    print(render_json(result))
    return exit_code


def _run_ask(args: argparse.Namespace) -> int:
    try:
        as_of_time = _aware_datetime(args.as_of_time) if args.as_of_time else None
        session = ask_command.make_session(args.symbol, as_of_time,
                                           live_cutoff=args.as_of_time is None,
                                           no_research=args.no_research)
        if args.question is not None:
            result = session.ask(args.question)
            print(render_json(result.to_dict() if hasattr(result, "to_dict") else result)
                  if args.json_output else ask_command.render(result))
            return 0 if result["status"] in {"PASS", "PARTIAL"} else 2
        if not sys.stdin.isatty():
            raise ask_command.AskFailure("QUESTION_REQUIRED")
        print("QuantOS Ask：输入问题，输入 :q 退出。")
        while True:
            try:
                question = input("ask> ").strip()
            except EOFError:
                break
            if question.lower() in {":q", "exit", "quit"}:
                break
            if not question:
                continue
            try:
                result = session.ask(question)
                print(render_json(result.to_dict() if hasattr(result, "to_dict") else result)
                      if args.json_output else ask_command.render(result))
            except ask_command.AskFailure as error:
                print(f"Ask failed: {error}", file=sys.stderr)
        return 0
    except (ask_command.AskFailure, ValueError) as error:
        code = str(error) if isinstance(error, ask_command.AskFailure) else "TIMEZONE_REQUIRED"
        print(render_json({"status": "FAIL", "error_code": code,
                           "remediation": "Check symbol, time, provider availability, and the question."}),
              file=sys.stderr)
        return 2


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("as-of-time must include a timezone offset")
    return parsed


def _build_market_provider(name: str) -> MarketDataProvider:
    if name == "tushare":
        return TushareMarketCollector()
    if name == "eastmoney":
        return EastmoneyMarketCollector()
    raise ValueError(f"unsupported provider: {name}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quantos")
    parser.add_argument("--version", action="version", version=f"quantos {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    doctor = subparsers.add_parser(
        "doctor", help="check the local installation without network access"
    )
    doctor.add_argument("--json", action="store_true", dest="json_output")
    doctor.add_argument("--project-root", type=Path, default=Path.cwd())
    demo = subparsers.add_parser(
        "demo", help="run an offline demo using audited synthetic data"
    )
    demo.add_argument("--json", action="store_true", dest="json_output")
    health = subparsers.add_parser(
        "health", help="collect market data and run pipeline health checks"
    )
    health.add_argument("--symbol", action="append", required=True)
    health.add_argument("--frequency", default="1d")
    health.add_argument(
        "--provider", choices=("tushare", "eastmoney"), default="tushare"
    )
    health.add_argument("--start-date", required=True)
    health.add_argument("--end-date", required=True)
    health.add_argument("--as-of-time")
    health.add_argument("--run-id")
    health.add_argument("--project-root", default=".")
    health.add_argument("--json", action="store_true", dest="json_output")
    health.add_argument("--save-report", action="store_true")
    health.add_argument("--report-dir")
    status = subparsers.add_parser(
        "status", help="inspect local data and product readiness"
    )
    status.add_argument("--project-root", type=Path, default=Path.cwd())
    status.add_argument("--json", action="store_true", dest="json_output")

    serve = subparsers.add_parser(
        "serve", help="serve the local read-only Research API"
    )
    serve.add_argument("--project-root", type=Path, default=Path.cwd())
    serve.add_argument(
        "--host", choices=("127.0.0.1", "localhost", "::1"),
        default="127.0.0.1",
    )
    serve.add_argument("--port", type=_port_number, default=8000)

    start = subparsers.add_parser(
        "start", help="start the integrated local Research Workspace"
    )
    start.add_argument("--project-root", type=Path, default=Path.cwd())
    start.add_argument(
        "--host", choices=("127.0.0.1", "localhost", "::1"),
        default="127.0.0.1",
    )
    start.add_argument("--port", type=_port_number)
    start.add_argument("--no-browser", action="store_true")
    start.add_argument("--demo", action="store_true")

    data = subparsers.add_parser(
        "data", help="bootstrap or refresh persistent real-data foundations"
    )
    data.set_defaults(_command_parser=data)
    data_actions = data.add_subparsers(dest="data_action")
    bootstrap = data_actions.add_parser("bootstrap", help="initialize a bounded dataset")
    bootstrap.set_defaults(_command_parser=bootstrap)
    bootstrap_sets = bootstrap.add_subparsers(dest="dataset")
    bootstrap_security = bootstrap_sets.add_parser("security-master")
    bootstrap_security.add_argument("--provider", choices=("tushare", "baostock"), default="tushare")
    bootstrap_security.add_argument("--project-root", type=Path, default=Path.cwd())
    bootstrap_security.add_argument("--json", action="store_true", dest="json_output")
    bootstrap_market = bootstrap_sets.add_parser("market")
    bootstrap_market.add_argument("--provider", choices=("tushare",), default="tushare")
    bootstrap_market.add_argument("--symbol", action="append", required=True)
    bootstrap_market.add_argument("--trading-days", type=int, default=5)
    bootstrap_market.add_argument("--as-of-time")
    bootstrap_market.add_argument("--project-root", type=Path, default=Path.cwd())
    bootstrap_market.add_argument("--json", action="store_true", dest="json_output")
    refresh = data_actions.add_parser("refresh", help="append a current provider observation")
    refresh.set_defaults(_command_parser=refresh)
    refresh_sets = refresh.add_subparsers(dest="dataset")
    refresh_security = refresh_sets.add_parser("security-master")
    refresh_security.add_argument("--provider", choices=("tushare", "baostock"), default="tushare")
    refresh_security.add_argument("--project-root", type=Path, default=Path.cwd())
    refresh_security.add_argument("--json", action="store_true", dest="json_output")

    replay = subparsers.add_parser(
        "replay", help="run manifest-gated offline historical evaluation"
    )
    replay.set_defaults(_command_parser=replay)
    replay_commands = replay.add_subparsers(dest="replay_command")
    identity_derivation = replay_commands.add_parser(
        "derive-identity",
        help="derive retrospective effective-time identity from an audited snapshot",
    )
    identity_derivation.add_argument("--snapshot-id", required=True)
    identity_derivation.add_argument("--project-root", type=Path, default=Path.cwd())
    identity_derivation.add_argument("--json", action="store_true", dest="json_output")
    historical_import = replay_commands.add_parser(
        "import-market", help="explicitly import a bounded historical market range"
    )
    historical_import.add_argument("--provider", choices=("tushare",), default="tushare")
    historical_import.add_argument("--symbol", action="append", required=True)
    historical_import.add_argument("--start", required=True)
    historical_import.add_argument("--end", required=True)
    historical_import.add_argument("--as-of-time")
    historical_import.add_argument("--project-root", type=Path, default=Path.cwd())
    historical_import.add_argument("--json", action="store_true", dest="json_output")
    replay_run = replay_commands.add_parser("run", help="run an offline replay campaign")
    replay_run.add_argument("--dataset-id", required=True)
    replay_run.add_argument("--symbol", action="append", required=True)
    replay_run.add_argument("--start", required=True)
    replay_run.add_argument("--end", required=True)
    replay_run.add_argument(
        "--mode", choices=("strict-operational", "retrospective-reconstructed"),
        default="strict-operational",
    )
    replay_run.add_argument("--identity-authority-id")
    replay_run.add_argument("--no-determinism-check", action="store_true")
    replay_run.add_argument("--project-root", type=Path, default=Path.cwd())
    replay_run.add_argument("--json", action="store_true", dest="json_output")
    replay_show = replay_commands.add_parser("show", help="inspect a persisted campaign")
    replay_show.add_argument("campaign_id")
    replay_show.add_argument("--project-root", type=Path, default=Path.cwd())
    replay_show.add_argument("--json", action="store_true", dest="json_output")
    replay_failures = replay_commands.add_parser(
        "failures", help="inspect structured campaign failures"
    )
    replay_failures.add_argument("campaign_id")
    replay_failures.add_argument("--project-root", type=Path, default=Path.cwd())
    replay_failures.add_argument("--json", action="store_true", dest="json_output")

    report = subparsers.add_parser(
        "report", help="generate Daily or time-sliced local reports"
    )
    report.set_defaults(_command_parser=report)
    report_subparsers = report.add_subparsers(dest="report_command")
    daily = report_subparsers.add_parser("daily", help="generate Daily Intelligence")
    report_command.add_daily_arguments(daily)
    pre_open = report_subparsers.add_parser(
        "pre-open", help="generate a PRE_OPEN TimeSlice"
    )
    report_command.add_time_slice_arguments(pre_open)
    post_close = report_subparsers.add_parser(
        "post-close", help="generate a POST_CLOSE TimeSlice"
    )
    report_command.add_time_slice_arguments(post_close)

    scheduler = subparsers.add_parser(
        "scheduler", help="run local scheduler operations"
    )
    scheduler.set_defaults(_command_parser=scheduler)
    scheduler_subparsers = scheduler.add_subparsers(dest="scheduler_command")
    once = scheduler_subparsers.add_parser(
        "once", help="evaluate or execute one scheduler tick"
    )
    scheduler_command.add_arguments(once)
    ask = subparsers.add_parser("ask", help="ask grounded questions about one stock")
    ask.add_argument("symbol", help="stock symbol, such as 600519.SH")
    ask.add_argument("--question", help="one question; omit for interactive mode")
    ask.add_argument("--as-of-time", help="timezone-aware ISO cutoff; defaults to now")
    ask.add_argument("--no-research", action="store_true",
                     help="answer from market facts without Tavily search")
    ask.add_argument("--json", action="store_true", dest="json_output")
    return parser
