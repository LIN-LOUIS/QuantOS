"""Shared one-shot scheduler command over existing scheduler semantics."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from quantos.commands import CommandFailure
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


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", choices=("research", "strict_live"), required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--runtime-db", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--at", type=datetime.fromisoformat)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--top-n", type=int)
    parser.add_argument("--max-execution-minutes", type=int, default=55)


def validate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.at is not None and not args.dry_run:
        parser.error("--at is allowed only with --dry-run")
    if args.at is not None and (args.at.tzinfo is None or args.at.utcoffset() is None):
        parser.error("--at must be timezone-aware")
    try:
        top_n = (
            args.top_n
            if args.top_n is not None
            else SynthesisBatchSettings.from_env().synthesis_top_n
        )
    except (TypeError, ValueError):
        raise CommandFailure(
            "OPERATIONAL_CONFIG_INVALID", "Fix scheduler environment configuration.",
            EXIT_INVALID_CONFIG,
        ) from None
    if top_n < 1 or args.max_execution_minutes < 1:
        parser.error("--top-n and --max-execution-minutes must be positive")
    if timedelta(minutes=args.max_execution_minutes) >= (
        DEFAULT_SCHEDULER_RUNTIME_POLICY.lease_duration
    ):
        parser.error("--max-execution-minutes must be shorter than the runtime lease")
    return top_n


def execute(
    args: argparse.Namespace,
    *,
    top_n: int,
    datetime_type=datetime,
    executor_factory: Callable = QuantOSRunExecutorAdapter,
) -> tuple[int, dict[str, object]]:
    evaluated_at = (
        args.at.astimezone(MARKET_TIMEZONE)
        if args.at is not None
        else datetime_type.now(timezone.utc).astimezone(MARKET_TIMEZONE)
    )
    try:
        calendar = load_local_calendar_artifact(args.calendar)
        validate_calendar_coverage(calendar, evaluated_at)
        repository = executor = None
        if not args.dry_run:
            project_root = args.project_root.expanduser().resolve()
            if not project_root.is_dir():
                raise OperationalConfigError("PROJECT_ROOT_INVALID")
            settings = Settings.from_project_root(project_root)
            repository = SchedulerRuntimeRepository(
                settings, path=args.runtime_db.expanduser().resolve(),
            )
            executor = executor_factory(settings)
        result = run_scheduler_once(
            evaluated_at=evaluated_at, mode=args.mode,
            calendar_artifact=calendar, dry_run=args.dry_run,
            repository=repository, run_executor=executor,
            top_n=top_n, llm_allowed=not args.no_llm,
            maximum_execution_duration=timedelta(
                minutes=args.max_execution_minutes,
            ),
        )
    except CalendarArtifactError as exc:
        code = str(exc) if str(exc).isupper() else "CALENDAR_ARTIFACT_INVALID"
        raise CommandFailure(code, "Provide a valid covered local calendar artifact.", EXIT_CALENDAR) from None
    except OperationalConfigError as exc:
        code = str(exc) if str(exc).isupper() else "OPERATIONAL_CONFIG_INVALID"
        raise CommandFailure(code, "Fix scheduler paths and configuration.", EXIT_INVALID_CONFIG) from None
    except StorageError:
        raise CommandFailure(
            "SCHEDULER_RUNTIME_STORAGE_FAILURE",
            "Repair the local scheduler runtime database path.", EXIT_RUNTIME,
        ) from None
    except CommandFailure:
        raise
    except Exception:
        raise CommandFailure(
            "SCHEDULER_INTERNAL_FAILURE",
            "Inspect local configuration and retry after resolving the failure.",
            EXIT_INTERNAL,
        ) from None
    return scheduler_once_exit_code(result), scheduler_once_record(result)
