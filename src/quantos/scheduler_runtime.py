"""Single-invocation scheduler lifecycle around an injected run executor."""

from datetime import datetime, time, timedelta
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from quantos.schemas._validation import require_aware
from quantos.schemas.run import RunContext, RunType
from quantos.schemas.schedule import (
    ScheduleDecision, ScheduleDecisionStatus, ScheduledRunIntent,
)
from quantos.schemas.scheduler_runtime import (
    ClaimStatus, MissedRunAction, MissedRunPolicy, MissedRunRule, RetryPolicy,
    RuntimeEligibility, RuntimeEligibilityStatus, ScheduleInstance,
    SchedulerExecutionOutcome, SchedulerExecutionResult, SchedulerInstanceStatus,
    SchedulerInvocationStatus, SchedulerRuntimePolicy, SchedulerRuntimeResult,
)
from quantos.scheduling import run_context_from_intent
from quantos.storage.scheduler_runtime import SchedulerRuntimeRepository


DEFAULT_MISSED_RUN_POLICY = MissedRunPolicy((
    MissedRunRule("PRE_OPEN_0900", MissedRunAction.SKIP),
    MissedRunRule("INTRADAY_1030", MissedRunAction.SKIP),
    MissedRunRule("POST_CLOSE_1600", MissedRunAction.CATCH_UP, time(20), 0),
    MissedRunRule("POST_CLOSE_2000", MissedRunAction.CATCH_UP, time(0), 1),
))
DEFAULT_SCHEDULER_RUNTIME_POLICY = SchedulerRuntimePolicy(
    lease_duration=timedelta(minutes=60),
    retry_policy=RetryPolicy(max_attempts=1, retry_delay=timedelta(0)),
    missed_run_policy=DEFAULT_MISSED_RUN_POLICY,
)


class RunExecutor(Protocol):
    def __call__(self, context: RunContext) -> SchedulerExecutionResult: ...


def schedule_instance_from_decision(decision: ScheduleDecision, *, mode: str) -> ScheduleInstance:
    """Build stable identity data; evaluated_at is intentionally excluded."""
    zone_name = getattr(decision.scheduled_for.tzinfo, "key", None)
    if not zone_name:
        raise ValueError("scheduled_for must use a named timezone")
    zone = ZoneInfo(zone_name)
    target = decision.scheduled_for.astimezone(zone).date()
    basis = (decision.intent.market_basis_trade_date if decision.intent is not None
             else target if decision.run_type == RunType.POST_CLOSE else None)
    return ScheduleInstance(
        slot_id=decision.slot_id, run_type=decision.run_type,
        scheduled_for=decision.scheduled_for, timezone=zone_name, mode=mode,
        target_trade_date=target, market_basis_trade_date=basis,
    )


def evaluate_runtime_eligibility(
    decision: ScheduleDecision, *, policy: SchedulerRuntimePolicy = DEFAULT_SCHEDULER_RUNTIME_POLICY,
) -> RuntimeEligibility:
    """Classify one decision without claiming, sleeping, executing or probing readiness."""
    if decision.status == ScheduleDecisionStatus.DUE:
        return RuntimeEligibility(RuntimeEligibilityStatus.ON_TIME_DUE)
    if decision.status == ScheduleDecisionStatus.NOT_DUE:
        return RuntimeEligibility(RuntimeEligibilityStatus.NOT_DUE)
    if decision.status == ScheduleDecisionStatus.NON_TRADING_DAY:
        return RuntimeEligibility(RuntimeEligibilityStatus.NON_TRADING_DAY)
    rule = policy.missed_run_policy.rule_for(decision.slot_id)
    if rule.action == MissedRunAction.SKIP:
        return RuntimeEligibility(RuntimeEligibilityStatus.SKIP, "MISSED_WINDOW_POLICY_SKIP")
    zone = ZoneInfo(getattr(decision.scheduled_for.tzinfo, "key", decision.scheduled_for.tzname()))
    local_day = decision.scheduled_for.astimezone(zone).date()
    deadline = datetime.combine(
        local_day + timedelta(days=rule.recovery_deadline_day_offset),
        rule.recovery_deadline_local_time,
        zone,
    )
    if decision.evaluated_at.astimezone(zone) < deadline:
        return RuntimeEligibility(RuntimeEligibilityStatus.CATCH_UP_DUE)
    return RuntimeEligibility(RuntimeEligibilityStatus.SKIP, "RECOVERY_DEADLINE_REACHED")


class SchedulerRuntime:
    """One external invocation: claim, bind, execute once, and fenced finalize."""

    def __init__(
        self, repository: SchedulerRuntimeRepository, run_executor: RunExecutor, *,
        policy: SchedulerRuntimePolicy = DEFAULT_SCHEDULER_RUNTIME_POLICY,
        clock: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.run_executor = run_executor
        self.policy = policy
        self.clock = clock

    def invoke(
        self, decision: ScheduleDecision, *, mode: str, universe_name: str,
        top_n: int, llm_allowed: bool,
    ) -> SchedulerRuntimeResult:
        instance = schedule_instance_from_decision(decision, mode=mode)
        existing = self.repository.get_instance(instance.schedule_instance_id)
        eligibility = evaluate_runtime_eligibility(decision, policy=self.policy)
        if existing is None:
            if eligibility.status == RuntimeEligibilityStatus.SKIP:
                record = self.repository.record_skip(
                    instance, evaluated_at=decision.evaluated_at,
                    skip_reason=eligibility.skip_reason,
                )
                return SchedulerRuntimeResult(
                    SchedulerInvocationStatus.SKIPPED, instance.schedule_instance_id,
                    eligibility.status, None, None, None, record,
                )
            if eligibility.status in {
                RuntimeEligibilityStatus.NOT_DUE, RuntimeEligibilityStatus.NON_TRADING_DAY,
            }:
                status = (SchedulerInvocationStatus.NOT_DUE
                          if eligibility.status == RuntimeEligibilityStatus.NOT_DUE
                          else SchedulerInvocationStatus.NON_TRADING_DAY)
                return SchedulerRuntimeResult(
                    status, instance.schedule_instance_id, eligibility.status,
                    None, None, None, None,
                )
        else:
            instance = existing.instance
            if existing.status == SchedulerInstanceStatus.FAILED_RETRYABLE:
                retry_status = (
                    RuntimeEligibilityStatus.RETRY_DUE
                    if decision.evaluated_at >= existing.next_retry_at
                    else RuntimeEligibilityStatus.RETRY_NOT_DUE
                )
                eligibility = RuntimeEligibility(retry_status)
            elif existing.status == SchedulerInstanceStatus.CLAIMED:
                active = self.repository.list_attempts(instance.schedule_instance_id)[-1]
                claim_status = (
                    RuntimeEligibilityStatus.CLAIM_ACTIVE
                    if decision.evaluated_at < active.lease_expires_at
                    else RuntimeEligibilityStatus.STALE_RECOVERY_DUE
                )
                eligibility = RuntimeEligibility(claim_status)
        claim = self.repository.acquire_claim(
            instance, now=decision.evaluated_at,
            lease_duration=self.policy.lease_duration,
            retry_policy=self.policy.retry_policy,
        )
        if claim.status != ClaimStatus.CLAIM_ACQUIRED:
            mapped = {
                ClaimStatus.ALREADY_CLAIMED: SchedulerInvocationStatus.ALREADY_CLAIMED,
                ClaimStatus.ALREADY_COMPLETED: SchedulerInvocationStatus.ALREADY_COMPLETED,
                ClaimStatus.FINAL_FAILURE: SchedulerInvocationStatus.FINAL_FAILURE,
                ClaimStatus.RETRY_NOT_DUE: SchedulerInvocationStatus.RETRY_NOT_DUE,
                ClaimStatus.RETRY_EXHAUSTED: SchedulerInvocationStatus.RETRY_EXHAUSTED,
                ClaimStatus.ALREADY_SKIPPED: SchedulerInvocationStatus.ALREADY_SKIPPED,
            }
            return SchedulerRuntimeResult(
                mapped[claim.status], instance.schedule_instance_id, eligibility.status,
                claim.status, None, None, claim.instance_record,
            )
        attempt = claim.attempt
        started_at = self._clock(decision.evaluated_at)
        intent = ScheduledRunIntent(
            instance.slot_id, instance.run_type, instance.scheduled_for,
            decision.evaluated_at, instance.target_trade_date,
            instance.market_basis_trade_date, instance.timezone,
        )
        context = run_context_from_intent(
            intent, mode=instance.mode, universe_name=universe_name, top_n=top_n,
            llm_allowed=llm_allowed, generated_at=started_at,
        )
        attempt = self.repository.bind_run(
            instance.schedule_instance_id, claim_id=attempt.claim_id,
            fencing_token=attempt.fencing_token, run_id=context.run_id,
            started_at=started_at,
        )
        try:
            execution = self.run_executor(context)
            if not isinstance(execution, SchedulerExecutionResult) or execution.run_id != context.run_id:
                execution = SchedulerExecutionResult(
                    context.run_id, SchedulerExecutionOutcome.FAILED_FINAL,
                    safe_error_code="INVALID_EXECUTOR_RESULT",
                )
        except Exception:
            execution = SchedulerExecutionResult(
                context.run_id, SchedulerExecutionOutcome.FAILED_FINAL,
                safe_error_code="EXECUTOR_EXCEPTION",
            )
        finished_at = self._clock(started_at)
        record = self.repository.finalize_attempt(
            instance.schedule_instance_id, claim_id=attempt.claim_id,
            fencing_token=attempt.fencing_token, result=execution,
            finished_at=finished_at, retry_policy=self.policy.retry_policy,
        )
        attempt = self.repository.list_attempts(instance.schedule_instance_id)[-1]
        mapped = {
            SchedulerInstanceStatus.SUCCEEDED: SchedulerInvocationStatus.SUCCEEDED,
            SchedulerInstanceStatus.FAILED_RETRYABLE: SchedulerInvocationStatus.FAILED_RETRYABLE,
            SchedulerInstanceStatus.FAILED_FINAL: SchedulerInvocationStatus.FAILED_FINAL,
            SchedulerInstanceStatus.RETRY_EXHAUSTED: SchedulerInvocationStatus.RETRY_EXHAUSTED,
        }
        return SchedulerRuntimeResult(
            mapped[record.status], instance.schedule_instance_id, eligibility.status,
            ClaimStatus.CLAIM_ACQUIRED, context, attempt, record,
        )

    def _clock(self, fallback: datetime) -> datetime:
        value = self.clock() if self.clock is not None else fallback
        require_aware(value, "runtime clock")
        return value
