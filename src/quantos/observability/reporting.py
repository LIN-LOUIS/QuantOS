"""Human-readable, JSON, and logging output for run health reports."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from .models import ModuleResult, ResultStatus, RunHealthReport, ValidationResult


_DISPLAY_STATUS = {
    ResultStatus.SUCCESS: "PASS",
    ResultStatus.WARNING: "WARN",
    ResultStatus.FAILED: "FAIL",
    ResultStatus.SKIPPED: "SKIP",
}


def render_health_report(report: RunHealthReport) -> str:
    lines = [
        f"Run ID          {report.run_id}",
        f"As Of Time      {report.as_of_time.isoformat()}",
        "",
    ]
    for module in report.modules:
        lines.append(
            f"{module.module_name:<16} {_DISPLAY_STATUS[module.status]:<5} "
            f"input={module.input_count} output={module.output_count} "
            f"{module.duration_ms:.1f} ms"
        )
    lines.extend(("", f"Overall         {report.status.value.upper()}"))
    return "\n".join(lines)


def health_report_to_dict(report: RunHealthReport) -> dict[str, object]:
    return {
        "run_id": report.run_id,
        "job_type": report.job_context.job_type.value,
        "trade_date": report.job_context.trade_date.isoformat(),
        "as_of_time": report.as_of_time.isoformat(),
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "duration_ms": report.duration_ms,
        "status": report.status.value,
        "warning_count": report.warning_count,
        "error_count": report.error_count,
        "modules": [_module_to_dict(module) for module in report.modules],
    }


def write_health_report(report: RunHealthReport, target: Path) -> None:
    """Atomically create a report and refuse to overwrite an existing run."""

    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        health_report_to_dict(report), ensure_ascii=False, indent=2
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.link(temporary_path, target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def save_health_report(report: RunHealthReport, report_dir: Path) -> Path:
    target = report_dir / f"run-{report.run_id}.json"
    write_health_report(report, target)
    return target


def log_health_report(
    report: RunHealthReport, logger: logging.Logger | None = None
) -> None:
    target_logger = logger or logging.getLogger("quantos.observability")
    log_method = {
        "healthy": target_logger.info,
        "degraded": target_logger.warning,
        "unhealthy": target_logger.error,
    }[report.status.value]
    log_method(
        "QuantOS run health=%s run_id=%s warnings=%d errors=%d",
        report.status.value,
        report.run_id,
        report.warning_count,
        report.error_count,
    )


def _module_to_dict(module: ModuleResult) -> dict[str, object]:
    return {
        "module_name": module.module_name,
        "run_id": module.run_id,
        "started_at": module.started_at.isoformat(),
        "finished_at": module.finished_at.isoformat(),
        "duration_ms": module.duration_ms,
        "status": module.status.value,
        "input_count": module.input_count,
        "output_count": module.output_count,
        "metrics": dict(module.metrics),
        "warnings": list(module.warnings),
        "errors": list(module.errors),
        "as_of_time": module.as_of_time.isoformat(),
        "dependency_failures": list(module.dependency_failures),
        "validations": [_validation_to_dict(item) for item in module.validations],
    }


def _validation_to_dict(result: ValidationResult) -> dict[str, object]:
    return {
        "rule_name": result.rule_name,
        "category": result.category.value,
        "status": result.status.value,
        "checked_count": result.checked_count,
        "failed_count": result.failed_count,
        "message": result.message,
        "metrics": dict(result.metrics),
    }
