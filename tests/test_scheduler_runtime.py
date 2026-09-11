"""Phase 3D.2 lifecycle and recovery tests with an injected fake executor."""

from dataclasses import fields, replace
from datetime import date, datetime, time, timedelta
from concurrent.futures import ThreadPoolExecutor
import inspect
import socket
from threading import Event

import pytest

from quantos.config import MARKET_TIMEZONE, Settings
from quantos.schemas.run import ArtifactReference, RunType
from quantos.schemas.schedule import ScheduleDecisionStatus
from quantos.schemas.scheduler_runtime import (
    ClaimStatus, MissedRunAction, MissedRunPolicy, MissedRunRule, RetryPolicy,
    RuntimeEligibilityStatus as E, ScheduleInstance, SchedulerExecutionOutcome as O,
    SchedulerExecutionResult, SchedulerInstanceStatus, SchedulerInvocationStatus as I,
    SchedulerRuntimePolicy,
)
from quantos.scheduler_runtime import (
    DEFAULT_MISSED_RUN_POLICY, DEFAULT_SCHEDULER_RUNTIME_POLICY, SchedulerRuntime,
    evaluate_runtime_eligibility, schedule_instance_from_decision,
)
from quantos.scheduling import LocalTradingCalendar, evaluate_schedule
from quantos.storage.scheduler_runtime import SchedulerRuntimeRepository

DAY = date(2026, 9, 1)
PREVIOUS = date(2026, 8, 31)
NEXT = date(2026, 9, 2)
CALENDAR = LocalTradingCalendar((PREVIOUS, DAY, NEXT))
TZ = MARKET_TIMEZONE


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("scheduler runtime must not access network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def repository(tmp_path):
    return SchedulerRuntimeRepository(Settings.from_project_root(tmp_path))


def at(hour, minute=0, second=0, *, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=TZ)


def decision(value, slot_id):
    evaluation = evaluate_schedule(evaluated_at=value, calendar=CALENDAR)
    return next(item for item in evaluation.decisions if item.slot_id == slot_id)


def runtime_policy(*, attempts=1, delay=timedelta(0), lease=timedelta(hours=1)):
    return SchedulerRuntimePolicy(lease, RetryPolicy(attempts, delay), DEFAULT_MISSED_RUN_POLICY)


class FakeExecutor:
    def __init__(self, outcomes=(O.SUCCEEDED,), *, before=None, reference=False):
        self.outcomes = list(outcomes)
        self.calls = []
        self.before = before
        self.reference = reference

    def __call__(self, context):
        self.calls.append(context)
        if self.before:
            self.before(context)
        outcome = self.outcomes.pop(0)
        code = None if outcome == O.SUCCEEDED else "FAKE_RETRYABLE" if outcome == O.FAILED_RETRYABLE else "FAKE_FINAL"
        reference = (ArtifactReference("a" * 64, "/tmp/run-manifest.json", "quantos-run-v1")
                     if self.reference else None)
        return SchedulerExecutionResult(context.run_id, outcome, reference, code)


def invoke(repository, executor, value, slot_id, *, mode="research", policy=DEFAULT_SCHEDULER_RUNTIME_POLICY):
    return SchedulerRuntime(repository, executor, policy=policy).invoke(
        decision(value, slot_id), mode=mode, universe_name="shse_szse_a_share",
        top_n=20, llm_allowed=False,
    )


def test_schedule_instance_canonical_fields():
    assert tuple(field.name for field in fields(ScheduleInstance)) == (
        "slot_id", "run_type", "scheduled_for", "timezone", "mode",
        "target_trade_date", "market_basis_trade_date", "schema_version",
    )


def test_schedule_instance_id_is_sha256_and_excludes_evaluated_at():
    first = schedule_instance_from_decision(decision(at(9), "PRE_OPEN_0900"), mode="research")
    later = schedule_instance_from_decision(decision(at(9, 10), "PRE_OPEN_0900"), mode="research")
    assert first.schedule_instance_id == later.schedule_instance_id
    assert len(first.schedule_instance_id) == 64
    assert not hasattr(first, "evaluated_at")


def test_mode_is_part_of_schedule_instance_identity():
    item = decision(at(9), "PRE_OPEN_0900")
    research = schedule_instance_from_decision(item, mode="research")
    strict = schedule_instance_from_decision(item, mode="strict_live")
    assert research.schedule_instance_id != strict.schedule_instance_id
    assert (research.slot_id, research.scheduled_for, research.run_type) == (
        strict.slot_id, strict.scheduled_for, strict.run_type,
    )


def test_catch_up_and_on_time_share_schedule_instance_identity():
    due = schedule_instance_from_decision(decision(at(16, 5), "POST_CLOSE_1600"), mode="research")
    catch_up = schedule_instance_from_decision(decision(at(18), "POST_CLOSE_1600"), mode="research")
    assert due.schedule_instance_id == catch_up.schedule_instance_id


@pytest.mark.parametrize("change", ["slot", "run_type", "scheduled", "timezone", "mode", "target"])
def test_canonical_identity_inputs_change_instance_id(change):
    value = schedule_instance_from_decision(decision(at(16), "POST_CLOSE_1600"), mode="research")
    if change == "slot":
        changed = replace(value, slot_id="OTHER")
    elif change == "run_type":
        changed = replace(value, run_type=RunType.INTRADAY,
                          market_basis_trade_date=PREVIOUS)
    elif change == "scheduled":
        changed = replace(value, scheduled_for=at(16, 1))
    elif change == "timezone":
        changed = replace(value, timezone="UTC",
                          target_trade_date=value.scheduled_for.astimezone(__import__("zoneinfo").ZoneInfo("UTC")).date())
    elif change == "mode":
        changed = replace(value, mode="strict_live")
    else:
        changed = replace(value, scheduled_for=at(16, day=NEXT), target_trade_date=NEXT,
                          market_basis_trade_date=NEXT)
    assert value.schedule_instance_id != changed.schedule_instance_id


def test_schedule_run_and_report_id_are_separate_concepts(repository):
    executor = FakeExecutor()
    result = invoke(repository, executor, at(9), "PRE_OPEN_0900")
    assert result.schedule_instance_id != result.run_context.run_id
    assert not hasattr(result, "report_id") and not hasattr(result.attempt, "report_id")


def test_default_runtime_policy_is_conservative():
    policy = DEFAULT_SCHEDULER_RUNTIME_POLICY
    assert policy.lease_duration == timedelta(hours=1)
    assert policy.retry_policy == RetryPolicy(1, timedelta(0))


@pytest.mark.parametrize("attempts,delay", [(0, timedelta(0)), (1, timedelta(seconds=-1))])
def test_retry_policy_rejects_invalid_values(attempts, delay):
    with pytest.raises(ValueError):
        RetryPolicy(attempts, delay)


def test_runtime_policy_rejects_non_positive_lease():
    with pytest.raises(ValueError, match="lease"):
        replace(DEFAULT_SCHEDULER_RUNTIME_POLICY, lease_duration=timedelta(0))


def test_default_missed_policy_is_explicit_for_all_slots():
    assert {rule.slot_id: rule.action for rule in DEFAULT_MISSED_RUN_POLICY.rules} == {
        "PRE_OPEN_0900": MissedRunAction.SKIP,
        "INTRADAY_1030": MissedRunAction.SKIP,
        "POST_CLOSE_1600": MissedRunAction.CATCH_UP,
        "POST_CLOSE_2000": MissedRunAction.CATCH_UP,
    }


def test_duplicate_or_missing_missed_rule_fails_closed():
    rule = MissedRunRule("PRE_OPEN_0900", MissedRunAction.SKIP)
    with pytest.raises(ValueError, match="duplicate"):
        MissedRunPolicy((rule, rule))
    with pytest.raises(ValueError, match="missing"):
        MissedRunPolicy((rule,)).rule_for("OTHER")


def test_first_claim_executes_once_and_completes(repository):
    executor = FakeExecutor()
    result = invoke(repository, executor, at(9), "PRE_OPEN_0900")
    assert result.claim_status == ClaimStatus.CLAIM_ACQUIRED
    assert result.status == I.SUCCEEDED
    assert result.instance_record.status == SchedulerInstanceStatus.SUCCEEDED
    assert len(executor.calls) == 1


def test_duplicate_completion_never_calls_executor_again(repository):
    executor = FakeExecutor()
    first = invoke(repository, executor, at(9), "PRE_OPEN_0900")
    second = invoke(repository, executor, at(9, 10), "PRE_OPEN_0900")
    assert first.status == I.SUCCEEDED and second.status == I.ALREADY_COMPLETED
    assert len(executor.calls) == 1


def test_research_and_strict_instances_execute_independently(repository):
    executor = FakeExecutor((O.SUCCEEDED, O.SUCCEEDED))
    research = invoke(repository, executor, at(9), "PRE_OPEN_0900", mode="research")
    strict = invoke(repository, executor, at(9), "PRE_OPEN_0900", mode="strict_live")
    assert research.schedule_instance_id != strict.schedule_instance_id
    assert len(executor.calls) == 2


def test_run_is_bound_before_executor_call(repository):
    seen = []

    def inspect_bound(context):
        attempts = repository.list_attempts(
            schedule_instance_from_decision(decision(at(9), "PRE_OPEN_0900"), mode="research").schedule_instance_id,
        )
        seen.append(attempts[-1])
        assert attempts[-1].run_id == context.run_id
        assert attempts[-1].status.value == "RUNNING"

    executor = FakeExecutor(before=inspect_bound)
    invoke(repository, executor, at(9), "PRE_OPEN_0900")
    assert len(seen) == 1


def test_executor_manifest_reference_is_recorded_without_parsing(repository):
    result = invoke(repository, FakeExecutor(reference=True), at(9), "PRE_OPEN_0900")
    assert result.attempt.manifest_ref.path == "/tmp/run-manifest.json"


def test_executor_exception_becomes_safe_final_failure(repository):
    class Broken:
        calls = 0

        def __call__(self, context):
            self.calls += 1
            raise RuntimeError("Authorization Bearer raw-provider-secret")

    executor = Broken()
    result = invoke(repository, executor, at(9), "PRE_OPEN_0900")
    assert result.status == I.FAILED_FINAL
    assert result.attempt.safe_error_code == "EXECUTOR_EXCEPTION"
    assert b"Authorization" not in repository.path.read_bytes()


def test_invalid_executor_result_fails_final_without_payload_persistence(repository):
    result = invoke(repository, lambda context: {"raw": "secret"}, at(9), "PRE_OPEN_0900")
    assert result.status == I.FAILED_FINAL
    assert result.attempt.safe_error_code == "INVALID_EXECUTOR_RESULT"
    assert b'"raw"' not in repository.path.read_bytes()


def test_explicit_failed_final_never_retries(repository):
    executor = FakeExecutor((O.FAILED_FINAL, O.SUCCEEDED))
    first = invoke(repository, executor, at(9), "PRE_OPEN_0900",
                   policy=runtime_policy(attempts=3))
    second = invoke(repository, executor, at(9, 5), "PRE_OPEN_0900",
                    policy=runtime_policy(attempts=3))
    assert first.status == I.FAILED_FINAL and second.status == I.FINAL_FAILURE
    assert len(executor.calls) == 1


def test_default_retryable_failure_is_immediately_exhausted(repository):
    executor = FakeExecutor((O.FAILED_RETRYABLE, O.SUCCEEDED))
    first = invoke(repository, executor, at(9), "PRE_OPEN_0900")
    second = invoke(repository, executor, at(9, 5), "PRE_OPEN_0900")
    assert first.status == second.status == I.RETRY_EXHAUSTED
    assert len(executor.calls) == 1


def test_retry_delay_exact_boundary_and_new_run_identity(repository):
    policy = runtime_policy(attempts=2, delay=timedelta(minutes=5))
    executor = FakeExecutor((O.FAILED_RETRYABLE, O.SUCCEEDED))
    first = invoke(repository, executor, at(16, 10), "POST_CLOSE_1600", policy=policy)
    early = invoke(repository, executor, at(16, 14, 59), "POST_CLOSE_1600", policy=policy)
    exact = invoke(repository, executor, at(16, 15), "POST_CLOSE_1600", policy=policy)
    assert first.status == I.FAILED_RETRYABLE
    assert early.status == I.RETRY_NOT_DUE
    assert early.eligibility == E.RETRY_NOT_DUE
    assert exact.status == I.SUCCEEDED
    assert exact.eligibility == E.RETRY_DUE
    assert first.schedule_instance_id == exact.schedule_instance_id
    assert first.run_context.run_id != exact.run_context.run_id
    assert first.attempt.attempt_id != exact.attempt.attempt_id
    assert first.attempt.claim_id != exact.attempt.claim_id
    assert exact.attempt.fencing_token == 2
    assert len(executor.calls) == 2


def test_missed_catch_up_failure_enters_retry_lifecycle(repository):
    policy = runtime_policy(attempts=2, delay=timedelta(minutes=5))
    executor = FakeExecutor((O.FAILED_RETRYABLE, O.SUCCEEDED))
    first = invoke(repository, executor, at(18), "POST_CLOSE_1600", policy=policy)
    early = invoke(repository, executor, at(18, 4, 59), "POST_CLOSE_1600", policy=policy)
    retry = invoke(repository, executor, at(18, 5), "POST_CLOSE_1600", policy=policy)
    assert first.status == I.FAILED_RETRYABLE
    assert first.eligibility == E.CATCH_UP_DUE
    assert early.status == I.RETRY_NOT_DUE and early.eligibility == E.RETRY_NOT_DUE
    assert retry.status == I.SUCCEEDED and retry.eligibility == E.RETRY_DUE
    assert first.schedule_instance_id == retry.schedule_instance_id
    assert len(executor.calls) == 2


def test_retry_policy_cannot_enable_first_missed_pre_open_attempt(repository):
    executor = FakeExecutor()
    result = invoke(
        repository, executor, at(10), "PRE_OPEN_0900",
        policy=runtime_policy(attempts=3, delay=timedelta(minutes=5)),
    )
    assert result.status == I.SKIPPED
    assert result.run_context is None
    assert executor.calls == []


def test_retry_exhaustion_prevents_third_executor_call(repository):
    policy = runtime_policy(attempts=2, delay=timedelta(minutes=5))
    executor = FakeExecutor((O.FAILED_RETRYABLE, O.FAILED_RETRYABLE, O.SUCCEEDED))
    first = invoke(repository, executor, at(16), "POST_CLOSE_1600", policy=policy)
    second = invoke(repository, executor, at(16, 5), "POST_CLOSE_1600", policy=policy)
    third = invoke(repository, executor, at(18), "POST_CLOSE_1600", policy=policy)
    assert first.status == I.FAILED_RETRYABLE
    assert second.status == third.status == I.RETRY_EXHAUSTED
    assert len(executor.calls) == 2


@pytest.mark.parametrize("slot_id,value", [
    ("PRE_OPEN_0900", at(10)),
    ("INTRADAY_1030", at(12)),
])
def test_missed_preopen_and_intraday_are_persisted_skips(repository, slot_id, value):
    executor = FakeExecutor()
    result = invoke(repository, executor, value, slot_id)
    assert result.status == I.SKIPPED
    assert result.eligibility == E.SKIP
    assert result.run_context is result.attempt is None
    assert result.instance_record.status == SchedulerInstanceStatus.SKIPPED
    assert result.instance_record.skip_reason == "MISSED_WINDOW_POLICY_SKIP"
    assert executor.calls == []


def test_repeated_missed_skip_is_stable_and_does_not_execute(repository):
    executor = FakeExecutor()
    first = invoke(repository, executor, at(10), "PRE_OPEN_0900")
    second = invoke(repository, executor, at(10, 1), "PRE_OPEN_0900")
    assert first.status == I.SKIPPED and second.status == I.ALREADY_SKIPPED
    assert executor.calls == []


def test_not_due_or_non_trading_creates_no_runtime_record(repository):
    executor = FakeExecutor()
    future = invoke(repository, executor, at(8), "PRE_OPEN_0900")
    closed_decision = decision(at(9, day=NEXT), "PRE_OPEN_0900")
    closed = SchedulerRuntime(repository, executor).invoke(
        replace(closed_decision, status=ScheduleDecisionStatus.NON_TRADING_DAY, intent=None),
        mode="strict_live", universe_name="u", top_n=1, llm_allowed=False,
    )
    assert future.status == I.NOT_DUE and closed.status == I.NON_TRADING_DAY
    assert repository.get_instance(future.schedule_instance_id) is None
    assert repository.get_instance(closed.schedule_instance_id) is None


def test_post_close_1600_catch_up_at_1800(repository):
    executor = FakeExecutor()
    result = invoke(repository, executor, at(18), "POST_CLOSE_1600")
    assert result.status == I.SUCCEEDED and result.eligibility == E.CATCH_UP_DUE
    assert result.attempt.claimed_at == at(18)
    assert result.run_context.as_of_time == at(18)
    assert result.instance_record.instance.scheduled_for == at(16)


def test_post_close_1600_exact_2000_deadline_skips(repository):
    executor = FakeExecutor()
    result = invoke(repository, executor, at(20), "POST_CLOSE_1600")
    assert result.status == I.SKIPPED
    assert result.instance_record.skip_reason == "RECOVERY_DEADLINE_REACHED"
    assert decision(at(20), "POST_CLOSE_2000").status == ScheduleDecisionStatus.DUE
    assert executor.calls == []


@pytest.mark.parametrize("value,status", [
    (at(22), E.CATCH_UP_DUE),
    (at(23, 59, 59), E.CATCH_UP_DUE),
])
def test_post_close_2000_catches_up_before_midnight(value, status):
    item = decision(value, "POST_CLOSE_2000")
    assert evaluate_runtime_eligibility(item).status == status


def test_post_close_2000_exact_midnight_stops_catch_up():
    item = decision(at(23, 59, 59), "POST_CLOSE_2000")
    midnight = replace(item, evaluated_at=at(0, day=NEXT))
    eligibility = evaluate_runtime_eligibility(midnight)
    assert eligibility.status == E.SKIP
    assert eligibility.skip_reason == "RECOVERY_DEADLINE_REACHED"


def test_runtime_clock_records_distinct_claim_start_and_finish(repository):
    times = iter((at(9, 0, 1), at(9, 0, 2)))
    runtime = SchedulerRuntime(repository, FakeExecutor(), clock=lambda: next(times))
    result = runtime.invoke(decision(at(9), "PRE_OPEN_0900"), mode="research",
                            universe_name="u", top_n=1, llm_allowed=False)
    assert result.attempt.claimed_at == at(9)
    assert result.attempt.started_at == at(9, 0, 1)
    assert result.attempt.finished_at == at(9, 0, 2)


def test_runtime_constructs_run_context_only_after_claim(repository):
    first = repository.acquire_claim(
        schedule_instance_from_decision(decision(at(9), "PRE_OPEN_0900"), mode="research"),
        now=at(9), lease_duration=timedelta(hours=1), retry_policy=RetryPolicy(),
    )
    executor = FakeExecutor()
    result = invoke(repository, executor, at(9, 1), "PRE_OPEN_0900")
    assert first.status == ClaimStatus.CLAIM_ACQUIRED
    assert result.status == I.ALREADY_CLAIMED
    assert result.eligibility == E.CLAIM_ACTIVE
    assert result.run_context is None and executor.calls == []


def test_eight_concurrent_runtime_invocations_execute_once(repository):
    started = Event()
    release = Event()

    class BlockingExecutor(FakeExecutor):
        def __call__(self, context):
            self.calls.append(context)
            started.set()
            assert release.wait(timeout=5)
            return SchedulerExecutionResult(context.run_id, O.SUCCEEDED)

    executor = BlockingExecutor()
    runtime = SchedulerRuntime(repository, executor)
    item = decision(at(9), "PRE_OPEN_0900")

    def call(_):
        return runtime.invoke(
            item, mode="research", universe_name="u", top_n=1, llm_allowed=False,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(call, index) for index in range(8)]
        assert started.wait(timeout=5)
        release.set()
        results = [future.result(timeout=5) for future in futures]
    assert len(executor.calls) == 1
    assert sum(result.status == I.SUCCEEDED for result in results) == 1
    assert all(result.status in {I.SUCCEEDED, I.ALREADY_CLAIMED, I.ALREADY_COMPLETED}
               for result in results)


def test_crash_after_bind_can_be_taken_over_but_old_owner_cannot_finalize(repository):
    value = schedule_instance_from_decision(decision(at(9), "PRE_OPEN_0900"), mode="research")
    first = repository.acquire_claim(value, now=at(9), lease_duration=timedelta(hours=1),
                                     retry_policy=RetryPolicy())
    repository.bind_run(value.schedule_instance_id, claim_id=first.attempt.claim_id,
                        fencing_token=first.attempt.fencing_token, run_id="1" * 64,
                        started_at=at(9))
    second = repository.acquire_claim(value, now=at(10), lease_duration=timedelta(hours=1),
                                      retry_policy=RetryPolicy())
    assert second.status == ClaimStatus.CLAIM_ACQUIRED
    assert second.attempt.fencing_token == 2


def test_scheduler_runtime_has_no_readiness_planner_product_or_provider_imports():
    import quantos.scheduler_runtime as runtime_module

    source = inspect.getsource(runtime_module)
    for forbidden in (
        "ReadinessSnapshot", "collect_readiness", "build_run_plan", "execute_run",
        "QuantOSRunManifest", "ReportType", "render_daily_report", "render_time_slice",
        "quantos.collectors", "quantos.fundflow", "quantos.discovery",
    ):
        assert forbidden not in source


def test_existing_run_report_schedule_and_intraday_semantics_unchanged():
    from quantos.schemas.schedule import ScheduleDecisionStatus as ExistingDecisionStatus
    from quantos.schemas.time_slice import ReportType
    from quantos.time_slices import INTRADAY_DISCLAIMER

    assert tuple(x.value for x in RunType) == ("PRE_OPEN", "INTRADAY", "POST_CLOSE")
    assert tuple(x.value for x in ReportType) == (
        "PRE_OPEN_BRIEF", "INTRADAY_BRIEF", "POST_CLOSE_REPORT",
    )
    assert tuple(x.value for x in ExistingDecisionStatus) == (
        "DUE", "NOT_DUE", "MISSED_WINDOW", "NON_TRADING_DAY",
    )
    assert "未接入 canonical 盘中行情数据" in INTRADAY_DISCLAIMER
