"""User command wrappers over existing local library execution paths."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import importlib.util
import inspect
import json
from pathlib import Path
import socket

import pytest

from quantos import cli
from quantos.commands import CommandFailure
from quantos.commands import report as report_command
from quantos.commands.status import inspect_status
from quantos.config import Settings
from quantos.schemas.run import RunType
from quantos.schemas.scheduler_ops import LOCAL_CALENDAR_SCHEMA_VERSION
from quantos.storage import MarketDataRepository
from tests.storage.test_market import make_daily_bar


NOW = "2026-09-11T18:00:00+08:00"
PRE = "2026-09-12T09:00:00+08:00"


def _script(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, Path(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _daily_result() -> dict[str, object]:
    return {
        "status": "PASS", "command": "report daily",
        "trade_date": "2026-09-11", "mode": "strict_live",
        "report_id": "a" * 64, "json_path": "/safe/report.json",
        "markdown_path": "/safe/report.md", "total_candidates": 3,
        "selected_candidates": 2, "llm_requests": 0,
        "knowledge_status": "READY", "provider_network_requests": 0,
        "synthetic_fallback_count": 0,
    }


def _daily_args(root: Path, *, json_output=False) -> list[str]:
    values = [
        "report", "daily", "--trade-date", "2026-09-11",
        "--as-of-time", NOW, "--mode", "strict_live", "--top-n", "2",
        "--no-llm", "--project-root", str(root),
    ]
    if json_output:
        values.append("--json")
    return values


def test_help_tree_contains_only_supported_commands(capsys):
    for arguments, expected in (
        (["report", "--help"], "{daily,pre-open,post-close}"),
        (["scheduler", "--help"], "{once}"),
    ):
        with pytest.raises(SystemExit) as caught:
            cli.main(arguments)
        assert caught.value.code == 0
        assert expected in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    top = capsys.readouterr().out
    assert all(name in top for name in (
        "doctor", "demo", "health", "status", "report", "scheduler",
    ))
    assert "intraday" not in top.lower()


def test_status_human_and_json_are_offline_read_only(tmp_path, monkeypatch, capsys):
    root = tmp_path / "absent-workspace"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("status must not access network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert cli.main(["status", "--project-root", str(root)]) == 0
    human = capsys.readouterr().out
    assert "QuantOS Local Status" in human
    assert "Market Data         EMPTY" in human
    assert "Daily Report        NONE" in human
    assert not root.exists()

    assert cli.main(["status", "--project-root", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["offline"] is payload["read_only"] is True
    assert payload["provider_requests"] == 0
    assert payload["market"]["readiness"] == "EMPTY"
    assert payload["evidence"]["readiness"] == "EMPTY"
    assert payload["daily_report"]["availability"] == "NONE"
    assert not root.exists()


def test_status_reports_local_artifact_availability_without_reading_content(tmp_path):
    paths = (
        tmp_path / "data/normalized/market/trade_date=2026-09-11/x.parquet",
        tmp_path / "data/normalized/news/publish_date=2026-09-11/x.parquet",
        tmp_path / "data/knowledge/documents/family/x.json",
        tmp_path / "data/knowledge/indexes/lexical/index.json",
        tmp_path / "data/derived/reports/trade_date=2026-09-11/report.json",
        tmp_path / "data/derived/time_slice_reports/target_trade_date=2026-09-12/report.json",
        tmp_path / "data/runtime/scheduler/scheduler_runtime.sqlite3",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not inspected")
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    result = inspect_status(tmp_path)
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert before == after
    assert result["market"] == {"readiness": "READY", "file_count": 1}
    assert result["evidence"] == {"readiness": "READY", "file_count": 1}
    assert result["knowledge"]["readiness"] == "READY"
    assert result["daily_report"]["latest_trade_date"] == "2026-09-11"
    assert result["time_slice"]["latest_trade_date"] == "2026-09-12"
    assert result["scheduler_runtime"]["availability"] == "AVAILABLE"


def test_daily_wrapper_argument_parity_human_and_json(tmp_path, monkeypatch, capsys):
    captured = []

    def execute(options, *, settings):
        captured.append((options, settings.project_root))
        return _daily_result()

    monkeypatch.setattr(report_command, "execute_daily", execute)
    assert cli.main(_daily_args(tmp_path)) == 0
    human = capsys.readouterr().out
    assert "Daily Intelligence Complete" in human
    assert "Report ID" in human and "a" * 64 in human
    assert "LLM Requests      0" in human

    assert cli.main(_daily_args(tmp_path, json_output=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["report_id"] == "a" * 64
    options, root = captured[-1]
    assert options.trade_date == date(2026, 9, 11)
    assert options.as_of_time == datetime.fromisoformat(NOW)
    assert (options.mode, options.top_n, options.no_llm) == ("strict_live", 2, True)
    assert root == tmp_path.resolve()


def test_daily_no_data_is_safe_json_failure_without_fallback(tmp_path, capsys):
    root = tmp_path / "missing"
    code = cli.main(_daily_args(root, json_output=True))
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["error_code"] == "MARKET_ARTIFACTS_MISSING"
    assert "traceback" not in json.dumps(payload).lower()
    assert "synthetic" not in json.dumps(payload).lower()
    assert not root.exists()


def test_daily_does_not_treat_another_trade_date_as_ready(tmp_path, capsys):
    repository = MarketDataRepository(Settings.from_project_root(tmp_path))
    repository.write_daily_bars([make_daily_bar(date(2026, 9, 10))])
    code = cli.main(_daily_args(tmp_path, json_output=True))
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["error_code"] == "MARKET_ARTIFACTS_MISSING"


@pytest.mark.parametrize("command", ("daily", "pre-open", "post-close"))
def test_report_wrappers_reject_naive_as_of_time(command, tmp_path):
    arguments = [
        "report", command, "--trade-date", "2026-09-11",
        "--as-of-time", "2026-09-11T18:00:00", "--mode", "strict_live",
        "--no-llm", "--project-root", str(tmp_path),
    ]
    with pytest.raises(SystemExit) as caught:
        cli.main(arguments)
    assert caught.value.code == 2


def test_report_json_rejects_naive_time_as_structured_error(tmp_path, capsys):
    code = cli.main([
        "report", "daily", "--trade-date", "2026-09-11",
        "--as-of-time", "2026-09-11T18:00:00", "--mode", "strict_live",
        "--no-llm", "--json", "--project-root", str(tmp_path),
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "FAIL"
    assert payload["error_code"] == "TIMEZONE_REQUIRED"
    assert "traceback" not in json.dumps(payload).lower()


@pytest.mark.parametrize(
    ("command", "cutoff", "run_type"),
    (("pre-open", PRE, RunType.PRE_OPEN),
     ("post-close", NOW, RunType.POST_CLOSE)),
)
def test_time_slice_wrappers_preserve_run_type_and_ids(
    command, cutoff, run_type, tmp_path, monkeypatch, capsys,
):
    captured = []
    result = {
        "status": "PASS", "command": f"report {command}",
        "run_id": "run-id", "manifest_path": "/safe/manifest.json",
        "report_id": "b" * 64, "report_type": command.upper(),
        "json_path": "/safe/slice.json", "markdown_path": "/safe/slice.md",
        "daily_report_id": "a" * 64, "daily_report_ref": "/safe/daily.json",
        "market_basis_trade_date": "2026-09-11",
        "REAL_DEEPSEEK_REQUEST_COUNT": 0,
        "PROVIDER_NETWORK_REQUEST_COUNT": 0, "plan_only": False,
        "knowledge_status": "READY", "knowledge_reason_code": "KNOWLEDGE_READY",
        "synthetic_fallback_count": 0,
    }

    def execute(options, *, settings):
        captured.append(options)
        return result

    monkeypatch.setattr(report_command, "execute_time_slice", execute)
    arguments = [
        "report", command, "--trade-date", "2026-09-12",
        "--as-of-time", cutoff, "--mode", "research", "--top-n", "2",
        "--no-llm", "--project-root", str(tmp_path), "--json",
    ]
    assert cli.main(arguments) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["run_id"] == "run-id"
    assert output["report_id"] == "b" * 64
    assert output["daily_report_id"] == "a" * 64
    assert captured[0].run_type is run_type


def test_time_slice_shared_executor_does_not_reimplement_forbidden_work():
    source = inspect.getsource(report_command.execute_time_slice)
    assert "retrieve_knowledge" not in source
    assert "prepare_candidate_synthesis_input" not in source
    assert "synthesize_evidence" not in source
    assert "generate_daily_report(" not in source
    assert "load_daily_reference" in source


def test_time_slice_old_and_new_wrappers_preserve_run_and_reason_identity(
    tmp_path, monkeypatch, capsys,
):
    arguments = [
        "--trade-date", "2026-09-12", "--as-of-time", PRE,
        "--mode", "research", "--top-n", "2", "--no-llm",
    ]
    assert cli.main([
        "report", "pre-open", *arguments,
        "--project-root", str(tmp_path / "new"), "--json",
    ]) == 1
    new = json.loads(capsys.readouterr().out)

    old_cli = _script("scripts/generate_time_slice_report.py", "slice_script_parity")
    monkeypatch.setattr(
        old_cli, "DEFAULT_SETTINGS", Settings.from_project_root(tmp_path / "old"),
    )
    assert old_cli.main(["--run-type", "PRE_OPEN", *arguments]) == 1
    old = json.loads(capsys.readouterr().out)
    assert new["run_id"] == old["run_id"]
    assert new["error_code"] == old["error_code"] == "PRODUCT_UNAVAILABLE"


def test_old_report_scripts_use_shared_library_implementation():
    daily = _script("scripts/generate_daily_report.py", "daily_script_shared")
    sliced = _script("scripts/generate_time_slice_report.py", "slice_script_shared")
    assert daily.execute_daily is report_command.execute_daily
    assert sliced.execute_time_slice is report_command.execute_time_slice


def test_daily_old_script_and_new_cli_produce_same_report_id(
    tmp_path, monkeypatch, capsys,
):
    new_root = tmp_path / "new"
    old_root = tmp_path / "old"
    days = tuple(date(2026, 8, 20) + timedelta(days=index) for index in range(23))
    for root in (new_root, old_root):
        repository = MarketDataRepository(Settings.from_project_root(root))
        repository.write_daily_bars([make_daily_bar(day) for day in days])
    arguments = [
        "--trade-date", "2026-09-11", "--as-of-time", NOW,
        "--mode", "strict_live", "--top-n", "2", "--no-llm",
    ]
    assert cli.main([
        "report", "daily", *arguments, "--project-root", str(new_root), "--json",
    ]) == 0
    new = json.loads(capsys.readouterr().out)

    old_cli = _script("scripts/generate_daily_report.py", "daily_script_parity")
    monkeypatch.setattr(old_cli, "DEFAULT_SETTINGS", Settings.from_project_root(old_root))
    assert old_cli.main(arguments) == 0
    old = json.loads(capsys.readouterr().out)
    assert new["report_id"] == old["report_id"]
    assert new["total_candidates"] == old["total_candidates"]
    assert new["selected_candidates"] == old["selected_candidates"]
    assert new["llm_requests"] == old["llm_requests"] == 0


def _calendar(path: Path) -> None:
    path.write_text(json.dumps({
        "schema_version": LOCAL_CALENDAR_SCHEMA_VERSION,
        "market": "CN_A_SHARE", "timezone": "Asia/Shanghai",
        "coverage_start": "2026-08-31", "coverage_end": "2026-09-06",
        "trading_dates": [
            "2026-08-31", "2026-09-01", "2026-09-02",
            "2026-09-03", "2026-09-04",
        ],
        "generated_at": "2026-08-30T12:00:00+08:00",
    }), encoding="utf-8")


def test_scheduler_once_new_and_old_wrappers_have_identical_dry_run_result(
    tmp_path, capsys,
):
    calendar = tmp_path / "calendar.json"
    runtime = tmp_path / "runtime.sqlite3"
    _calendar(calendar)
    common = [
        "--mode", "strict_live", "--calendar", str(calendar),
        "--runtime-db", str(runtime), "--at", "2026-09-01T09:00:00+08:00",
        "--dry-run", "--no-llm", "--top-n", "2",
    ]
    assert cli.main(["scheduler", "once", *common]) == 0
    new = json.loads(capsys.readouterr().out)
    old_cli = _script("scripts/run_scheduler_once.py", "scheduler_script_shared")
    assert old_cli.main(common) == 0
    old = json.loads(capsys.readouterr().out)
    stable_keys = (
        "schema_version", "evaluated_at", "mode", "dry_run", "overall_status",
        "calendar_market", "calendar_timezone", "calendar_coverage_start",
        "calendar_coverage_end", "slot_results",
    )
    assert {key: new[key] for key in stable_keys} == {
        key: old[key] for key in stable_keys
    }
    assert new["overall_status"] == "DRY_RUN"
    assert not runtime.exists()


def test_scheduler_reason_code_parity_for_invalid_calendar(tmp_path, capsys):
    calendar = tmp_path / "calendar.json"
    calendar.write_text("invalid", encoding="utf-8")
    common = [
        "--mode", "research", "--calendar", str(calendar),
        "--runtime-db", str(tmp_path / "runtime.sqlite3"), "--dry-run",
    ]
    assert cli.main(["scheduler", "once", *common]) == 3
    new = json.loads(capsys.readouterr().err)
    old_cli = _script("scripts/run_scheduler_once.py", "scheduler_script_error")
    assert old_cli.main(common) == 3
    old = json.loads(capsys.readouterr().err)
    assert new["error_code"] == old["error_code"] == "CALENDAR_ARTIFACT_INVALID"


def test_real_command_failure_never_echoes_secret_or_uses_synthetic_fallback(
    tmp_path, monkeypatch, capsys,
):
    secret = "phase5b-secret-must-not-escape"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    monkeypatch.setattr(
        report_command, "execute_daily",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(CommandFailure(
            "REPORT_ARTIFACTS_NOT_READY", "Prepare local artifacts.", 3,
        )),
    )
    assert cli.main(_daily_args(tmp_path, json_output=True)) == 3
    output = capsys.readouterr().out
    assert secret not in output
    assert "synthetic" not in output.lower()
    assert json.loads(output)["error_code"] == "REPORT_ARTIFACTS_NOT_READY"
