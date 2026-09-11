"""Transactional SQLite state for single-host scheduler runtime claims."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
import sqlite3
from uuid import uuid4

from quantos.config import Settings
from quantos.schemas._validation import require_aware, require_non_empty
from quantos.schemas.run import ArtifactReference, RunType
from quantos.schemas.scheduler_runtime import (
    ClaimResult, ClaimStatus, RetryPolicy, ScheduleInstance, SchedulerAttempt,
    SchedulerAttemptStatus, SchedulerExecutionOutcome, SchedulerExecutionResult,
    SchedulerInstanceRecord, SchedulerInstanceStatus, SCHEDULER_RUNTIME_SCHEMA_VERSION,
)
from .market import StorageError


_INSTANCE_COLUMNS = (
    "schedule_instance_id", "schema_version", "slot_id", "run_type", "scheduled_for",
    "timezone", "mode", "target_trade_date", "market_basis_trade_date", "status",
    "active_claim_id", "active_fencing_token", "latest_attempt_number",
    "latest_fencing_token", "next_retry_at", "skip_reason", "last_safe_error_code",
    "updated_at",
)
_ATTEMPT_COLUMNS = (
    "attempt_id", "schedule_instance_id", "attempt_number", "claim_id", "fencing_token",
    "claimed_at", "lease_expires_at", "run_id", "started_at", "finished_at", "status",
    "manifest_artifact_id", "manifest_path", "manifest_version", "safe_error_code",
)


class SchedulerRuntimeRepository:
    """Atomic claims and fenced transitions; each operation uses its own connection."""

    def __init__(self, settings: Settings, *, path: Path | None = None):
        self.path = path or settings.data_root / "runtime" / "scheduler" / "scheduler_runtime.sqlite3"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._raw_connection()
            try:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS scheduler_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS scheduler_instances (
                        schedule_instance_id TEXT PRIMARY KEY,
                        schema_version TEXT NOT NULL,
                        slot_id TEXT NOT NULL,
                        run_type TEXT NOT NULL,
                        scheduled_for TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        target_trade_date TEXT NOT NULL,
                        market_basis_trade_date TEXT,
                        status TEXT NOT NULL,
                        active_claim_id TEXT,
                        active_fencing_token INTEGER,
                        latest_attempt_number INTEGER NOT NULL,
                        latest_fencing_token INTEGER NOT NULL,
                        next_retry_at TEXT,
                        skip_reason TEXT,
                        last_safe_error_code TEXT,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS scheduler_attempts (
                        attempt_id TEXT PRIMARY KEY,
                        schedule_instance_id TEXT NOT NULL,
                        attempt_number INTEGER NOT NULL,
                        claim_id TEXT NOT NULL UNIQUE,
                        fencing_token INTEGER NOT NULL,
                        claimed_at TEXT NOT NULL,
                        lease_expires_at TEXT NOT NULL,
                        run_id TEXT UNIQUE,
                        started_at TEXT,
                        finished_at TEXT,
                        status TEXT NOT NULL,
                        manifest_artifact_id TEXT,
                        manifest_path TEXT,
                        manifest_version TEXT,
                        safe_error_code TEXT,
                        UNIQUE(schedule_instance_id, attempt_number),
                        UNIQUE(schedule_instance_id, fencing_token),
                        FOREIGN KEY(schedule_instance_id) REFERENCES scheduler_instances(schedule_instance_id)
                    );
                """)
                connection.execute(
                    "INSERT OR IGNORE INTO scheduler_metadata(key, value) VALUES ('schema_version', ?)",
                    (SCHEDULER_RUNTIME_SCHEMA_VERSION,),
                )
                self._validate_schema(connection)
            finally:
                connection.close()
        except Exception:
            raise StorageError("failed to initialize scheduler runtime state") from None

    def get_instance(self, schedule_instance_id: str) -> SchedulerInstanceRecord | None:
        require_non_empty(schedule_instance_id, "schedule_instance_id")
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM scheduler_instances WHERE schedule_instance_id = ?",
                    (schedule_instance_id,),
                ).fetchone()
                if row is None:
                    return None
                record = self._record_from_row(row)
                self._validate_record_history(connection, record)
                return record
        except StorageError:
            raise
        except Exception:
            raise StorageError("failed to read scheduler instance") from None

    def list_attempts(self, schedule_instance_id: str) -> tuple[SchedulerAttempt, ...]:
        require_non_empty(schedule_instance_id, "schedule_instance_id")
        try:
            with self._connection() as connection:
                instance_row = connection.execute(
                    "SELECT * FROM scheduler_instances WHERE schedule_instance_id = ?",
                    (schedule_instance_id,),
                ).fetchone()
                rows = connection.execute(
                    "SELECT * FROM scheduler_attempts WHERE schedule_instance_id = ? ORDER BY attempt_number",
                    (schedule_instance_id,),
                ).fetchall()
                attempts = tuple(self._attempt_from_row(row) for row in rows)
                if instance_row is not None:
                    record = self._record_from_row(instance_row)
                    self._validate_record_history(connection, record, attempts=attempts)
                elif attempts:
                    raise StorageError("scheduler attempts have no instance")
                return attempts
        except StorageError:
            raise
        except Exception:
            raise StorageError("failed to read scheduler attempts") from None

    def acquire_claim(
        self, instance: ScheduleInstance, *, now: datetime,
        lease_duration: timedelta, retry_policy: RetryPolicy,
    ) -> ClaimResult:
        require_aware(now, "now")
        if type(lease_duration) is not timedelta or lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        if not isinstance(retry_policy, RetryPolicy):
            raise ValueError("retry policy required")
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM scheduler_instances WHERE schedule_instance_id = ?",
                    (instance.schedule_instance_id,),
                ).fetchone()
                if row is None:
                    attempt = self._new_claim(connection, instance, now, lease_duration, 1, 1)
                    record = self._record_for_id(connection, instance.schedule_instance_id)
                    return ClaimResult(ClaimStatus.CLAIM_ACQUIRED, record, attempt)
                record = self._record_from_row(row)
                self._validate_record_history(connection, record)
                if record.instance != instance:
                    raise StorageError("scheduler instance identity collision")
                terminal = {
                    SchedulerInstanceStatus.SUCCEEDED: ClaimStatus.ALREADY_COMPLETED,
                    SchedulerInstanceStatus.FAILED_FINAL: ClaimStatus.FINAL_FAILURE,
                    SchedulerInstanceStatus.RETRY_EXHAUSTED: ClaimStatus.RETRY_EXHAUSTED,
                    SchedulerInstanceStatus.SKIPPED: ClaimStatus.ALREADY_SKIPPED,
                }
                if record.status in terminal:
                    return ClaimResult(terminal[record.status], record)
                if record.status == SchedulerInstanceStatus.CLAIMED:
                    active = self._active_attempt(connection, record)
                    if now < active.lease_expires_at:
                        return ClaimResult(ClaimStatus.ALREADY_CLAIMED, record)
                    connection.execute(
                        "UPDATE scheduler_attempts SET status = ?, finished_at = ?, safe_error_code = ? "
                        "WHERE attempt_id = ?",
                        (SchedulerAttemptStatus.STALE.value, _iso(now), "LEASE_EXPIRED", active.attempt_id),
                    )
                elif record.status == SchedulerInstanceStatus.FAILED_RETRYABLE:
                    if record.next_retry_at is None or now < record.next_retry_at:
                        return ClaimResult(ClaimStatus.RETRY_NOT_DUE, record)
                    if record.latest_attempt_number >= retry_policy.max_attempts:
                        connection.execute(
                            "UPDATE scheduler_instances SET status = ?, next_retry_at = NULL, updated_at = ? "
                            "WHERE schedule_instance_id = ?",
                            (SchedulerInstanceStatus.RETRY_EXHAUSTED.value, _iso(now), instance.schedule_instance_id),
                        )
                        exhausted = self._record_for_id(connection, instance.schedule_instance_id)
                        return ClaimResult(ClaimStatus.RETRY_EXHAUSTED, exhausted)
                attempt = self._new_claim(
                    connection, instance, now, lease_duration,
                    record.latest_attempt_number + 1, record.latest_fencing_token + 1,
                )
                current = self._record_for_id(connection, instance.schedule_instance_id)
                return ClaimResult(ClaimStatus.CLAIM_ACQUIRED, current, attempt)
        except StorageError:
            raise
        except Exception:
            raise StorageError("failed to acquire scheduler claim") from None

    def bind_run(
        self, schedule_instance_id: str, *, claim_id: str, fencing_token: int,
        run_id: str, started_at: datetime,
    ) -> SchedulerAttempt:
        require_aware(started_at, "started_at")
        require_non_empty(run_id, "run_id")
        try:
            with self._transaction() as connection:
                record = self._required_record(connection, schedule_instance_id)
                attempt = self._require_active_owner(connection, record, claim_id, fencing_token)
                if started_at >= attempt.lease_expires_at:
                    raise StorageError("claim lease expired before run binding")
                if attempt.status != SchedulerAttemptStatus.CLAIMED:
                    raise StorageError("claim has already been bound")
                connection.execute(
                    "UPDATE scheduler_attempts SET run_id = ?, started_at = ?, status = ? WHERE attempt_id = ?",
                    (run_id, _iso(started_at), SchedulerAttemptStatus.RUNNING.value, attempt.attempt_id),
                )
                connection.execute(
                    "UPDATE scheduler_instances SET updated_at = ? WHERE schedule_instance_id = ?",
                    (_iso(started_at), schedule_instance_id),
                )
                return self._attempt_for_id(connection, attempt.attempt_id)
        except StorageError:
            raise
        except Exception:
            raise StorageError("failed to bind scheduler run") from None

    def renew_claim(
        self, schedule_instance_id: str, *, claim_id: str, fencing_token: int,
        now: datetime, lease_expires_at: datetime,
    ) -> SchedulerAttempt:
        require_aware(now, "now")
        require_aware(lease_expires_at, "lease_expires_at")
        try:
            with self._transaction() as connection:
                record = self._required_record(connection, schedule_instance_id)
                attempt = self._require_active_owner(connection, record, claim_id, fencing_token)
                if now >= attempt.lease_expires_at:
                    raise StorageError("cannot renew stale claim")
                if lease_expires_at <= attempt.lease_expires_at or lease_expires_at <= now:
                    raise StorageError("claim renewal must extend the lease")
                connection.execute(
                    "UPDATE scheduler_attempts SET lease_expires_at = ? WHERE attempt_id = ?",
                    (_iso(lease_expires_at), attempt.attempt_id),
                )
                connection.execute(
                    "UPDATE scheduler_instances SET updated_at = ? WHERE schedule_instance_id = ?",
                    (_iso(now), schedule_instance_id),
                )
                return self._attempt_for_id(connection, attempt.attempt_id)
        except StorageError:
            raise
        except Exception:
            raise StorageError("failed to renew scheduler claim") from None

    def finalize_attempt(
        self, schedule_instance_id: str, *, claim_id: str, fencing_token: int,
        result: SchedulerExecutionResult, finished_at: datetime,
        retry_policy: RetryPolicy,
    ) -> SchedulerInstanceRecord:
        require_aware(finished_at, "finished_at")
        if not isinstance(result, SchedulerExecutionResult) or not isinstance(retry_policy, RetryPolicy):
            raise ValueError("canonical execution result and retry policy required")
        try:
            with self._transaction() as connection:
                record = self._required_record(connection, schedule_instance_id)
                attempt = self._require_active_owner(connection, record, claim_id, fencing_token)
                if finished_at >= attempt.lease_expires_at:
                    raise StorageError("cannot finalize expired claim")
                if attempt.status != SchedulerAttemptStatus.RUNNING or attempt.run_id != result.run_id:
                    raise StorageError("execution result does not match bound run")
                if finished_at < attempt.started_at:
                    raise StorageError("attempt cannot finish before start")
                attempt_status = SchedulerAttemptStatus(result.outcome.value)
                if result.outcome == SchedulerExecutionOutcome.SUCCEEDED:
                    instance_status = SchedulerInstanceStatus.SUCCEEDED
                    next_retry = None
                elif result.outcome == SchedulerExecutionOutcome.FAILED_FINAL:
                    instance_status = SchedulerInstanceStatus.FAILED_FINAL
                    next_retry = None
                elif attempt.attempt_number >= retry_policy.max_attempts:
                    instance_status = SchedulerInstanceStatus.RETRY_EXHAUSTED
                    next_retry = None
                else:
                    instance_status = SchedulerInstanceStatus.FAILED_RETRYABLE
                    next_retry = finished_at + retry_policy.retry_delay
                reference = result.manifest_ref
                connection.execute(
                    "UPDATE scheduler_attempts SET status = ?, finished_at = ?, manifest_artifact_id = ?, "
                    "manifest_path = ?, manifest_version = ?, safe_error_code = ? WHERE attempt_id = ?",
                    (attempt_status.value, _iso(finished_at), reference.artifact_id if reference else None,
                     reference.path if reference else None, reference.version if reference else None,
                     result.safe_error_code, attempt.attempt_id),
                )
                connection.execute(
                    "UPDATE scheduler_instances SET status = ?, active_claim_id = NULL, "
                    "active_fencing_token = NULL, next_retry_at = ?, last_safe_error_code = ?, "
                    "updated_at = ? WHERE schedule_instance_id = ?",
                    (instance_status.value, _iso(next_retry), result.safe_error_code,
                     _iso(finished_at), schedule_instance_id),
                )
                return self._record_for_id(connection, schedule_instance_id)
        except StorageError:
            raise
        except Exception:
            raise StorageError("failed to finalize scheduler attempt") from None

    def record_skip(
        self, instance: ScheduleInstance, *, evaluated_at: datetime, skip_reason: str,
    ) -> SchedulerInstanceRecord:
        require_aware(evaluated_at, "evaluated_at")
        require_non_empty(skip_reason, "skip_reason")
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM scheduler_instances WHERE schedule_instance_id = ?",
                    (instance.schedule_instance_id,),
                ).fetchone()
                if row is not None:
                    record = self._record_from_row(row)
                    self._validate_record_history(connection, record)
                    if record.instance != instance or record.status != SchedulerInstanceStatus.SKIPPED:
                        raise StorageError("invalid transition to skipped")
                    return record
                connection.execute(
                    "INSERT INTO scheduler_instances VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (instance.schedule_instance_id, instance.schema_version, instance.slot_id,
                     instance.run_type.value, _iso(instance.scheduled_for), instance.timezone,
                     instance.mode, instance.target_trade_date.isoformat(),
                     instance.market_basis_trade_date.isoformat() if instance.market_basis_trade_date else None,
                     SchedulerInstanceStatus.SKIPPED.value, None, None, 0, 0, None,
                     skip_reason, None, _iso(evaluated_at)),
                )
                return self._record_for_id(connection, instance.schedule_instance_id)
        except StorageError:
            raise
        except Exception:
            raise StorageError("failed to persist scheduler skip") from None

    def _new_claim(self, connection, instance, now, lease_duration, attempt_number, fencing_token):
        attempt_id = uuid4().hex
        claim_id = uuid4().hex
        lease_expires_at = now + lease_duration
        exists = connection.execute(
            "SELECT 1 FROM scheduler_instances WHERE schedule_instance_id = ?",
            (instance.schedule_instance_id,),
        ).fetchone()
        if exists is None:
            connection.execute(
                "INSERT INTO scheduler_instances VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (instance.schedule_instance_id, instance.schema_version, instance.slot_id,
                 instance.run_type.value, _iso(instance.scheduled_for), instance.timezone,
                 instance.mode, instance.target_trade_date.isoformat(),
                 instance.market_basis_trade_date.isoformat() if instance.market_basis_trade_date else None,
                 SchedulerInstanceStatus.CLAIMED.value, claim_id, fencing_token,
                 attempt_number, fencing_token, None, None, None, _iso(now)),
            )
        else:
            connection.execute(
                "UPDATE scheduler_instances SET status = ?, active_claim_id = ?, active_fencing_token = ?, "
                "latest_attempt_number = ?, latest_fencing_token = ?, next_retry_at = NULL, "
                "skip_reason = NULL, updated_at = ? WHERE schedule_instance_id = ?",
                (SchedulerInstanceStatus.CLAIMED.value, claim_id, fencing_token, attempt_number,
                 fencing_token, _iso(now), instance.schedule_instance_id),
            )
        connection.execute(
            "INSERT INTO scheduler_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, instance.schedule_instance_id, attempt_number, claim_id, fencing_token,
             _iso(now), _iso(lease_expires_at), None, None, None,
             SchedulerAttemptStatus.CLAIMED.value, None, None, None, None),
        )
        return self._attempt_for_id(connection, attempt_id)

    def _required_record(self, connection, schedule_instance_id):
        row = connection.execute(
            "SELECT * FROM scheduler_instances WHERE schedule_instance_id = ?",
            (schedule_instance_id,),
        ).fetchone()
        if row is None:
            raise StorageError("scheduler instance not found")
        record = self._record_from_row(row)
        self._validate_record_history(connection, record)
        return record

    def _record_for_id(self, connection, schedule_instance_id):
        return self._required_record(connection, schedule_instance_id)

    def _attempt_for_id(self, connection, attempt_id):
        row = connection.execute(
            "SELECT * FROM scheduler_attempts WHERE attempt_id = ?", (attempt_id,),
        ).fetchone()
        if row is None:
            raise StorageError("scheduler attempt not found")
        return self._attempt_from_row(row)

    def _active_attempt(self, connection, record):
        row = connection.execute(
            "SELECT * FROM scheduler_attempts WHERE schedule_instance_id = ? AND claim_id = ? "
            "AND fencing_token = ?",
            (record.instance.schedule_instance_id, record.active_claim_id, record.active_fencing_token),
        ).fetchone()
        if row is None:
            raise StorageError("active claim attempt missing")
        attempt = self._attempt_from_row(row)
        if attempt.status not in {SchedulerAttemptStatus.CLAIMED, SchedulerAttemptStatus.RUNNING}:
            raise StorageError("active claim attempt is not active")
        return attempt

    def _require_active_owner(self, connection, record, claim_id, fencing_token):
        if (record.status != SchedulerInstanceStatus.CLAIMED
            or record.active_claim_id != claim_id
            or record.active_fencing_token != fencing_token):
            raise StorageError("claim fencing ownership mismatch")
        return self._active_attempt(connection, record)

    def _record_from_row(self, row):
        instance = ScheduleInstance(
            slot_id=row["slot_id"], run_type=RunType(row["run_type"]),
            scheduled_for=_datetime(row["scheduled_for"]), timezone=row["timezone"],
            mode=row["mode"], target_trade_date=date.fromisoformat(row["target_trade_date"]),
            market_basis_trade_date=(date.fromisoformat(row["market_basis_trade_date"])
                                     if row["market_basis_trade_date"] else None),
            schema_version=row["schema_version"],
        )
        if instance.schedule_instance_id != row["schedule_instance_id"]:
            raise StorageError("scheduler instance identity mismatch")
        return SchedulerInstanceRecord(
            instance=instance, status=SchedulerInstanceStatus(row["status"]),
            active_claim_id=row["active_claim_id"], active_fencing_token=row["active_fencing_token"],
            latest_attempt_number=row["latest_attempt_number"],
            latest_fencing_token=row["latest_fencing_token"],
            next_retry_at=_datetime(row["next_retry_at"]) if row["next_retry_at"] else None,
            skip_reason=row["skip_reason"], last_safe_error_code=row["last_safe_error_code"],
            updated_at=_datetime(row["updated_at"]),
        )

    def _attempt_from_row(self, row):
        reference_values = (row["manifest_artifact_id"], row["manifest_path"], row["manifest_version"])
        if any(value is not None for value in reference_values) and reference_values[:2].count(None):
            raise StorageError("partial manifest reference")
        reference = (ArtifactReference(*reference_values) if row["manifest_artifact_id"] is not None else None)
        return SchedulerAttempt(
            attempt_id=row["attempt_id"], schedule_instance_id=row["schedule_instance_id"],
            attempt_number=row["attempt_number"], claim_id=row["claim_id"],
            fencing_token=row["fencing_token"], claimed_at=_datetime(row["claimed_at"]),
            lease_expires_at=_datetime(row["lease_expires_at"]), run_id=row["run_id"],
            started_at=_datetime(row["started_at"]) if row["started_at"] else None,
            finished_at=_datetime(row["finished_at"]) if row["finished_at"] else None,
            status=SchedulerAttemptStatus(row["status"]), manifest_ref=reference,
            safe_error_code=row["safe_error_code"],
        )

    def _validate_record_history(self, connection, record, *, attempts=None):
        if attempts is None:
            rows = connection.execute(
                "SELECT * FROM scheduler_attempts WHERE schedule_instance_id = ? ORDER BY attempt_number",
                (record.instance.schedule_instance_id,),
            ).fetchall()
            attempts = tuple(self._attempt_from_row(row) for row in rows)
        if record.status == SchedulerInstanceStatus.SKIPPED:
            if attempts or record.latest_attempt_number != 0 or record.latest_fencing_token != 0:
                raise StorageError("skipped instance has attempt history")
            return
        if not attempts:
            raise StorageError("scheduler instance has no attempt history")
        expected_numbers = tuple(range(1, len(attempts) + 1))
        if tuple(item.attempt_number for item in attempts) != expected_numbers:
            raise StorageError("scheduler attempt numbers are not contiguous")
        fences = tuple(item.fencing_token for item in attempts)
        if any(current <= previous for previous, current in zip(fences, fences[1:])):
            raise StorageError("scheduler fencing tokens are not increasing")
        if any(item.status not in {
            SchedulerAttemptStatus.STALE, SchedulerAttemptStatus.FAILED_RETRYABLE,
        } for item in attempts[:-1]):
            raise StorageError("non-latest scheduler attempt is not historical")
        latest = attempts[-1]
        if (record.latest_attempt_number != latest.attempt_number
            or record.latest_fencing_token != latest.fencing_token):
            raise StorageError("scheduler instance and latest attempt disagree")
        expected_latest = {
            SchedulerInstanceStatus.CLAIMED: {
                SchedulerAttemptStatus.CLAIMED, SchedulerAttemptStatus.RUNNING,
            },
            SchedulerInstanceStatus.SUCCEEDED: {SchedulerAttemptStatus.SUCCEEDED},
            SchedulerInstanceStatus.FAILED_RETRYABLE: {SchedulerAttemptStatus.FAILED_RETRYABLE},
            SchedulerInstanceStatus.FAILED_FINAL: {SchedulerAttemptStatus.FAILED_FINAL},
            SchedulerInstanceStatus.RETRY_EXHAUSTED: {SchedulerAttemptStatus.FAILED_RETRYABLE},
        }
        if latest.status not in expected_latest[record.status]:
            raise StorageError("scheduler instance and attempt status disagree")
        if record.status == SchedulerInstanceStatus.CLAIMED:
            if (record.active_claim_id != latest.claim_id
                or record.active_fencing_token != latest.fencing_token):
                raise StorageError("active claim and latest attempt disagree")

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @contextmanager
    def _connection(self):
        connection = self._raw_connection()
        try:
            self._validate_schema(connection)
            yield connection
        finally:
            connection.close()

    def _raw_connection(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _validate_schema(self, connection):
        metadata = dict(connection.execute("SELECT key, value FROM scheduler_metadata").fetchall())
        if metadata != {"schema_version": SCHEDULER_RUNTIME_SCHEMA_VERSION}:
            raise StorageError("unsupported scheduler runtime schema")
        expected = {
            "scheduler_metadata": ("key", "value"),
            "scheduler_instances": _INSTANCE_COLUMNS,
            "scheduler_attempts": _ATTEMPT_COLUMNS,
        }
        for table, columns in expected.items():
            actual = tuple(row["name"] for row in connection.execute(f"PRAGMA table_info({table})"))
            if actual != columns:
                raise StorageError("noncanonical scheduler runtime schema")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    require_aware(value, "runtime timestamp")
    return value.isoformat()


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    require_aware(parsed, "persisted runtime timestamp")
    return parsed
