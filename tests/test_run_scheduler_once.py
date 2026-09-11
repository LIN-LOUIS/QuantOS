"""Thin CLI contract tests for the Phase 3D.3 one-shot entry point."""

from datetime import datetime
import importlib.util
import json
from pathlib import Path
import socket

import pytest

from quantos.config import MARKET_TIMEZONE
from quantos.schemas.scheduler_ops import LOCAL_CALENDAR_SCHEMA_VERSION


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("scheduler CLI must not access network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location(
        "run_scheduler_once_cli", Path("scripts/run_scheduler_once.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def calendar_path(tmp_path):
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps({
        "schema_version": LOCAL_CALENDAR_SCHEMA_VERSION,
        "market": "CN_A_SHARE",
        "timezone": "Asia/Shanghai",
        "coverage_start": "2026-08-31",
        "coverage_end": "2026-09-06",
        "trading_dates": ["2026-08-31", "2026-09-01", "2026-09-02",
                          "2026-09-03", "2026-09-04"],
        "generated_at": "2026-08-30T12:00:00+08:00",
    }), encoding="utf-8")
    return path


@pytest.mark.parametrize("args", [
    [],
    ["--calendar", "x", "--runtime-db", "y"],
    ["--mode", "research", "--runtime-db", "y"],
    ["--mode", "research", "--calendar", "x"],
])
def test_cli_requires_mode_calendar_and_runtime_db(cli, args):
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


def test_cli_imports_and_dry_run_outputs_canonical_json(
    cli, calendar_path, tmp_path, capsys,
):
    runtime = tmp_path / "not-created" / "runtime.sqlite3"
    code = cli.main([
        "--mode", "research", "--calendar", str(calendar_path),
        "--runtime-db", str(runtime), "--at", "2026-09-01T09:00:00+08:00",
        "--dry-run", "--no-llm",
    ])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["overall_status"] == "DRY_RUN"
    assert output["evaluated_at"] == "2026-09-01T09:00:00+08:00"
    assert not runtime.exists()


def test_at_requires_dry_run(cli, calendar_path, tmp_path):
    with pytest.raises(SystemExit) as error:
        cli.main([
            "--mode", "strict_live", "--calendar", str(calendar_path),
            "--runtime-db", str(tmp_path / "runtime.sqlite3"),
            "--at", "2026-09-01T09:00:00+08:00",
        ])
    assert error.value.code == 2


def test_at_must_be_timezone_aware(cli, calendar_path, tmp_path):
    with pytest.raises(SystemExit) as error:
        cli.main([
            "--mode", "research", "--calendar", str(calendar_path),
            "--runtime-db", str(tmp_path / "runtime.sqlite3"),
            "--at", "2026-09-01T09:00:00", "--dry-run",
        ])
    assert error.value.code == 2


def test_execution_window_config_fails_before_runtime_db(
    cli, calendar_path, tmp_path,
):
    runtime = tmp_path / "scheduler" / "runtime.sqlite3"
    with pytest.raises(SystemExit) as error:
        cli.main([
            "--mode", "strict_live", "--calendar", str(calendar_path),
            "--runtime-db", str(runtime), "--project-root", str(tmp_path),
            "--max-execution-minutes", "60",
        ])
    assert error.value.code == 2
    assert not runtime.exists()


def test_invalid_environment_config_has_controlled_exit(
    cli, calendar_path, tmp_path, capsys, monkeypatch,
):
    monkeypatch.setenv("QUANTOS_SYNTHESIS_TOP_N", "invalid")
    runtime = tmp_path / "scheduler" / "runtime.sqlite3"
    code = cli.main([
        "--mode", "strict_live", "--calendar", str(calendar_path),
        "--runtime-db", str(runtime), "--dry-run",
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": "OPERATIONAL_CONFIG_INVALID"}
    assert not runtime.exists()


def test_utc_at_converts_to_shanghai_independent_of_machine_timezone(
    cli, calendar_path, tmp_path, capsys, monkeypatch,
):
    monkeypatch.setenv("TZ", "America/New_York")
    code = cli.main([
        "--mode", "research", "--calendar", str(calendar_path),
        "--runtime-db", str(tmp_path / "runtime.sqlite3"),
        "--at", "2026-09-01T01:00:00+00:00", "--dry-run",
    ])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert datetime.fromisoformat(output["evaluated_at"]).astimezone(MARKET_TIMEZONE).hour == 9


def test_corrupt_calendar_has_stable_exit_code(cli, tmp_path, capsys):
    calendar = tmp_path / "calendar.json"
    calendar.write_text("not-json", encoding="utf-8")
    code = cli.main([
        "--mode", "research", "--calendar", str(calendar),
        "--runtime-db", str(tmp_path / "runtime.sqlite3"), "--dry-run",
    ])
    error = json.loads(capsys.readouterr().err)
    assert code == 3 and error == {"error_code": "CALENDAR_ARTIFACT_INVALID"}


def test_calendar_out_of_coverage_has_stable_exit_and_no_db(
    cli, calendar_path, tmp_path, capsys,
):
    runtime = tmp_path / "runtime.sqlite3"
    code = cli.main([
        "--mode", "research", "--calendar", str(calendar_path),
        "--runtime-db", str(runtime), "--at", "2026-09-07T09:00:00+08:00",
        "--dry-run",
    ])
    error = json.loads(capsys.readouterr().err)
    assert code == 3 and error == {"error_code": "CALENDAR_OUT_OF_COVERAGE"}
    assert not runtime.exists()


def test_production_out_of_coverage_fails_before_runtime_db_creation(
    cli, calendar_path, tmp_path, capsys, monkeypatch,
):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, 1, tzinfo=tz)

    monkeypatch.setattr(cli, "datetime", FixedDateTime)
    runtime = tmp_path / "scheduler" / "runtime.sqlite3"
    code = cli.main([
        "--mode", "strict_live", "--calendar", str(calendar_path),
        "--runtime-db", str(runtime), "--project-root", str(tmp_path), "--no-llm",
    ])
    error = json.loads(capsys.readouterr().err)
    assert code == 3 and error == {"error_code": "CALENDAR_OUT_OF_COVERAGE"}
    assert not runtime.exists()


def test_runtime_database_configuration_failure_has_stable_exit(
    cli, calendar_path, tmp_path, capsys, monkeypatch,
):
    import quantos.scheduler_ops as ops

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 1, 1, tzinfo=tz)

    monkeypatch.setattr(cli, "datetime", FixedDateTime)
    monkeypatch.setattr(cli, "QuantOSRunExecutorAdapter", lambda settings: object())
    blocked_parent = tmp_path / "file"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    code = cli.main([
        "--mode", "strict_live", "--calendar", str(calendar_path),
        "--runtime-db", str(blocked_parent / "runtime.sqlite3"),
        "--project-root", str(tmp_path), "--no-llm",
    ])
    error = json.loads(capsys.readouterr().err)
    assert code == 4 and error == {"error_code": "SCHEDULER_RUNTIME_STORAGE_FAILURE"}


def test_cli_does_not_persist_or_print_secret_values(
    cli, calendar_path, tmp_path, capsys, monkeypatch,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "forbidden-test-secret")
    cli.main([
        "--mode", "research", "--calendar", str(calendar_path),
        "--runtime-db", str(tmp_path / "runtime.sqlite3"),
        "--at", "2026-09-01T08:00:00+08:00", "--dry-run",
    ])
    captured = capsys.readouterr()
    assert "forbidden-test-secret" not in captured.out + captured.err
