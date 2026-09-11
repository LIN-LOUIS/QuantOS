import json

import pytest

from quantos.config import Settings
from quantos.reporting import render_daily_report
from quantos.storage import DailyReportRepository, StorageError
from tests.test_reporting import build_report


def test_report_storage_is_append_only_atomic_pair_with_same_stem(tmp_path):
    report, _, _ = build_report(tmp_path / "inputs")
    repository = DailyReportRepository(Settings.from_project_root(tmp_path / "outputs"))
    first_json, first_md = repository.write(report)
    second_json, second_md = repository.write(report)
    assert first_json.stem == first_md.stem and second_json.stem == second_md.stem
    assert first_json != second_json and first_md != second_md
    assert first_json.parent.name == f"trade_date={report.trade_date}"
    assert repository.read_json(first_json).report_id == report.report_id
    assert first_md.read_text() == render_daily_report(repository.read_json(first_json))
    assert not list(repository.settings.report_dir.rglob("*.tmp"))


def test_existing_report_pair_is_never_overwritten(tmp_path, monkeypatch):
    report, _, _ = build_report(tmp_path / "inputs")
    repository = DailyReportRepository(Settings.from_project_root(tmp_path / "outputs"))
    monkeypatch.setattr("quantos.storage.report.uuid4", lambda: type("U", (), {"hex": "fixed"})())
    first = repository.write(report)
    contents = tuple(path.read_bytes() for path in first)
    with pytest.raises(StorageError, match="already exists"):
        repository.write(report)
    assert tuple(path.read_bytes() for path in first) == contents


def test_persisted_report_contains_no_secret_or_authorization(tmp_path):
    report, _, _ = build_report(tmp_path / "inputs")
    paths = DailyReportRepository(Settings.from_project_root(tmp_path / "outputs")).write(report)
    combined = b"\n".join(path.read_bytes() for path in paths).lower()
    assert b"api_key" not in combined and b"authorization" not in combined


def test_markdown_failure_does_not_partially_publish_report(tmp_path, monkeypatch):
    report, _, _ = build_report(tmp_path / "inputs")
    repository = DailyReportRepository(Settings.from_project_root(tmp_path / "outputs"))
    monkeypatch.setattr(
        "quantos.reporting.render_daily_report",
        lambda _report: (_ for _ in ()).throw(RuntimeError("render failed")),
    )
    with pytest.raises(StorageError, match="complete daily report pair"):
        repository.write(report)
    assert not list(repository.settings.report_dir.rglob("*.json"))
    assert not list(repository.settings.report_dir.rglob("*.md"))
    assert not list(repository.settings.report_dir.rglob("*.tmp"))
