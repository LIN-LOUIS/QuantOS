from datetime import date, datetime, timedelta
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
