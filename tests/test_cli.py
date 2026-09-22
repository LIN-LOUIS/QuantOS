from datetime import date, datetime, timedelta
import json
from zoneinfo import ZoneInfo

import pytest

from quantos import cli
from quantos.observability import ModuleResult, ResultStatus, RunHealthReport
from quantos.schemas import JobContext, JobType

SHANGHAI = ZoneInfo("Asia/Shanghai")
REAL_BUILD_PROVIDER = cli._build_market_provider


def make_report(status: ResultStatus) -> RunHealthReport:
    context = JobContext(
        run_id="cli-run",
        job_type=JobType.MARKET_INGESTION,
        trade_date=date(2024, 1, 5),
        as_of_time=datetime(2024, 1, 5, 15, 0, tzinfo=SHANGHAI),
    )
    started = datetime(2024, 1, 5, 16, 0, tzinfo=SHANGHAI)
    module = ModuleResult(
        module_name="Collector",
        job_context=context,
        started_at=started,
        finished_at=started + timedelta(milliseconds=1),
        duration_ms=1,
        status=status,
        input_count=1,
        output_count=1,
        warnings=("market warning",) if status is ResultStatus.WARNING else (),
        errors=("system failure",) if status is ResultStatus.FAILED else (),
    )
    return RunHealthReport.from_modules(
        job_context=context,
        started_at=started,
        finished_at=module.finished_at,
        duration_ms=1,
        modules=(module,),
    )


class FakeRunner:
    report = make_report(ResultStatus.SUCCESS)

    def __init__(self, **kwargs) -> None:
        pass

    def run(self, **kwargs):
        return self.report


@pytest.fixture(autouse=True)
def avoid_real_provider(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_build_market_provider", lambda name: object())


def arguments(tmp_path, *, json_output: bool = False) -> list[str]:
    result = [
        "health",
        "--symbol",
        "600519.SH",
        "--start-date",
        "2024-01-02",
        "--end-date",
        "2024-01-05",
        "--as-of-time",
        "2024-01-05T23:59:59+08:00",
        "--project-root",
        str(tmp_path),
    ]
    if json_output:
        result.append("--json")
    return result


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [
        (ResultStatus.SUCCESS, 0),
        (ResultStatus.WARNING, 1),
        (ResultStatus.FAILED, 2),
    ],
)
def test_cli_exit_code_reflects_health(
    monkeypatch, tmp_path, status: ResultStatus, exit_code: int
) -> None:
    FakeRunner.report = make_report(status)
    monkeypatch.setattr(cli, "HealthPipelineRunner", FakeRunner)

    assert cli.main(arguments(tmp_path)) == exit_code


def test_cli_renders_json_from_run_health_report(monkeypatch, tmp_path, capsys) -> None:
    FakeRunner.report = make_report(ResultStatus.SUCCESS)
    monkeypatch.setattr(cli, "HealthPipelineRunner", FakeRunner)

    exit_code = cli.main(arguments(tmp_path, json_output=True))
    output = capsys.readouterr().out

    assert exit_code == 0
    assert '"run_id": "cli-run"' in output
    assert '"status": "healthy"' in output
    assert '"modules"' in output


def test_cli_renders_text_health_report(monkeypatch, tmp_path, capsys) -> None:
    FakeRunner.report = make_report(ResultStatus.SUCCESS)
    monkeypatch.setattr(cli, "HealthPipelineRunner", FakeRunner)

    cli.main(arguments(tmp_path))
    output = capsys.readouterr().out

    assert "Collector        PASS" in output
    assert "Overall         HEALTHY" in output


def test_cli_report_dir_override_saves_existing_report_model(
    monkeypatch, tmp_path, capsys
) -> None:
    FakeRunner.report = make_report(ResultStatus.SUCCESS)
    monkeypatch.setattr(cli, "HealthPipelineRunner", FakeRunner)
    report_dir = tmp_path / "custom-reports"
    args = arguments(tmp_path, json_output=True) + [
        "--save-report",
        "--report-dir",
        str(report_dir),
    ]

    assert cli.main(args) == 0

    target = report_dir / "run-cli-run.json"
    assert target.is_file()
    assert '"run_id": "cli-run"' in target.read_text(encoding="utf-8")
    assert str(target) in capsys.readouterr().err


@pytest.mark.parametrize("provider_name", ["tushare", "eastmoney"])
def test_cli_selects_market_provider(monkeypatch, tmp_path, provider_name) -> None:
    selected = []
    monkeypatch.setattr(
        cli, "_build_market_provider", lambda name: selected.append(name) or object()
    )
    monkeypatch.setattr(cli, "HealthPipelineRunner", FakeRunner)

    assert cli.main(arguments(tmp_path) + ["--provider", provider_name]) == 0
    assert selected == [provider_name]


def test_cli_rejects_invalid_provider(tmp_path) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(arguments(tmp_path, json_output=True) + ["--provider", "baostock"])
    assert info.value.code == 2


def test_cli_missing_tushare_token_fails_without_polluting_json_stdout(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(cli, "_build_market_provider", REAL_BUILD_PROVIDER)
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)

    assert cli.main(arguments(tmp_path, json_output=True)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "TUSHARE_TOKEN is not configured" in captured.err


def test_top_level_help_exposes_only_real_user_commands_and_product_wording(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--help"])
    assert info.value.code == 0
    output = capsys.readouterr().out
    assert "{doctor,demo,health,status,serve,start,data,replay,report,scheduler,ask}" in output
    assert "collect market data and run pipeline health checks" in output
    for internal_name in ("Phase 1A", "Phase 3C.2", "Phase 4F", "E2E.3"):
        assert internal_name not in output


def test_start_help_exposes_product_options_and_local_default(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["start", "--help"])

    assert stopped.value.code == 0
    output = capsys.readouterr().out
    assert "--no-browser" in output
    assert "--demo" in output
    assert "--project-root" in output
    assert "--host" in output
    assert "--port" in output


def test_demo_start_preserves_release_commit_before_switching_workspace(
    tmp_path, monkeypatch,
):
    from quantos.config import Settings

    workspace = tmp_path / "web" / "dist"
    (workspace / "assets").mkdir(parents=True)
    (workspace / "index.html").write_text("<main>QuantOS</main>", encoding="utf-8")
    (tmp_path / "release-manifest.json").write_text(
        '{"git_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
        encoding="utf-8",
    )
    received = {}
    monkeypatch.setattr(
        "quantos.product.prepare_demo_workspace",
        lambda _root: Settings.from_project_root(tmp_path / "temporary-demo"),
    )
    monkeypatch.setattr(
        "quantos.product.launch_product",
        lambda **kwargs: received.update(kwargs) or 0,
    )

    result = cli.main([
        "start", "--project-root", str(tmp_path), "--demo", "--no-browser",
    ])

    assert result == 0
    assert received["build_commit"] == "a" * 40


def test_version_is_available_from_unified_cli(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out == "quantos 0.3.1\n"


def test_doctor_json_is_offline_read_only_and_needs_no_credentials(
    tmp_path, monkeypatch, capsys,
):
    workspace = tmp_path / "web" / "dist"
    (workspace / "assets").mkdir(parents=True)
    (workspace / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setattr(
        "quantos.product.select_local_port", lambda *_args, **_kwargs: 8000,
    )

    assert cli.main([
        "doctor", "--project-root", str(tmp_path), "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PASS"
    assert payload["offline"] is True
    assert payload["read_only"] is True
    assert payload["credential_required"] is False
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["workspace_assets"]["status"] == "PASS"
    assert checks["demo_mode"]["status"] == "PASS"
    assert checks["port_strategy"]["status"] == "PASS"
    assert all(check["status"] == "PASS" for check in payload["checks"])


def test_doctor_uses_workspace_assets_from_explicit_project_root(
    tmp_path, monkeypatch, capsys,
):
    project_root = tmp_path / "release"
    assets = project_root / "web" / "dist" / "assets"
    assets.mkdir(parents=True)
    (assets.parent / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "installed" / "quantos" / "cli.py"))

    assert cli.main([
        "doctor", "--project-root", str(project_root), "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["workspace_assets"] == {
        "name": "workspace_assets",
        "status": "PASS",
        "detail": "built assets: dist",
    }


def test_demo_json_uses_audited_synthetic_evaluation(monkeypatch, capsys):
    called = {}

    class Result:
        summary = {
            "data_source": "SYNTHETIC_FIXTURE",
            "provider_network_request_count": 0,
            "real_deepseek_request_count": 0,
            "embedding_provider_request_count": 0,
            "pit_violation_count": 0,
            "trading_days_evaluated": 2,
            "candidate_count": 4,
            "evaluation_id": "fixture-id",
        }

    def run(output_dir, *, trading_days, candidate_count):
        called.update({
            "output_dir": output_dir,
            "trading_days": trading_days,
            "candidate_count": candidate_count,
        })
        return Result()

    monkeypatch.setattr("quantos.evaluation.run_synthetic_strict_evaluation", run)
    assert cli.main(["demo", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert called["trading_days"] == called["candidate_count"] == 2
    assert payload == {
        "command": "demo",
        "status": "PASS",
        "data_source": "SYNTHETIC_FIXTURE",
        "synthetic_disclaimer": "PRESENT",
        "credential_required": False,
        "real_provider_requests": 0,
        "real_llm_requests": 0,
        "embedding_requests": 0,
        "pit_violations": 0,
        "trading_days_evaluated": 2,
        "candidate_count": 4,
        "evaluation_id": "fixture-id",
    }
