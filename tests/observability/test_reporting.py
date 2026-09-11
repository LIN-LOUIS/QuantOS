import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantos.observability import (
    ModuleResult,
    ResultStatus,
    RunHealthReport,
    log_health_report,
    render_health_report,
    save_health_report,
    write_health_report,
)
from quantos.observability.reporting import health_report_to_dict
from quantos.schemas import JobContext, JobType

SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_report(status: ResultStatus = ResultStatus.SUCCESS) -> RunHealthReport:
    context = JobContext(
        run_id="run-report",
        job_type=JobType.MARKET_INGESTION,
        trade_date=date(2026, 8, 26),
        as_of_time=datetime(2026, 8, 26, 15, 0, tzinfo=SHANGHAI),
    )
    started = datetime(2026, 8, 26, 15, 1, tzinfo=SHANGHAI)
    module = ModuleResult(
        module_name="Collector",
        job_context=context,
        started_at=started,
        finished_at=started + timedelta(milliseconds=2),
        duration_ms=1.75,
        status=status,
        input_count=1,
        output_count=4,
        warnings=("market closed",) if status is ResultStatus.WARNING else (),
        errors=("request failed",) if status is ResultStatus.FAILED else (),
    )
    return RunHealthReport.from_modules(
        job_context=context,
        started_at=started,
        finished_at=module.finished_at,
        duration_ms=2,
        modules=(module,),
    )


def test_render_health_report_has_cli_summary() -> None:
    rendered = render_health_report(make_report())

    assert "Collector        PASS" in rendered
    assert "input=1 output=4" in rendered
    assert "Overall         HEALTHY" in rendered


def test_write_health_report_creates_json_artifact(tmp_path: Path) -> None:
    target = tmp_path / "observability" / "run-report.json"
    write_health_report(make_report(), target)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-report"
    assert payload["status"] == "healthy"
    assert payload["modules"][0]["as_of_time"].endswith("+08:00")
    assert payload["modules"][0]["duration_ms"] == 1.75


def test_saved_report_round_trips_existing_serialization(tmp_path: Path) -> None:
    report = make_report()
    target = save_health_report(report, tmp_path)

    assert target.name == "run-run-report.json"
    assert json.loads(target.read_text(encoding="utf-8")) == health_report_to_dict(
        report
    )


def test_write_health_report_does_not_overwrite_existing_run(tmp_path: Path) -> None:
    report = make_report()
    target = save_health_report(report, tmp_path)
    original = target.read_text(encoding="utf-8")

    with pytest.raises(FileExistsError):
        save_health_report(report, tmp_path)

    assert target.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*.tmp"))


def test_log_health_report_uses_warning_for_degraded_run(caplog) -> None:
    report = make_report(ResultStatus.WARNING)

    with caplog.at_level(logging.WARNING, logger="quantos.observability.test"):
        log_health_report(report, logging.getLogger("quantos.observability.test"))

    assert "health=degraded" in caplog.text
    assert "run_id=run-report" in caplog.text
