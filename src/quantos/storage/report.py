"""Atomic append-only persistence for Phase 3B daily reports."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from quantos.config import Settings
from quantos.schemas import DailyIntelligenceReport

from .market import StorageError


class DailyReportRepository:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_directories()

    def write(self, report: DailyIntelligenceReport) -> tuple[Path, Path]:
        from quantos.reporting import (
            canonical_json_bytes, render_daily_report, report_from_dict, report_to_dict,
            validate_daily_report_identity,
        )
        try:
            validate_daily_report_identity(report)
        except (TypeError, ValueError) as exc:
            raise StorageError("daily report identity collision or corruption") from exc
        generation_id = uuid4().hex
        target_dir = self.settings.report_dir / f"trade_date={report.trade_date.isoformat()}"
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{report.report_id}-{generation_id}"
        json_path, markdown_path = target_dir / f"{stem}.json", target_dir / f"{stem}.md"
        if json_path.exists() or markdown_path.exists():
            raise StorageError("daily report target already exists")
        json_temp = target_dir / f".{stem}.json.tmp"
        markdown_temp = target_dir / f".{stem}.md.tmp"
        published: list[Path] = []
        try:
            json_temp.write_bytes(canonical_json_bytes(report_to_dict(report)) + b"\n")
            persisted = report_from_dict(json.loads(
                json_temp.read_text(encoding="utf-8"),
            ))
            markdown_temp.write_text(render_daily_report(persisted), encoding="utf-8")
            os.replace(json_temp, json_path)
            published.append(json_path)
            os.replace(markdown_temp, markdown_path)
            published.append(markdown_path)
        except Exception as exc:
            for temporary in (json_temp, markdown_temp):
                temporary.unlink(missing_ok=True)
            for target in published:
                target.unlink(missing_ok=True)
            if isinstance(exc, StorageError):
                raise
            raise StorageError("failed to persist complete daily report pair") from exc
        return json_path, markdown_path

    def read_json(self, path: Path) -> DailyIntelligenceReport:
        from quantos.reporting import report_from_dict
        try:
            return report_from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            raise StorageError("failed to read canonical daily report") from exc
