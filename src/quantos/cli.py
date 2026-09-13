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
        return _run_doctor(json_output=args.json_output)
    if args.command == "demo":
        return _run_demo(json_output=args.json_output)
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


def _run_doctor(*, json_output: bool) -> int:
    """Check the installed local runtime without credentials, writes, or network."""
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
    status = "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL"
    payload = {
        "command": "doctor",
        "status": status,
        "offline": True,
        "read_only": True,
        "credential_required": False,
        "checks": checks,
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"QuantOS doctor  {status}")
        for item in checks:
            print(f"{item['name']:<24} {item['status']:<4}  {item['detail']}")
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
    doctor = subparsers.add_parser(
        "doctor", help="check the local installation without network access"
    )
    doctor.add_argument("--json", action="store_true", dest="json_output")
    demo = subparsers.add_parser(
        "demo", help="run an offline demo using audited synthetic data"
    )
    demo.add_argument("--json", action="store_true", dest="json_output")
    return parser
