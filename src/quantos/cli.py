"""Command-line entry points for QuantOS operational health checks."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Sequence
from uuid import uuid4

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
    if args.command != "health":
        parser.print_help()
        return 2

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
    subparsers = parser.add_subparsers(dest="command")
    health = subparsers.add_parser("health", help="run the Phase 1A health pipeline")
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
    return parser
