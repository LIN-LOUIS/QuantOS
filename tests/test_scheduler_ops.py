"""Offline Phase 3D.3 operational adapter and calendar coverage tests."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import socket
from threading import Event, Lock

import pytest

from quantos.config import MARKET_TIMEZONE, Settings
from quantos.schemas.run import RunType
from quantos.schemas.schedule import ScheduleDecisionStatus
from quantos.schemas.scheduler_ops import (
    LOCAL_CALENDAR_SCHEMA_VERSION, LocalTradingCalendarArtifact,
    SchedulerOnceOverallStatus, SchedulerOnceResult, SchedulerOpsEventType,
)
from quantos.schemas.scheduler_runtime import (
    ClaimStatus, RetryPolicy, SchedulerExecutionOutcome,
    SchedulerExecutionResult, SchedulerInvocationStatus, SchedulerRuntimePolicy,
)
from quantos.scheduler_ops import (
    EXIT_EXECUTION, EXIT_OK, CalendarArtifactError, CalendarOutOfCoverageError,
    OperationalConfigError, QuantOSRunExecutorAdapter, calendar_artifact_record,
    load_local_calendar_artifact, run_scheduler_once, scheduler_once_exit_code,
    scheduler_once_record,
)
from quantos.scheduler_runtime import (
    DEFAULT_MISSED_RUN_POLICY, DEFAULT_SCHEDULER_RUNTIME_POLICY,
    schedule_instance_from_decision,
)
from quantos.scheduling import LocalTradingCalendar, evaluate_schedule
from quantos.storage.scheduler_runtime import SchedulerRuntimeRepository

DAY = date(2026, 9, 1)
PREVIOUS = date(2026, 8, 31)
TZ = MARKET_TIMEZONE


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("operational scheduler must not access network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def calendar_artifact():
    return LocalTradingCalendarArtifact(
        LOCAL_CALENDAR_SCHEMA_VERSION, "CN_A_SHARE", "Asia/Shanghai",
        PREVIOUS, date(2026, 9, 6),
        (PREVIOUS, DAY, date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)),
        datetime(2026, 8, 30, 12, tzinfo=TZ),
    )


@pytest.fixture
def repository(tmp_path):
    return SchedulerRuntimeRepository(Settings.from_project_root(tmp_path))


def at(hour, minute=0, second=0, *, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=TZ)


class FakeExecutor:
    def __init__(self, outcomes=(SchedulerExecutionOutcome.SUCCEEDED,)):
        self.outcomes = list(outcomes)
        self.calls = []
        self.lock = Lock()

    def __call__(self, context):
        with self.lock:
            self.calls.append(context)
            outcome = self.outcomes.pop(0)
        code = None if outcome == SchedulerExecutionOutcome.SUCCEEDED else (
            "TEMPORARY_FAILURE" if outcome == SchedulerExecutionOutcome.FAILED_RETRYABLE
            else "DETERMINISTIC_FAILURE"
        )
        return SchedulerExecutionResult(context.run_id, outcome, safe_error_code=code)


def run_once(calendar_artifact, repository, executor, value, **kwargs):
    return run_scheduler_once(
        evaluated_at=value, mode=kwargs.pop("mode", "research"),
        calendar_artifact=calendar_artifact, dry_run=kwargs.pop("dry_run", False),
        repository=repository, run_executor=executor, llm_allowed=False, **kwargs,
    )


def slot(result, slot_id):
    return next(item for item in result.slot_results if item.slot_id == slot_id)


def test_calendar_artifact_canonical_fields():
    assert tuple(field.name for field in fields(LocalTradingCalendarArtifact)) == (
        "schema_version", "market", "timezone", "coverage_start", "coverage_end",
        "trading_dates", "generated_at",
    )


@pytest.mark.parametrize("change,match", [
    ({"schema_version": "future"}, "schema"),
    ({"timezone": "UTC"}, "timezone"),
    ({"coverage_start": date(2026, 9, 7)}, "reversed"),
    ({"trading_dates": (DAY, DAY)}, "unique"),
    ({"trading_dates": ()}, "at least one"),
    ({"trading_dates": (date(2026, 8, 30),)}, "outside"),
    ({"generated_at": datetime(2026, 8, 30, 12)}, "timezone-aware"),
])
def test_calendar_artifact_rejects_invalid_contract(calendar_artifact, change, match):
    with pytest.raises(ValueError, match=match):
        replace(calendar_artifact, **change)


def test_calendar_artifact_json_roundtrip(tmp_path, calendar_artifact):
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(calendar_artifact_record(calendar_artifact)), encoding="utf-8")
    assert load_local_calendar_artifact(path) == calendar_artifact


@pytest.mark.parametrize("payload", [
    "not json",
    "{}",
    json.dumps({"schema_version": LOCAL_CALENDAR_SCHEMA_VERSION}),
])
def test_calendar_file_corruption_fails_closed(tmp_path, payload):
    path = tmp_path / "calendar.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(CalendarArtifactError, match="CALENDAR_ARTIFACT_INVALID"):
        load_local_calendar_artifact(path)


def test_inside_coverage_trading_day_executes(calendar_artifact, repository):
    executor = FakeExecutor()
    result = run_once(calendar_artifact, repository, executor, at(9))
    assert slot(result, "PRE_OPEN_0900").runtime_action == "SUCCEEDED"
    assert len(executor.calls) == 1


def test_inside_coverage_non_trading_day_has_no_runtime_state(calendar_artifact, repository):
    executor = FakeExecutor()
    result = run_once(calendar_artifact, repository, executor, at(9, day=date(2026, 9, 5)))
    assert all(item.schedule_decision == ScheduleDecisionStatus.NON_TRADING_DAY
               for item in result.slot_results)
    assert result.overall_status == SchedulerOnceOverallStatus.NO_WORK
    assert executor.calls == []
    assert repository.path.exists()
    import sqlite3
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM scheduler_instances").fetchone()[0] == 0


@pytest.mark.parametrize("value", [
    datetime(2026, 8, 30, 9, tzinfo=TZ),
    datetime(2026, 9, 7, 9, tzinfo=TZ),
])
def test_calendar_out_of_coverage_fails_before_runtime_mutation(
    calendar_artifact, repository, value,
):
    with pytest.raises(CalendarOutOfCoverageError, match="CALENDAR_OUT_OF_COVERAGE"):
        run_once(calendar_artifact, repository, FakeExecutor(), value)
    import sqlite3
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM scheduler_instances").fetchone()[0] == 0


def test_dry_run_has_zero_runtime_and_executor_side_effects(tmp_path, calendar_artifact):
    runtime_path = tmp_path / "absent" / "runtime.sqlite3"
    executor = FakeExecutor()
    result = run_scheduler_once(
        evaluated_at=at(9), mode="research", calendar_artifact=calendar_artifact,
        dry_run=True, repository=None, run_executor=executor,
    )
    assert result.overall_status == SchedulerOnceOverallStatus.DRY_RUN
    assert slot(result, "PRE_OPEN_0900").runtime_action == "ON_TIME_DUE"
    assert executor.calls == [] and not runtime_path.exists()
    assert all(item.schedule_instance_id is None for item in result.slot_results)


@pytest.mark.parametrize("value,slot_id,run_type", [
    (at(9), "PRE_OPEN_0900", RunType.PRE_OPEN),
    (at(10, 30), "INTRADAY_1030", RunType.INTRADAY),
    (at(16), "POST_CLOSE_1600", RunType.POST_CLOSE),
    (at(20), "POST_CLOSE_2000", RunType.POST_CLOSE),
])
def test_single_shot_due_slots_execute_once(
    calendar_artifact, repository, value, slot_id, run_type,
):
    executor = FakeExecutor()
    result = run_once(calendar_artifact, repository, executor, value)
    target = slot(result, slot_id)
    assert target.runtime_action == "SUCCEEDED"
    assert target.schedule_instance_id and target.run_id and target.attempt_number == 1
    assert executor.calls[0].run_type == run_type


def test_already_completed_is_normal_noop(calendar_artifact, repository):
    executor = FakeExecutor()
    first = run_once(calendar_artifact, repository, executor, at(9))
    second = run_once(calendar_artifact, repository, executor, at(9, 5))
    assert slot(first, "PRE_OPEN_0900").runtime_action == "SUCCEEDED"
    assert slot(second, "PRE_OPEN_0900").runtime_action == "ALREADY_COMPLETED"
    assert second.overall_status == SchedulerOnceOverallStatus.NO_WORK
    assert len(executor.calls) == 1 and scheduler_once_exit_code(second) == EXIT_OK


def test_active_claim_is_fast_normal_noop(calendar_artifact, repository):
    evaluation = evaluate_schedule(
        evaluated_at=at(9), calendar=LocalTradingCalendar(calendar_artifact.trading_dates),
    )
    decision = next(x for x in evaluation.decisions if x.slot_id == "PRE_OPEN_0900")
    instance = schedule_instance_from_decision(decision, mode="research")
    claim = repository.acquire_claim(
        instance, now=at(9), lease_duration=timedelta(hours=1),
        retry_policy=RetryPolicy(),
    )
    executor = FakeExecutor()
    result = run_once(calendar_artifact, repository, executor, at(9, 1))
    assert claim.status == ClaimStatus.CLAIM_ACQUIRED
    assert slot(result, "PRE_OPEN_0900").runtime_action == "CLAIM_ACTIVE"
    assert result.overall_status == SchedulerOnceOverallStatus.NO_WORK
    assert executor.calls == []


def test_1800_boot_executes_only_post_close_1600(calendar_artifact, repository):
    executor = FakeExecutor()
    result = run_once(calendar_artifact, repository, executor, at(18))
    assert [item.runtime_action for item in result.slot_results] == [
        "SKIPPED", "SKIPPED", "SUCCEEDED", "NOT_DUE",
    ]
    assert len(executor.calls) == 1


def test_2005_boot_executes_only_post_close_2000(calendar_artifact, repository):
    executor = FakeExecutor()
    result = run_once(calendar_artifact, repository, executor, at(20, 5))
    assert [item.runtime_action for item in result.slot_results] == [
        "SKIPPED", "SKIPPED", "SKIPPED", "SUCCEEDED",
    ]
    assert len(executor.calls) == 1


def test_2200_boot_catches_up_only_post_close_2000(calendar_artifact, repository):
    executor = FakeExecutor()
    result = run_once(calendar_artifact, repository, executor, at(22))
    assert [item.runtime_action for item in result.slot_results] == [
        "SKIPPED", "SKIPPED", "SKIPPED", "SUCCEEDED",
    ]
    assert len(executor.calls) == 1


def test_concurrent_single_shot_uses_runtime_claim_not_ops_lock(
    calendar_artifact, tmp_path,
):
    started = Event()
    release = Event()

    class BlockingExecutor(FakeExecutor):
        def __call__(self, context):
            with self.lock:
                self.calls.append(context)
            started.set()
            assert release.wait(timeout=5)
            return SchedulerExecutionResult(context.run_id, SchedulerExecutionOutcome.SUCCEEDED)

    executor = BlockingExecutor()
    settings = Settings.from_project_root(tmp_path)
    runtime_path = tmp_path / "runtime" / "scheduler.sqlite3"
    repositories = (
        SchedulerRuntimeRepository(settings, path=runtime_path),
        SchedulerRuntimeRepository(settings, path=runtime_path),
    )

    def invoke(index):
        return run_once(calendar_artifact, repositories[index], executor, at(9))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke, value) for value in range(2)]
        assert started.wait(timeout=5)
        release.set()
        results = [future.result(timeout=5) for future in futures]
    assert len(executor.calls) == 1
    actions = [slot(result, "PRE_OPEN_0900").runtime_action for result in results]
    assert actions.count("SUCCEEDED") == 1
    assert set(actions) <= {"SUCCEEDED", "CLAIM_ACTIVE", "ALREADY_COMPLETED"}


def test_retry_failure_returns_without_sleep_and_exact_due_retries(
    calendar_artifact, repository,
):
    policy = SchedulerRuntimePolicy(
        timedelta(hours=1), RetryPolicy(2, timedelta(minutes=5)),
        DEFAULT_MISSED_RUN_POLICY,
    )
    executor = FakeExecutor((SchedulerExecutionOutcome.FAILED_RETRYABLE,
                             SchedulerExecutionOutcome.SUCCEEDED))
    first = run_once(calendar_artifact, repository, executor, at(16), runtime_policy=policy)
    early = run_once(calendar_artifact, repository, executor, at(16, 4, 59), runtime_policy=policy)
    exact = run_once(calendar_artifact, repository, executor, at(16, 5), runtime_policy=policy)
    assert first.overall_status == SchedulerOnceOverallStatus.RETRY_SCHEDULED
    assert scheduler_once_exit_code(first) == EXIT_EXECUTION
    assert slot(early, "POST_CLOSE_1600").runtime_action == "RETRY_NOT_DUE"
    assert slot(exact, "POST_CLOSE_1600").runtime_action == "SUCCEEDED"
    assert len(executor.calls) == 2


def test_terminal_failure_is_not_reexecuted(calendar_artifact, repository):
    executor = FakeExecutor((SchedulerExecutionOutcome.FAILED_FINAL,
                             SchedulerExecutionOutcome.SUCCEEDED))
    first = run_once(calendar_artifact, repository, executor, at(9))
    second = run_once(calendar_artifact, repository, executor, at(9, 5))
    assert first.overall_status == SchedulerOnceOverallStatus.EXECUTION_FAILED
    assert slot(second, "PRE_OPEN_0900").runtime_action == "FINAL_FAILURE"
    assert len(executor.calls) == 1


def test_maximum_execution_window_must_be_below_lease(calendar_artifact):
    with pytest.raises(OperationalConfigError, match="SHORTER_THAN_LEASE"):
        run_scheduler_once(
            evaluated_at=at(9), mode="research", calendar_artifact=calendar_artifact,
            dry_run=True, maximum_execution_duration=timedelta(hours=1),
        )


@pytest.mark.parametrize("kwargs,match", [
    ({"top_n": 0}, "TOP_N"),
    ({"llm_allowed": 1}, "LLM_ALLOWED"),
    ({"universe_name": ""}, "UNIVERSE"),
])
def test_invalid_execution_config_fails_before_claim(
    calendar_artifact, repository, kwargs, match,
):
    with pytest.raises(OperationalConfigError, match=match):
        run_scheduler_once(
            evaluated_at=at(9), mode="research", calendar_artifact=calendar_artifact,
            dry_run=False, repository=repository, run_executor=FakeExecutor(), **kwargs,
        )
    import sqlite3
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM scheduler_instances").fetchone()[0] == 0


def test_result_roundtrip_record_has_safe_correlations(calendar_artifact, repository):
    result = run_once(calendar_artifact, repository, FakeExecutor(), at(9))
    record = scheduler_once_record(result)
    target = next(item for item in record["slot_results"] if item["slot_id"] == "PRE_OPEN_0900")
    assert record["schema_version"] == "quantos-scheduler-once-v1"
    assert target["schedule_instance_id"] and target["run_id"]
    assert json.dumps(record, sort_keys=True)


def test_observability_has_started_evaluated_execution_and_completed(
    calendar_artifact, repository,
):
    result = run_once(calendar_artifact, repository, FakeExecutor(), at(9))
    kinds = tuple(item.event_type for item in result.events)
    assert kinds[:3] == (
        SchedulerOpsEventType.SCHEDULER_ONCE_STARTED,
        SchedulerOpsEventType.CALENDAR_LOADED,
        SchedulerOpsEventType.SCHEDULE_EVALUATED,
    )
    assert SchedulerOpsEventType.EXECUTION_STARTED in kinds
    assert SchedulerOpsEventType.EXECUTION_FINISHED in kinds
    assert kinds[-1] == SchedulerOpsEventType.SCHEDULER_ONCE_COMPLETED


def test_no_work_observability_event(calendar_artifact, repository):
    result = run_once(calendar_artifact, repository, FakeExecutor(), at(8))
    assert result.overall_status == SchedulerOnceOverallStatus.NO_WORK
    assert result.events[-1].event_type == SchedulerOpsEventType.SCHEDULER_ONCE_NO_WORK


def test_raw_executor_exception_is_not_in_operational_result(calendar_artifact, repository):
    def broken(context):
        raise RuntimeError("Authorization Bearer raw-provider-secret")

    result = run_once(calendar_artifact, repository, broken, at(9))
    serialized = json.dumps(scheduler_once_record(result), sort_keys=True)
    assert result.overall_status == SchedulerOnceOverallStatus.EXECUTION_FAILED
    assert "EXECUTOR_EXCEPTION" in serialized
    assert "Authorization" not in serialized and "raw-provider-secret" not in serialized


def test_operational_adapter_reuses_existing_run_boundary(monkeypatch, tmp_path):
    import quantos.scheduler_ops as module

    context = evaluate_schedule(
        evaluated_at=at(9), calendar=LocalTradingCalendar((PREVIOUS, DAY)),
    ).intents[0]
    from quantos.scheduling import run_context_from_intent
    run_context = run_context_from_intent(
        context, mode="research", universe_name="u", top_n=1,
        llm_allowed=False, generated_at=at(9),
    )
    observed = []
    readiness = object()
    manifest = type("Manifest", (), {
        "summary": type("Summary", (), {"failed_modules": 0})(),
    })()
    path = tmp_path / "manifest.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module, "load_local_artifacts", lambda *args, **kwargs: observed.append("load") or ())
    monkeypatch.setattr(module, "collect_readiness", lambda **kwargs: observed.append("readiness") or readiness)
    monkeypatch.setattr(module, "local_artifact_handlers", lambda value: {})
    monkeypatch.setattr(module, "execute_run", lambda *args, **kwargs: observed.append("execute") or manifest)
    monkeypatch.setattr(module.RunRepository, "write", lambda self, value: observed.append("persist") or path)
    result = QuantOSRunExecutorAdapter(Settings.from_project_root(tmp_path))(run_context)
    assert observed == ["load", "readiness", "execute", "persist"]
    assert result.outcome == SchedulerExecutionOutcome.SUCCEEDED


def test_once_runner_does_not_reimplement_downstream_layers():
    import inspect
    from quantos.scheduler_ops import run_scheduler_once as target

    source = inspect.getsource(target)
    for forbidden in (
        "collect_readiness", "build_run_plan", "execute_run", "ReportType",
        "render_daily_report", "render_time_slice", "fund_flow", "attribution",
    ):
        assert forbidden not in source


def test_existing_phase_3d_contracts_remain_unchanged():
    from quantos.schemas.time_slice import ReportType
    from quantos.time_slices import INTRADAY_DISCLAIMER

    assert tuple(value.value for value in RunType) == ("PRE_OPEN", "INTRADAY", "POST_CLOSE")
    assert tuple(value.value for value in ReportType) == (
        "PRE_OPEN_BRIEF", "INTRADAY_BRIEF", "POST_CLOSE_REPORT",
    )
    assert DEFAULT_SCHEDULER_RUNTIME_POLICY.retry_policy == RetryPolicy()
    assert "未接入 canonical 盘中行情数据" in INTRADAY_DISCLAIMER


def test_no_python_daemon_or_exactly_once_claim_in_operational_source():
    source = Path("src/quantos/scheduler_ops.py").read_text(encoding="utf-8")
    assert "while True" not in source and "sleep(" not in source
    assert "EXACTLY_ONCE_BUSINESS_EXECUTION" not in source


def test_systemd_templates_are_oneshot_persistent_and_portable():
    service = Path("ops/systemd/quantos-scheduler.service").read_text(encoding="utf-8")
    timer = Path("ops/systemd/quantos-scheduler.timer").read_text(encoding="utf-8")
    assert "Type=oneshot" in service and "Restart=always" not in service
    assert "--mode strict_live" in service and "--no-llm" in service
    assert "Persistent=true" in timer and "OnCalendar=*-*-* *:00/5:00" in timer
    combined = service + timer
    assert re.search(r"(?:^|\s)/(?:home|Users)/[^/\s]+/", combined) is None
    assert "systemctl" not in combined and "sudo" not in combined and "cron" not in combined.lower()
    for marker in ("API_KEY", "Authorization", "Bearer", "TUSHARE_TOKEN"):
        assert marker not in combined


def test_exactly_once_limitation_is_documented():
    text = " ".join(Path("ops/systemd/README.md").read_text(encoding="utf-8").lower().split())
    assert "exactly-once business execution is not guaranteed" in text
    assert "heartbeat loop" in text
