"""Closed scheduler-runtime contracts for claims, attempts, retry and recovery."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
import hashlib
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ._validation import require_aware, require_non_empty
from .run import ArtifactReference, RunContext, RunType

SCHEDULER_RUNTIME_SCHEMA_VERSION = "quantos-scheduler-runtime-v1"
SCHEDULER_RUNTIME_POLICY_VERSION = "quantos-scheduler-runtime-policy-v1"
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class SchedulerInstanceStatus(str, Enum):
    CLAIMED = "CLAIMED"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    SKIPPED = "SKIPPED"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"


class SchedulerAttemptStatus(str, Enum):
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    STALE = "STALE"


class SchedulerExecutionOutcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"


class ClaimStatus(str, Enum):
    CLAIM_ACQUIRED = "CLAIM_ACQUIRED"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"
    FINAL_FAILURE = "FINAL_FAILURE"
    RETRY_NOT_DUE = "RETRY_NOT_DUE"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    ALREADY_SKIPPED = "ALREADY_SKIPPED"


class RuntimeEligibilityStatus(str, Enum):
    ON_TIME_DUE = "ON_TIME_DUE"
    CATCH_UP_DUE = "CATCH_UP_DUE"
    RETRY_DUE = "RETRY_DUE"
    RETRY_NOT_DUE = "RETRY_NOT_DUE"
    CLAIM_ACTIVE = "CLAIM_ACTIVE"
    STALE_RECOVERY_DUE = "STALE_RECOVERY_DUE"
    SKIP = "SKIP"
    NOT_DUE = "NOT_DUE"
    NON_TRADING_DAY = "NON_TRADING_DAY"


class SchedulerInvocationStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    SKIPPED = "SKIPPED"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"
    FINAL_FAILURE = "FINAL_FAILURE"
    RETRY_NOT_DUE = "RETRY_NOT_DUE"
    ALREADY_SKIPPED = "ALREADY_SKIPPED"
    NOT_DUE = "NOT_DUE"
    NON_TRADING_DAY = "NON_TRADING_DAY"


class MissedRunAction(str, Enum):
    SKIP = "SKIP"
    CATCH_UP = "CATCH_UP"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1
    retry_delay: timedelta = timedelta(0)

    def __post_init__(self):
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if type(self.retry_delay) is not timedelta or self.retry_delay < timedelta(0):
            raise ValueError("retry_delay must be non-negative")


@dataclass(frozen=True, slots=True)
class MissedRunRule:
    slot_id: str
    action: MissedRunAction
    recovery_deadline_local_time: time | None = None
    recovery_deadline_day_offset: int = 0

    def __post_init__(self):
        require_non_empty(self.slot_id, "slot_id")
        if not isinstance(self.action, MissedRunAction):
            raise ValueError("explicit missed-run action required")
        if self.action == MissedRunAction.SKIP:
            if self.recovery_deadline_local_time is not None or self.recovery_deadline_day_offset != 0:
                raise ValueError("SKIP rule cannot have a recovery deadline")
        else:
            if (type(self.recovery_deadline_local_time) is not time
                or self.recovery_deadline_local_time.tzinfo is not None):
                raise ValueError("CATCH_UP requires a naive local deadline")
            if type(self.recovery_deadline_day_offset) is not int or self.recovery_deadline_day_offset not in {0, 1}:
                raise ValueError("recovery deadline day offset must be zero or one")


@dataclass(frozen=True, slots=True)
class MissedRunPolicy:
    rules: tuple[MissedRunRule, ...]

    def __post_init__(self):
        if type(self.rules) is not tuple or not self.rules:
            raise ValueError("missed-run policy requires rules")
        identifiers = [rule.slot_id for rule in self.rules]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("duplicate missed-run slot rule")

    def rule_for(self, slot_id: str) -> MissedRunRule:
        matches = [rule for rule in self.rules if rule.slot_id == slot_id]
        if len(matches) != 1:
            raise ValueError("missing missed-run rule")
        return matches[0]


@dataclass(frozen=True, slots=True)
class SchedulerRuntimePolicy:
    lease_duration: timedelta
    retry_policy: RetryPolicy
    missed_run_policy: MissedRunPolicy
    policy_version: str = SCHEDULER_RUNTIME_POLICY_VERSION

    def __post_init__(self):
        if type(self.lease_duration) is not timedelta or self.lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if not isinstance(self.retry_policy, RetryPolicy) or not isinstance(self.missed_run_policy, MissedRunPolicy):
            raise ValueError("explicit runtime policies required")
        if self.policy_version != SCHEDULER_RUNTIME_POLICY_VERSION:
            raise ValueError("unsupported scheduler runtime policy")


@dataclass(frozen=True, slots=True)
class ScheduleInstance:
    slot_id: str
    run_type: RunType
    scheduled_for: datetime
    timezone: str
    mode: str
    target_trade_date: date
    market_basis_trade_date: date | None
    schema_version: str = SCHEDULER_RUNTIME_SCHEMA_VERSION

    def __post_init__(self):
        require_non_empty(self.slot_id, "slot_id")
        require_aware(self.scheduled_for, "scheduled_for")
        zone = _zone(self.timezone)
        if not isinstance(self.run_type, RunType):
            raise ValueError("explicit RunType required")
        if self.mode not in {"research", "strict_live"}:
            raise ValueError("explicit mode required")
        if type(self.target_trade_date) is not date:
            raise ValueError("target_trade_date must be date")
        if self.scheduled_for.astimezone(zone).date() != self.target_trade_date:
            raise ValueError("scheduled_for and target trade date disagree")
        if self.market_basis_trade_date is not None and type(self.market_basis_trade_date) is not date:
            raise ValueError("market basis must be date or null")
        if self.run_type == RunType.POST_CLOSE:
            if self.market_basis_trade_date != self.target_trade_date:
                raise ValueError("POST_CLOSE basis must equal target")
        elif self.market_basis_trade_date is not None and self.market_basis_trade_date >= self.target_trade_date:
            raise ValueError("background basis must precede target")
        if self.schema_version != SCHEDULER_RUNTIME_SCHEMA_VERSION:
            raise ValueError("unsupported scheduler runtime schema")

    @property
    def schedule_instance_id(self) -> str:
        from quantos.reporting import canonical_json_bytes

        zone = _zone(self.timezone)
        identity = {
            "schema_version": self.schema_version,
            "slot_id": self.slot_id,
            "run_type": self.run_type.value,
            "scheduled_for": self.scheduled_for.astimezone(zone),
            "timezone": self.timezone,
            "mode": self.mode,
            "target_trade_date": self.target_trade_date,
        }
        return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


@dataclass(frozen=True, slots=True)
class SchedulerAttempt:
    attempt_id: str
    schedule_instance_id: str
    attempt_number: int
    claim_id: str
    fencing_token: int
    claimed_at: datetime
    lease_expires_at: datetime
    run_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    status: SchedulerAttemptStatus
    manifest_ref: ArtifactReference | None = None
    safe_error_code: str | None = None

    def __post_init__(self):
        for field_name in ("attempt_id", "schedule_instance_id", "claim_id"):
            require_non_empty(getattr(self, field_name), field_name)
        if type(self.attempt_number) is not int or self.attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        if type(self.fencing_token) is not int or self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        require_aware(self.claimed_at, "claimed_at")
        require_aware(self.lease_expires_at, "lease_expires_at")
        if self.lease_expires_at <= self.claimed_at:
            raise ValueError("lease must expire after claim")
        for field_name in ("started_at", "finished_at"):
            value = getattr(self, field_name)
            if value is not None:
                require_aware(value, field_name)
        if not isinstance(self.status, SchedulerAttemptStatus):
            raise ValueError("explicit attempt status required")
        if self.started_at is not None and self.started_at < self.claimed_at:
            raise ValueError("attempt cannot start before claim")
        if self.finished_at is not None and self.started_at is not None and self.finished_at < self.started_at:
            raise ValueError("attempt cannot finish before start")
        if self.status == SchedulerAttemptStatus.CLAIMED:
            if self.run_id is not None or self.started_at is not None or self.finished_at is not None:
                raise ValueError("unbound claim has execution fields")
        elif self.status == SchedulerAttemptStatus.RUNNING:
            if not self.run_id or self.started_at is None or self.finished_at is not None:
                raise ValueError("running attempt fields are incomplete")
        elif self.status == SchedulerAttemptStatus.STALE:
            if self.finished_at is None or ((self.run_id is None) != (self.started_at is None)):
                raise ValueError("stale attempt fields are inconsistent")
        elif not self.run_id or self.started_at is None or self.finished_at is None:
            raise ValueError("terminal execution attempt fields are incomplete")
        if self.manifest_ref is not None:
            _validate_reference(self.manifest_ref)
        _validate_safe_code(self.safe_error_code)


@dataclass(frozen=True, slots=True)
class SchedulerInstanceRecord:
    instance: ScheduleInstance
    status: SchedulerInstanceStatus
    active_claim_id: str | None
    active_fencing_token: int | None
    latest_attempt_number: int
    latest_fencing_token: int
    next_retry_at: datetime | None
    skip_reason: str | None
    last_safe_error_code: str | None
    updated_at: datetime

    def __post_init__(self):
        if not isinstance(self.instance, ScheduleInstance) or not isinstance(self.status, SchedulerInstanceStatus):
            raise ValueError("invalid scheduler instance record")
        if type(self.latest_attempt_number) is not int or self.latest_attempt_number < 0:
            raise ValueError("invalid latest attempt number")
        if type(self.latest_fencing_token) is not int or self.latest_fencing_token < 0:
            raise ValueError("invalid fencing token")
        require_aware(self.updated_at, "updated_at")
        if self.next_retry_at is not None:
            require_aware(self.next_retry_at, "next_retry_at")
        active = self.status == SchedulerInstanceStatus.CLAIMED
        if active != bool(self.active_claim_id) or active != (self.active_fencing_token is not None):
            raise ValueError("active claim fields disagree with state")
        if self.status == SchedulerInstanceStatus.FAILED_RETRYABLE and self.next_retry_at is None:
            raise ValueError("retryable instance requires next_retry_at")
        if self.status != SchedulerInstanceStatus.FAILED_RETRYABLE and self.next_retry_at is not None:
            raise ValueError("only retryable instance has next_retry_at")
        if self.status == SchedulerInstanceStatus.SKIPPED:
            _validate_safe_code(self.skip_reason)
        elif self.skip_reason is not None:
            raise ValueError("only skipped instance has skip_reason")
        _validate_safe_code(self.last_safe_error_code)


@dataclass(frozen=True, slots=True)
class ClaimResult:
    status: ClaimStatus
    instance_record: SchedulerInstanceRecord
    attempt: SchedulerAttempt | None = None

    def __post_init__(self):
        if not isinstance(self.status, ClaimStatus):
            raise ValueError("explicit claim status required")
        if (self.status == ClaimStatus.CLAIM_ACQUIRED) != (self.attempt is not None):
            raise ValueError("only acquired claims contain an attempt")


@dataclass(frozen=True, slots=True)
class SchedulerExecutionResult:
    run_id: str
    outcome: SchedulerExecutionOutcome
    manifest_ref: ArtifactReference | None = None
    safe_error_code: str | None = None

    def __post_init__(self):
        require_non_empty(self.run_id, "run_id")
        if not isinstance(self.outcome, SchedulerExecutionOutcome):
            raise ValueError("explicit execution outcome required")
        if self.outcome == SchedulerExecutionOutcome.SUCCEEDED:
            if self.safe_error_code is not None:
                raise ValueError("successful execution cannot have error code")
        elif self.safe_error_code is None:
            raise ValueError("failed execution requires safe error code")
        if self.manifest_ref is not None:
            _validate_reference(self.manifest_ref)
        _validate_safe_code(self.safe_error_code)


@dataclass(frozen=True, slots=True)
class RuntimeEligibility:
    status: RuntimeEligibilityStatus
    skip_reason: str | None = None

    def __post_init__(self):
        if not isinstance(self.status, RuntimeEligibilityStatus):
            raise ValueError("explicit runtime eligibility required")
        if self.status == RuntimeEligibilityStatus.SKIP:
            _validate_safe_code(self.skip_reason)
        elif self.skip_reason is not None:
            raise ValueError("only skipped eligibility has a reason")


@dataclass(frozen=True, slots=True)
class SchedulerRuntimeResult:
    status: SchedulerInvocationStatus
    schedule_instance_id: str
    eligibility: RuntimeEligibilityStatus
    claim_status: ClaimStatus | None
    run_context: RunContext | None
    attempt: SchedulerAttempt | None
    instance_record: SchedulerInstanceRecord | None

    def __post_init__(self):
        if not isinstance(self.status, SchedulerInvocationStatus):
            raise ValueError("explicit invocation status required")
        require_non_empty(self.schedule_instance_id, "schedule_instance_id")
        if not isinstance(self.eligibility, RuntimeEligibilityStatus):
            raise ValueError("explicit runtime eligibility required")
        if self.run_context is not None and self.attempt is None:
            raise ValueError("run context requires an attempt")


def _zone(name: str) -> ZoneInfo:
    require_non_empty(name, "timezone")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone") from exc


def _validate_safe_code(value: str | None) -> None:
    if value is not None and not _SAFE_CODE.fullmatch(value):
        raise ValueError("unsafe scheduler error code")


def _validate_reference(value: ArtifactReference) -> None:
    for item in (value.artifact_id, value.path, value.version or ""):
        lowered = item.lower()
        if any(marker in lowered for marker in ("authorization", "bearer ", "api_key", "secret", "token=")):
            raise ValueError("unsafe artifact reference")
