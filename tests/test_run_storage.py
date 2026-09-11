import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from quantos.config import Settings
from quantos.orchestration import execute_run
from quantos.storage.market import StorageError
from quantos.storage.run import RunRepository, manifest_from_record, manifest_record
from tests.test_orchestration import setup_run, handlers, AT


def manifest():
    context, readiness = setup_run()
    return execute_run(context, readiness, handlers=handlers([]), plan_only=True,
                       clock=lambda: AT, monotonic=lambda: 1.0)


def test_manifest_roundtrip(tmp_path):
    value = manifest()
    repo = RunRepository(Settings.from_project_root(tmp_path))
    path = repo.write(value)
    assert repo.read(path) == value
    assert path.parent.name == "target_trade_date=2026-08-31"
    raw = json.loads(path.read_text())
    assert raw["run_context"]["run_id"] == value.run_context.run_id
    assert set(raw) == {"schema_version", "run_context", "plan", "execution_results", "summary", "provenance"}


def test_append_only_same_logical_id_different_generations(tmp_path):
    value = manifest()
    repo = RunRepository(Settings.from_project_root(tmp_path))
    first = repo.write(value)
    original = first.read_bytes()
    second = repo.write(replace(value, run_context=replace(value.run_context, generated_at=AT + timedelta(seconds=1))))
    assert first != second and first.read_bytes() == original
    assert first.name[:64] == second.name[:64]


def test_generation_collision_never_overwrites(tmp_path, monkeypatch):
    repo = RunRepository(Settings.from_project_root(tmp_path))
    monkeypatch.setattr("quantos.storage.run.uuid4", lambda: SimpleNamespace(hex="fixed"))
    first = repo.write(manifest())
    before = first.read_bytes()
    with pytest.raises(StorageError):
        repo.write(manifest())
    assert first.read_bytes() == before


def test_atomic_rename_failure_no_partial_json_or_temp(tmp_path, monkeypatch):
    repo = RunRepository(Settings.from_project_root(tmp_path))
    def broken(*args):
        raise OSError("Authorization secret must not escape")
    monkeypatch.setattr("quantos.storage.run.os.rename", broken)
    with pytest.raises(StorageError) as exc:
        repo.write(manifest())
    assert "Authorization" not in str(exc.value) and exc.value.__cause__ is None
    assert not list(repo.root.rglob("*.json"))
    assert not list(repo.root.rglob("*.tmp"))
    assert not list(repo.root.rglob("*.lock"))


@pytest.mark.parametrize("change", ["identity", "schema", "extra", "module_order"])
def test_corrupt_manifest_rejected(change):
    record = manifest_record(manifest())
    if change == "identity":
        record["run_context"]["run_id"] = "0" * 64
    elif change == "schema":
        record["schema_version"] = "other"
    elif change == "extra":
        record["raw_llm_response"] = "sensitive"
    else:
        record["plan"].reverse()
    with pytest.raises(ValueError):
        manifest_from_record(record)


def test_error_safety_and_no_business_payload_persistence(tmp_path):
    context, readiness = setup_run()
    from quantos.schemas.run import ModuleName
    value = execute_run(context, readiness, handlers=handlers([], {ModuleName.EVIDENCE}))
    repo = RunRepository(Settings.from_project_root(tmp_path))
    path = repo.write(value)
    text = path.read_text().lower()
    for forbidden in ("authorization", "secret-invalid", "fake-credential", "possible_explanations", "market_overview"):
        assert forbidden not in text


def test_unreadable_manifest_safe_storage_error(tmp_path):
    with pytest.raises(StorageError):
        RunRepository(Settings.from_project_root(tmp_path)).read(tmp_path / "missing.json")


@pytest.mark.parametrize("mutation", ["counts", "disabled_pass", "will_execute", "nested_payload"])
def test_manifest_rejects_inconsistent_persisted_execution(mutation):
    context, ready = setup_run()
    record = manifest_record(execute_run(context, ready, handlers=handlers([])))
    if mutation == "counts":
        record["summary"]["passed_modules"] += 1
    elif mutation == "disabled_pass":
        record["plan"][0]["availability"] = "SKIP_POLICY_DISABLED"
        record["plan"][0]["will_execute"] = False
    elif mutation == "will_execute":
        record["plan"][0]["will_execute"] = False
    else:
        record["execution_results"][0]["raw_llm_response"] = "must-not-be-silently-accepted"
    with pytest.raises(ValueError):
        manifest_from_record(record)
