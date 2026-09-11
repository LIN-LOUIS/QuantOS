"""Single-shot operational adapter over scheduling and durable runtime."""

from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path
import time
from typing import Callable
from zoneinfo import ZoneInfo

from quantos.config import Settings
from quantos.orchestration import (
    collect_readiness, execute_run, load_local_artifacts, local_artifact_handlers,
)
from quantos.schemas._validation import require_aware
from quantos.schemas.run import ArtifactReference, RUN_SCHEMA_VERSION, RunContext
from quantos.schemas.schedule import ScheduleDecisionStatus, SchedulePolicy
from quantos.schemas.scheduler_ops import (
    LOCAL_CALENDAR_SCHEMA_VERSION, SCHEDULER_ONCE_SCHEMA_VERSION,
    LocalTradingCalendarArtifact, SchedulerOnceOverallStatus, SchedulerOnceResult,
    SchedulerOpsEvent, SchedulerOpsEventType, SchedulerSlotResult,
)
from quantos.schemas.scheduler_runtime import (
    RuntimeEligibilityStatus, SchedulerExecutionOutcome, SchedulerExecutionResult,
    SchedulerInvocationStatus, SchedulerRuntimePolicy,
)
from quantos.scheduler_runtime import (
    DEFAULT_SCHEDULER_RUNTIME_POLICY, SchedulerRuntime, evaluate_runtime_eligibility,
    schedule_instance_from_decision,
)
from quantos.scheduling import DEFAULT_SCHEDULE_POLICY, LocalTradingCalendar, evaluate_schedule
from quantos.storage.run import RunRepository
from quantos.storage.scheduler_runtime import SchedulerRuntimeRepository


EXIT_OK = 0
EXIT_INVALID_CONFIG = 2
EXIT_CALENDAR = 3
EXIT_RUNTIME = 4
EXIT_EXECUTION = 5
EXIT_INTERNAL = 6


class CalendarArtifactError(ValueError):
    """The local calendar file is missing, corrupt, or noncanonical."""


class CalendarOutOfCoverageError(CalendarArtifactError):
    """The evaluation date is not represented by the local calendar artifact."""


class OperationalConfigError(ValueError):
    """Operational configuration is unsafe or incomplete."""


class QuantOSRunExecutorAdapter:
    """Adapt the existing local RunContext orchestration boundary to 3D.2."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def __call__(self, context: RunContext) -> SchedulerExecutionResult:
        artifacts = load_local_artifacts(
            self.settings, target_trade_date=context.target_trade_date,
            as_of_time=context.as_of_time, mode=context.mode, top_n=context.top_n,
            universe_name=context.universe_name,
        )
        readiness = collect_readiness(
            target_trade_date=context.target_trade_date, as_of_time=context.as_of_time,
            run_type=context.run_type, mode=context.mode, artifacts=artifacts,
        )
        manifest = execute_run(
            context, readiness, handlers=local_artifact_handlers(readiness),
        )
        path = RunRepository(self.settings).write(manifest)
        raw = path.read_bytes()
        reference = ArtifactReference(
            hashlib.sha256(raw).hexdigest(), str(path.resolve()), RUN_SCHEMA_VERSION,
        )
        if manifest.summary.failed_modules:
            return SchedulerExecutionResult(
                context.run_id, SchedulerExecutionOutcome.FAILED_FINAL,
                manifest_ref=reference, safe_error_code="QUANTOS_RUN_FAILED",
            )
        return SchedulerExecutionResult(
            context.run_id, SchedulerExecutionOutcome.SUCCEEDED,
            manifest_ref=reference,
        )


def load_local_calendar_artifact(path: Path) -> LocalTradingCalendarArtifact:
    """Read a closed local artifact; never infer or fetch missing calendar facts."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        expected = {
            "schema_version", "market", "timezone", "coverage_start", "coverage_end",
            "trading_dates", "generated_at",
        }
        if type(record) is not dict or set(record) != expected:
            raise ValueError("invalid calendar artifact fields")
        if type(record["trading_dates"]) is not list:
            raise ValueError("trading_dates must be a list")
        return LocalTradingCalendarArtifact(
            schema_version=record["schema_version"], market=record["market"],
            timezone=record["timezone"],
            coverage_start=date.fromisoformat(record["coverage_start"]),
            coverage_end=date.fromisoformat(record["coverage_end"]),
            trading_dates=tuple(date.fromisoformat(value) for value in record["trading_dates"]),
            generated_at=datetime.fromisoformat(record["generated_at"]),
        )
    except Exception:
        raise CalendarArtifactError("CALENDAR_ARTIFACT_INVALID") from None


def calendar_artifact_record(value: LocalTradingCalendarArtifact) -> dict:
    return {
        "schema_version": value.schema_version,
        "market": value.market,
        "timezone": value.timezone,
        "coverage_start": value.coverage_start.isoformat(),
        "coverage_end": value.coverage_end.isoformat(),
        "trading_dates": [item.isoformat() for item in value.trading_dates],
        "generated_at": value.generated_at.isoformat(),
    }


def validate_calendar_coverage(
    calendar_artifact: LocalTradingCalendarArtifact, evaluated_at: datetime,
) -> datetime:
    """Return market-local evaluation time or fail before runtime state exists."""
    if not isinstance(calendar_artifact, LocalTradingCalendarArtifact):
        raise CalendarArtifactError("CALENDAR_ARTIFACT_INVALID")
    require_aware(evaluated_at, "evaluated_at")
    local_at = evaluated_at.astimezone(ZoneInfo(calendar_artifact.timezone))
    if not calendar_artifact.coverage_start <= local_at.date() <= calendar_artifact.coverage_end:
        raise CalendarOutOfCoverageError("CALENDAR_OUT_OF_COVERAGE")
    return local_at


def run_scheduler_once(
    *, evaluated_at: datetime, mode: str,
    calendar_artifact: LocalTradingCalendarArtifact,
    dry_run: bool, repository: SchedulerRuntimeRepository | None = None,
    run_executor: Callable[[RunContext], SchedulerExecutionResult] | None = None,
    schedule_policy: SchedulePolicy = DEFAULT_SCHEDULE_POLICY,
    runtime_policy: SchedulerRuntimePolicy = DEFAULT_SCHEDULER_RUNTIME_POLICY,
    universe_name: str = "shse_szse_a_share", top_n: int = 20,
    llm_allowed: bool = False, maximum_execution_duration: timedelta = timedelta(minutes=55),
    wall_clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> SchedulerOnceResult:
    """Evaluate every slot once, possibly invoke 3D.2, then return."""
    if mode not in {"research", "strict_live"}:
        raise OperationalConfigError("MODE_REQUIRED")
    if type(dry_run) is not bool:
        raise OperationalConfigError("DRY_RUN_MUST_BE_BOOLEAN")
    if (type(maximum_execution_duration) is not timedelta
        or maximum_execution_duration <= timedelta(0)
        or maximum_execution_duration >= runtime_policy.lease_duration):
        raise OperationalConfigError("EXECUTION_WINDOW_MUST_BE_SHORTER_THAN_LEASE")
    if not dry_run and (repository is None or run_executor is None):
        raise OperationalConfigError("RUNTIME_REPOSITORY_AND_EXECUTOR_REQUIRED")
    if (not isinstance(schedule_policy, SchedulePolicy)
        or schedule_policy.timezone != calendar_artifact.timezone):
        raise OperationalConfigError("SCHEDULE_CALENDAR_TIMEZONE_MISMATCH")
    if type(top_n) is not int or top_n < 1:
        raise OperationalConfigError("TOP_N_MUST_BE_POSITIVE")
    if type(llm_allowed) is not bool:
        raise OperationalConfigError("LLM_ALLOWED_MUST_BE_BOOLEAN")
    if not isinstance(universe_name, str) or not universe_name.strip():
        raise OperationalConfigError("UNIVERSE_NAME_REQUIRED")
    local_at = validate_calendar_coverage(calendar_artifact, evaluated_at)
    started_at = wall_clock() if wall_clock is not None else local_at
    require_aware(started_at, "operational started_at")
    started_tick = monotonic()
    def observed_at():
        return started_at + timedelta(seconds=max(0.0, monotonic() - started_tick))

    events = [SchedulerOpsEvent(SchedulerOpsEventType.SCHEDULER_ONCE_STARTED, started_at)]
    events.append(SchedulerOpsEvent(SchedulerOpsEventType.CALENDAR_LOADED, observed_at(),
                                    status="CALENDAR_READY"))
    calendar = LocalTradingCalendar(calendar_artifact.trading_dates)
    evaluation = evaluate_schedule(
        evaluated_at=local_at, calendar=calendar, policy=schedule_policy,
    )
    events.append(SchedulerOpsEvent(SchedulerOpsEventType.SCHEDULE_EVALUATED, observed_at(),
                                    status="SCHEDULE_EVALUATED"))
    slot_results = []
    for decision in evaluation.decisions:
        if decision.status in {
            ScheduleDecisionStatus.NOT_DUE, ScheduleDecisionStatus.NON_TRADING_DAY,
        }:
            slot_results.append(SchedulerSlotResult(
                decision.slot_id, decision.status, decision.status.value,
                safe_status_code=decision.status.value,
            ))
            continue
        initial_eligibility = evaluate_runtime_eligibility(decision, policy=runtime_policy)
        if dry_run:
            slot_results.append(SchedulerSlotResult(
                decision.slot_id, decision.status, initial_eligibility.status.value,
                safe_status_code=initial_eligibility.skip_reason,
            ))
            continue
        instance = schedule_instance_from_decision(decision, mode=mode)

        def observed_executor(context, *, _slot=decision.slot_id,
                              _instance=instance.schedule_instance_id):
            events.append(SchedulerOpsEvent(
                SchedulerOpsEventType.EXECUTION_STARTED, observed_at(), _slot, _instance,
                context.run_id, status="EXECUTION_STARTED",
            ))
            result = run_executor(context)
            if isinstance(result, SchedulerExecutionResult):
                events.append(SchedulerOpsEvent(
                    SchedulerOpsEventType.EXECUTION_FINISHED, observed_at(), _slot, _instance,
                    context.run_id, status=result.outcome.value,
                    safe_error_code=result.safe_error_code,
                ))
            return result

        runtime_result = SchedulerRuntime(
            repository, observed_executor, policy=runtime_policy,
        ).invoke(
            decision, mode=mode, universe_name=universe_name, top_n=top_n,
            llm_allowed=llm_allowed,
        )
        action = ("CLAIM_ACTIVE" if runtime_result.status == SchedulerInvocationStatus.ALREADY_CLAIMED
                  else runtime_result.status.value)
        attempt = runtime_result.attempt
        safe_code = (attempt.safe_error_code if attempt is not None
                     else runtime_result.instance_record.last_safe_error_code
                     if runtime_result.instance_record is not None else None)
        run_id = runtime_result.run_context.run_id if runtime_result.run_context else None
        slot_results.append(SchedulerSlotResult(
            decision.slot_id, decision.status, action, runtime_result.schedule_instance_id,
            run_id, attempt.attempt_number if attempt else None, safe_code,
        ))
        events.append(SchedulerOpsEvent(
            SchedulerOpsEventType.RUNTIME_ACTION, observed_at(), decision.slot_id,
            runtime_result.schedule_instance_id, run_id,
            attempt.attempt_number if attempt else None, action, safe_code,
        ))
    overall = _overall_status(tuple(slot_results), dry_run=dry_run)
    terminal_event = (
        SchedulerOpsEventType.SCHEDULER_ONCE_FAILED
        if overall in {SchedulerOnceOverallStatus.RETRY_SCHEDULED,
                       SchedulerOnceOverallStatus.EXECUTION_FAILED}
        else SchedulerOpsEventType.SCHEDULER_ONCE_NO_WORK
        if overall == SchedulerOnceOverallStatus.NO_WORK
        else SchedulerOpsEventType.SCHEDULER_ONCE_COMPLETED
    )
    elapsed_seconds = max(0.0, monotonic() - started_tick)
    finished_at = started_at + timedelta(seconds=elapsed_seconds)
    require_aware(finished_at, "operational finished_at")
    events.append(SchedulerOpsEvent(terminal_event, finished_at, status=overall.value))
    duration_ms = round(elapsed_seconds * 1000)
    return SchedulerOnceResult(
        SCHEDULER_ONCE_SCHEMA_VERSION, local_at, mode, calendar_artifact.market,
        calendar_artifact.timezone, calendar_artifact.coverage_start,
        calendar_artifact.coverage_end, dry_run, tuple(slot_results), overall,
        started_at, finished_at, duration_ms, tuple(events),
    )


def scheduler_once_exit_code(result: SchedulerOnceResult) -> int:
    if result.overall_status in {
        SchedulerOnceOverallStatus.RETRY_SCHEDULED,
        SchedulerOnceOverallStatus.EXECUTION_FAILED,
    }:
        return EXIT_EXECUTION
    return EXIT_OK


def scheduler_once_record(result: SchedulerOnceResult) -> dict:
    def encode(value):
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        raise TypeError("unsupported scheduler once value")

    return json.loads(json.dumps(asdict(result), default=encode, ensure_ascii=False))


def _overall_status(slot_results: tuple[SchedulerSlotResult, ...], *, dry_run: bool):
    if dry_run:
        return SchedulerOnceOverallStatus.DRY_RUN
    actions = {item.runtime_action for item in slot_results}
    if actions & {"FAILED_FINAL", "FINAL_FAILURE", "RETRY_EXHAUSTED"}:
        return SchedulerOnceOverallStatus.EXECUTION_FAILED
    if "FAILED_RETRYABLE" in actions:
        return SchedulerOnceOverallStatus.RETRY_SCHEDULED
    if "SUCCEEDED" in actions:
        return SchedulerOnceOverallStatus.SUCCESS
    return SchedulerOnceOverallStatus.NO_WORK
