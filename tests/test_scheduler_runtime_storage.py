"""Transactional scheduler state tests; all databases are temporary and offline."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import date, datetime, timedelta
import socket
import sqlite3
from threading import Barrier

import pytest

from quantos.config import MARKET_TIMEZONE, Settings
from quantos.schemas.run import ArtifactReference
from quantos.schemas.scheduler_runtime import (
    ClaimStatus, RetryPolicy, ScheduleInstance, SchedulerAttempt,
    SchedulerAttemptStatus, SchedulerExecutionOutcome, SchedulerExecutionResult,
    SchedulerInstanceStatus,
)
from quantos.scheduler_runtime import schedule_instance_from_decision
from quantos.scheduling import LocalTradingCalendar, evaluate_schedule
from quantos.storage.market import StorageError
from quantos.storage.scheduler_runtime import SchedulerRuntimeRepository

DAY = date(2026, 9, 1)
PREVIOUS = date(2026, 8, 31)
CALENDAR = LocalTradingCalendar((PREVIOUS, DAY))
AT = datetime(2026, 9, 1, 9, tzinfo=MARKET_TIMEZONE)
LEASE = timedelta(hours=1)
NO_RETRY = RetryPolicy()
RUN_ONE = "1" * 64
RUN_TWO = "2" * 64


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("scheduler storage must not access network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def repo(tmp_path):
    return SchedulerRuntimeRepository(Settings.from_project_root(tmp_path))


def decision(value=AT, slot_id="PRE_OPEN_0900"):
    result = evaluate_schedule(evaluated_at=value, calendar=CALENDAR)
    return next(item for item in result.decisions if item.slot_id == slot_id)


def instance(*, mode="research", value=AT, slot_id="PRE_OPEN_0900"):
    return schedule_instance_from_decision(decision(value, slot_id), mode=mode)


def acquire(repository, value=None, *, now=AT, retry=NO_RETRY, lease=LEASE):
    return repository.acquire_claim(value or instance(), now=now,
                                    lease_duration=lease, retry_policy=retry)


def bind(repository, claim, *, run_id=RUN_ONE, started=AT):
    return repository.bind_run(
        claim.instance_record.instance.schedule_instance_id,
        claim_id=claim.attempt.claim_id, fencing_token=claim.attempt.fencing_token,
        run_id=run_id, started_at=started,
    )


def finalize(repository, claim, *, run_id=RUN_ONE, outcome=SchedulerExecutionOutcome.SUCCEEDED,
             finished=AT, retry=NO_RETRY, code=None, manifest=None):
    result = SchedulerExecutionResult(run_id, outcome, manifest_ref=manifest, safe_error_code=code)
    return repository.finalize_attempt(
        claim.instance_record.instance.schedule_instance_id,
        claim_id=claim.attempt.claim_id, fencing_token=claim.attempt.fencing_token,
        result=result, finished_at=finished, retry_policy=retry,
    )


def test_default_storage_path_is_runtime_not_derived(tmp_path):
    repository = SchedulerRuntimeRepository(Settings.from_project_root(tmp_path))
    assert repository.path == tmp_path / "data/runtime/scheduler/scheduler_runtime.sqlite3"
    assert repository.path.exists()


def test_claim_creates_readable_instance_and_attempt(repo):
    result = acquire(repo)
    assert result.status == ClaimStatus.CLAIM_ACQUIRED
    assert repo.get_instance(instance().schedule_instance_id) == result.instance_record
    assert repo.list_attempts(instance().schedule_instance_id) == (result.attempt,)
    assert result.attempt.attempt_number == result.attempt.fencing_token == 1


def test_attempt_canonical_fields_are_closed():
    assert tuple(field.name for field in fields(SchedulerAttempt)) == (
        "attempt_id", "schedule_instance_id", "attempt_number", "claim_id",
        "fencing_token", "claimed_at", "lease_expires_at", "run_id",
        "started_at", "finished_at", "status", "manifest_ref", "safe_error_code",
    )


def test_duplicate_active_claim_is_rejected_without_new_attempt(repo):
    first = acquire(repo)
    second = acquire(repo, now=AT + timedelta(minutes=59, seconds=59))
    assert first.status == ClaimStatus.CLAIM_ACQUIRED
    assert second.status == ClaimStatus.ALREADY_CLAIMED
    assert len(repo.list_attempts(instance().schedule_instance_id)) == 1


def test_eight_concurrent_claims_have_exactly_one_winner(repo):
    barrier = Barrier(8)

    def compete(_):
        barrier.wait()
        return acquire(repo).status

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(compete, range(8)))
    assert statuses.count(ClaimStatus.CLAIM_ACQUIRED) == 1
    assert statuses.count(ClaimStatus.ALREADY_CLAIMED) == 7
    assert len(repo.list_attempts(instance().schedule_instance_id)) == 1


def test_different_instances_can_each_hold_claim(repo):
    research = acquire(repo, instance(mode="research"))
    strict = acquire(repo, instance(mode="strict_live"))
    assert research.status == strict.status == ClaimStatus.CLAIM_ACQUIRED
    assert research.instance_record.instance.schedule_instance_id != strict.instance_record.instance.schedule_instance_id


def test_exact_lease_expiry_is_stale_and_increments_fence(repo):
    first = acquire(repo)
    before = acquire(repo, now=AT + LEASE - timedelta(microseconds=1))
    second = acquire(repo, now=AT + LEASE)
    assert before.status == ClaimStatus.ALREADY_CLAIMED
    assert second.status == ClaimStatus.CLAIM_ACQUIRED
    assert second.attempt.attempt_number == second.attempt.fencing_token == 2
    attempts = repo.list_attempts(instance().schedule_instance_id)
    assert attempts[0].status == SchedulerAttemptStatus.STALE
    assert attempts[0].safe_error_code == "LEASE_EXPIRED"


@pytest.mark.parametrize("outcome,code", [
    (SchedulerExecutionOutcome.SUCCEEDED, None),
    (SchedulerExecutionOutcome.FAILED_RETRYABLE, "TEMPORARY_FAILURE"),
    (SchedulerExecutionOutcome.FAILED_FINAL, "DETERMINISTIC_FAILURE"),
])
def test_zombie_finalize_rejected_after_stale_takeover(repo, outcome, code):
    first = acquire(repo)
    bind(repo, first)
    second = acquire(repo, now=AT + LEASE)
    bind(repo, second, run_id=RUN_TWO, started=AT + LEASE)
    with pytest.raises(StorageError, match="fencing"):
        finalize(repo, first, outcome=outcome, code=code,
                 finished=AT + timedelta(minutes=30))
    current = finalize(repo, second, run_id=RUN_TWO, finished=AT + LEASE)
    assert current.status == SchedulerInstanceStatus.SUCCEEDED


def test_zombie_unbound_claim_cannot_bind_after_takeover(repo):
    first = acquire(repo)
    second = acquire(repo, now=AT + LEASE)
    with pytest.raises(StorageError, match="fencing"):
        bind(repo, first, started=AT + timedelta(minutes=30))
    assert repo.get_instance(instance().schedule_instance_id).active_claim_id == second.attempt.claim_id


def test_stale_unbound_claim_remains_in_attempt_audit(repo):
    first = acquire(repo)
    acquire(repo, now=AT + LEASE)
    stale = repo.list_attempts(instance().schedule_instance_id)[0]
    assert stale.attempt_id == first.attempt.attempt_id
    assert stale.status == SchedulerAttemptStatus.STALE
    assert stale.run_id is stale.started_at is None
    assert stale.finished_at == AT + LEASE


def test_bind_run_happens_before_running_status(repo):
    claim = acquire(repo)
    attempt = bind(repo, claim)
    assert attempt.status == SchedulerAttemptStatus.RUNNING
    assert attempt.run_id == RUN_ONE and attempt.started_at == AT
    assert attempt.finished_at is None


def test_bind_rejects_exact_expiry(repo):
    claim = acquire(repo)
    with pytest.raises(StorageError, match="expired"):
        bind(repo, claim, started=AT + LEASE)


def test_renew_claim_only_by_current_owner_and_extends_forward(repo):
    claim = acquire(repo)
    extended = repo.renew_claim(
        instance().schedule_instance_id, claim_id=claim.attempt.claim_id,
        fencing_token=claim.attempt.fencing_token, now=AT + timedelta(minutes=30),
        lease_expires_at=AT + timedelta(hours=2),
    )
    assert extended.lease_expires_at == AT + timedelta(hours=2)
    with pytest.raises(StorageError, match="extend"):
        repo.renew_claim(
            instance().schedule_instance_id, claim_id=claim.attempt.claim_id,
            fencing_token=claim.attempt.fencing_token, now=AT + timedelta(minutes=31),
            lease_expires_at=AT + timedelta(hours=1, minutes=30),
        )


def test_old_fence_cannot_renew_after_takeover(repo):
    first = acquire(repo)
    acquire(repo, now=AT + LEASE)
    with pytest.raises(StorageError, match="fencing"):
        repo.renew_claim(
            instance().schedule_instance_id, claim_id=first.attempt.claim_id,
            fencing_token=first.attempt.fencing_token, now=AT + LEASE,
            lease_expires_at=AT + timedelta(hours=2),
        )


def test_exact_expired_claim_cannot_renew(repo):
    claim = acquire(repo)
    with pytest.raises(StorageError, match="stale"):
        repo.renew_claim(
            instance().schedule_instance_id, claim_id=claim.attempt.claim_id,
            fencing_token=claim.attempt.fencing_token, now=AT + LEASE,
            lease_expires_at=AT + timedelta(hours=2),
        )


def test_finalize_success_is_terminal_and_idempotent_on_reclaim(repo):
    claim = acquire(repo)
    bind(repo, claim)
    completed = finalize(repo, claim)
    duplicate = acquire(repo)
    assert completed.status == SchedulerInstanceStatus.SUCCEEDED
    assert duplicate.status == ClaimStatus.ALREADY_COMPLETED
    assert len(repo.list_attempts(instance().schedule_instance_id)) == 1


def test_finalize_rejects_finish_before_start_without_committing(repo):
    claim = acquire(repo)
    bind(repo, claim, started=AT + timedelta(seconds=2))
    with pytest.raises(StorageError, match="before start"):
        finalize(repo, claim, finished=AT + timedelta(seconds=1))
    attempt = repo.list_attempts(instance().schedule_instance_id)[0]
    assert attempt.status == SchedulerAttemptStatus.RUNNING
    assert attempt.finished_at is None


def test_failed_final_never_reclaims(repo):
    claim = acquire(repo)
    bind(repo, claim)
    final = finalize(repo, claim, outcome=SchedulerExecutionOutcome.FAILED_FINAL,
                     code="DETERMINISTIC_FAILURE")
    assert final.status == SchedulerInstanceStatus.FAILED_FINAL
    assert acquire(repo, now=AT + timedelta(days=1)).status == ClaimStatus.FINAL_FAILURE


def test_retryable_failure_records_exact_retry_time(repo):
    retry = RetryPolicy(2, timedelta(minutes=5))
    claim = acquire(repo, retry=retry)
    bind(repo, claim)
    failed = finalize(repo, claim, outcome=SchedulerExecutionOutcome.FAILED_RETRYABLE,
                      code="TEMPORARY_FAILURE", retry=retry)
    assert failed.status == SchedulerInstanceStatus.FAILED_RETRYABLE
    assert failed.next_retry_at == AT + timedelta(minutes=5)
    assert acquire(repo, now=AT + timedelta(minutes=4, seconds=59), retry=retry).status == ClaimStatus.RETRY_NOT_DUE
    assert acquire(repo, now=AT + timedelta(minutes=5), retry=retry).status == ClaimStatus.CLAIM_ACQUIRED


def test_second_retryable_failure_exhausts_budget(repo):
    retry = RetryPolicy(2, timedelta(minutes=5))
    first = acquire(repo, retry=retry)
    bind(repo, first)
    finalize(repo, first, outcome=SchedulerExecutionOutcome.FAILED_RETRYABLE,
             code="TEMPORARY_FAILURE", retry=retry)
    second = acquire(repo, now=AT + timedelta(minutes=5), retry=retry)
    bind(repo, second, run_id=RUN_TWO, started=AT + timedelta(minutes=5))
    exhausted = finalize(
        repo, second, run_id=RUN_TWO, outcome=SchedulerExecutionOutcome.FAILED_RETRYABLE,
        code="TEMPORARY_FAILURE", retry=retry, finished=AT + timedelta(minutes=5),
    )
    assert exhausted.status == SchedulerInstanceStatus.RETRY_EXHAUSTED
    assert acquire(repo, now=AT + timedelta(hours=1), retry=retry).status == ClaimStatus.RETRY_EXHAUSTED
    assert len(repo.list_attempts(instance().schedule_instance_id)) == 2


def test_default_retry_policy_executes_once(repo):
    claim = acquire(repo)
    bind(repo, claim)
    exhausted = finalize(repo, claim, outcome=SchedulerExecutionOutcome.FAILED_RETRYABLE,
                         code="TEMPORARY_FAILURE")
    assert exhausted.status == SchedulerInstanceStatus.RETRY_EXHAUSTED
    assert acquire(repo, now=AT + timedelta(minutes=1)).status == ClaimStatus.RETRY_EXHAUSTED


def test_manifest_reference_is_correlation_only(repo):
    claim = acquire(repo)
    bind(repo, claim)
    reference = ArtifactReference("a" * 64, "/tmp/manifest.json", "quantos-run-v1")
    finalize(repo, claim, manifest=reference)
    assert repo.list_attempts(instance().schedule_instance_id)[0].manifest_ref == reference


def test_partial_manifest_reference_corruption_fails_closed(repo):
    claim = acquire(repo)
    bind(repo, claim)
    reference = ArtifactReference("a" * 64, "/tmp/manifest.json", "quantos-run-v1")
    finalize(repo, claim, manifest=reference)
    with sqlite3.connect(repo.path) as connection:
        connection.execute("UPDATE scheduler_attempts SET manifest_path = NULL")
    with pytest.raises(StorageError, match="partial manifest reference"):
        repo.list_attempts(instance().schedule_instance_id)


def test_record_skip_is_terminal_repeatable_and_has_no_attempt(repo):
    value = instance()
    first = repo.record_skip(value, evaluated_at=AT + timedelta(hours=1),
                             skip_reason="MISSED_WINDOW_POLICY_SKIP")
    second = repo.record_skip(value, evaluated_at=AT + timedelta(hours=2),
                              skip_reason="MISSED_WINDOW_POLICY_SKIP")
    assert first == second and first.status == SchedulerInstanceStatus.SKIPPED
    assert repo.list_attempts(value.schedule_instance_id) == ()
    assert acquire(repo, value, now=AT + timedelta(hours=2)).status == ClaimStatus.ALREADY_SKIPPED


def test_skip_cannot_overwrite_claimed_instance(repo):
    value = instance()
    acquire(repo, value)
    with pytest.raises(StorageError, match="invalid transition"):
        repo.record_skip(value, evaluated_at=AT + timedelta(minutes=16),
                         skip_reason="MISSED_WINDOW_POLICY_SKIP")


@pytest.mark.parametrize("field", ["now", "started", "finished"])
def test_runtime_storage_rejects_naive_timestamps(repo, field):
    naive = AT.replace(tzinfo=None)
    if field == "now":
        with pytest.raises(ValueError):
            acquire(repo, now=naive)
    elif field == "started":
        claim = acquire(repo)
        with pytest.raises(ValueError):
            bind(repo, claim, started=naive)
    else:
        claim = acquire(repo)
        bind(repo, claim)
        with pytest.raises(ValueError):
            finalize(repo, claim, finished=naive)


def test_unknown_schema_version_fails_closed(repo):
    with sqlite3.connect(repo.path) as connection:
        connection.execute("UPDATE scheduler_metadata SET value = 'future-v999'")
    with pytest.raises(StorageError, match="schema"):
        repo.get_instance(instance().schedule_instance_id)


def test_unknown_persisted_column_fails_closed(repo):
    with sqlite3.connect(repo.path) as connection:
        connection.execute("ALTER TABLE scheduler_instances ADD COLUMN raw_payload TEXT")
    with pytest.raises(StorageError, match="schema"):
        repo.get_instance(instance().schedule_instance_id)


def test_instance_and_latest_attempt_mismatch_fails_closed(repo):
    acquire(repo)
    with sqlite3.connect(repo.path) as connection:
        connection.execute("UPDATE scheduler_instances SET latest_attempt_number = 2")
    with pytest.raises(StorageError, match="latest attempt"):
        repo.get_instance(instance().schedule_instance_id)


def test_instance_and_attempt_status_mismatch_fails_closed(repo):
    claim = acquire(repo)
    bind(repo, claim)
    finalize(repo, claim)
    with sqlite3.connect(repo.path) as connection:
        connection.execute("UPDATE scheduler_instances SET status = 'FAILED_FINAL'")
    with pytest.raises(StorageError, match="status disagree"):
        repo.get_instance(instance().schedule_instance_id)


def test_non_latest_active_attempt_corruption_fails_closed(repo):
    retry = RetryPolicy(2, timedelta(0))
    first = acquire(repo, retry=retry)
    bind(repo, first)
    finalize(repo, first, outcome=SchedulerExecutionOutcome.FAILED_RETRYABLE,
             code="TEMPORARY_FAILURE", retry=retry)
    second = acquire(repo, retry=retry)
    bind(repo, second, run_id=RUN_TWO)
    finalize(repo, second, run_id=RUN_TWO, retry=retry)
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            "UPDATE scheduler_attempts SET status = 'RUNNING', finished_at = NULL "
            "WHERE attempt_number = 1"
        )
    with pytest.raises(StorageError, match="not historical"):
        repo.get_instance(instance().schedule_instance_id)


def test_acquire_claim_rolls_back_partial_instance_and_attempt(monkeypatch, repo):
    def fail_after_insert(*args, **kwargs):
        raise RuntimeError("fault injection")

    monkeypatch.setattr(repo, "_attempt_for_id", fail_after_insert)
    with pytest.raises(StorageError, match="acquire"):
        acquire(repo)
    with sqlite3.connect(repo.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM scheduler_instances").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM scheduler_attempts").fetchone()[0] == 0


def test_corrupt_instance_state_fails_closed(repo):
    acquire(repo)
    with sqlite3.connect(repo.path) as connection:
        connection.execute("UPDATE scheduler_instances SET status = 'UNKNOWN'")
    with pytest.raises(StorageError):
        repo.get_instance(instance().schedule_instance_id)


def test_corrupt_attempt_state_fails_closed(repo):
    acquire(repo)
    with sqlite3.connect(repo.path) as connection:
        connection.execute("UPDATE scheduler_attempts SET status = 'UNKNOWN'")
    with pytest.raises(StorageError):
        repo.list_attempts(instance().schedule_instance_id)


def test_unsafe_error_or_reference_rejected_before_persistence(repo):
    with pytest.raises(ValueError, match="unsafe"):
        SchedulerExecutionResult(RUN_ONE, SchedulerExecutionOutcome.FAILED_FINAL,
                                 safe_error_code="Authorization: Bearer secret")
    with pytest.raises(ValueError, match="unsafe"):
        SchedulerExecutionResult(
            RUN_ONE, SchedulerExecutionOutcome.SUCCEEDED,
            manifest_ref=ArtifactReference("a" * 64, "/tmp/secret-token=abc", "v1"),
        )
    assert b"Authorization" not in repo.path.read_bytes()
