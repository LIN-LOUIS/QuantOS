"""Evaluate QuantOS scheduling once, optionally execute, print JSON, and exit."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

from quantos.config import MARKET_TIMEZONE, Settings, SynthesisBatchSettings
from quantos.scheduler_ops import (
    EXIT_CALENDAR, EXIT_INTERNAL, EXIT_INVALID_CONFIG, EXIT_RUNTIME,
    CalendarArtifactError, OperationalConfigError, QuantOSRunExecutorAdapter,
    load_local_calendar_artifact, run_scheduler_once, scheduler_once_exit_code,
    scheduler_once_record, validate_calendar_coverage,
)
from quantos.scheduler_runtime import DEFAULT_SCHEDULER_RUNTIME_POLICY
from quantos.storage.market import StorageError
from quantos.storage.scheduler_runtime import SchedulerRuntimeRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("research", "strict_live"), required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--runtime-db", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--at", type=datetime.fromisoformat)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--top-n", type=int,
                        default=SynthesisBatchSettings.from_env().synthesis_top_n)
    parser.add_argument("--max-execution-minutes", type=int, default=55)
    return parser


def main(argv=None) -> int:
    try:
        parser = build_parser()
    except ValueError:
        _error("OPERATIONAL_CONFIG_INVALID")
        return EXIT_INVALID_CONFIG
    args = parser.parse_args(argv)
    if args.at is not None and not args.dry_run:
        parser.error("--at is allowed only with --dry-run")
    if args.at is not None and (args.at.tzinfo is None or args.at.utcoffset() is None):
        parser.error("--at must be timezone-aware")
    if args.top_n < 1 or args.max_execution_minutes < 1:
        parser.error("--top-n and --max-execution-minutes must be positive")
    if timedelta(minutes=args.max_execution_minutes) >= DEFAULT_SCHEDULER_RUNTIME_POLICY.lease_duration:
        parser.error("--max-execution-minutes must be shorter than the runtime lease")
    evaluated_at = (args.at.astimezone(MARKET_TIMEZONE) if args.at is not None
                    else datetime.now(timezone.utc).astimezone(MARKET_TIMEZONE))
    try:
        calendar = load_local_calendar_artifact(args.calendar)
        validate_calendar_coverage(calendar, evaluated_at)
        repository = executor = None
        if not args.dry_run:
            project_root = args.project_root.expanduser().resolve()
            if not project_root.is_dir():
                raise OperationalConfigError("PROJECT_ROOT_INVALID")
            settings = Settings.from_project_root(project_root)
            repository = SchedulerRuntimeRepository(settings, path=args.runtime_db.expanduser().resolve())
            executor = QuantOSRunExecutorAdapter(settings)
        result = run_scheduler_once(
            evaluated_at=evaluated_at, mode=args.mode, calendar_artifact=calendar,
            dry_run=args.dry_run, repository=repository, run_executor=executor,
            top_n=args.top_n, llm_allowed=not args.no_llm,
            maximum_execution_duration=timedelta(minutes=args.max_execution_minutes),
        )
    except CalendarArtifactError as exc:
        _error(str(exc) if str(exc).isupper() else "CALENDAR_ARTIFACT_INVALID")
        return EXIT_CALENDAR
    except OperationalConfigError as exc:
        _error(str(exc) if str(exc).isupper() else "OPERATIONAL_CONFIG_INVALID")
        return EXIT_INVALID_CONFIG
    except StorageError:
        _error("SCHEDULER_RUNTIME_STORAGE_FAILURE")
        return EXIT_RUNTIME
    except Exception:
        _error("SCHEDULER_INTERNAL_FAILURE")
        return EXIT_INTERNAL
    print(json.dumps(scheduler_once_record(result), ensure_ascii=False,
                     sort_keys=True, separators=(",", ":")))
    return scheduler_once_exit_code(result)


def _error(code: str) -> None:
    print(json.dumps({"error_code": code}, sort_keys=True), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
