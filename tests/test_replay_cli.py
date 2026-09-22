"""Thin replay CLI acceptance."""

from datetime import date, datetime
import json

import pytest

from quantos import cli
from quantos.config import MARKET_TIMEZONE, Settings
from quantos.schemas import SecurityMaster
from quantos.storage import SecurityMasterRepository


def test_replay_help_exposes_import_run_and_inspection(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["replay", "--help"])
    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert "{derive-identity,import-market,run,show,failures}" in output


def test_replay_import_without_provider_configuration_fails_safely(
    monkeypatch, tmp_path, capsys,
):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)

    code = cli.main([
        "replay", "import-market", "--symbol", "600519.SH",
        "--start", "2026-08-03", "--end", "2026-08-03",
        "--as-of-time", "2026-08-05T20:00:00+08:00",
        "--project-root", str(tmp_path), "--json",
    ])

    result = json.loads(capsys.readouterr().err)
    assert code == 2
    assert result == {"error_code": "UNCONFIGURED", "status": "FAIL"}
    assert not (tmp_path / "data/derived/historical_datasets").exists()


def test_replay_cli_derives_labeled_identity_without_historical_name_claim(
    tmp_path, capsys,
):
    observed_at = datetime(2026, 9, 20, 12, 30, tzinfo=MARKET_TIMEZONE)
    settings = Settings.from_project_root(tmp_path)
    snapshot = SecurityMasterRepository(settings).write_snapshot((SecurityMaster(
        "600519.SH", "贵州茅台", "SHSE", date(2001, 8, 27), observed_at,
        source="tushare",
    ),), provider_id="tushare", observed_at=observed_at).snapshot

    code = cli.main([
        "replay", "derive-identity", "--snapshot-id", snapshot.snapshot_id,
        "--project-root", str(tmp_path), "--json",
    ])

    value = json.loads(capsys.readouterr().out)
    assert code == 0
    assert value["derivation_type"] == "RETROSPECTIVE_DERIVATION"
    assert value["source_security_snapshot_id"] == snapshot.snapshot_id
    assert "贵州茅台" not in json.dumps(value, ensure_ascii=False)


def test_replay_cli_missing_identity_snapshot_fails_without_traceback(tmp_path, capsys):
    code = cli.main([
        "replay", "derive-identity", "--snapshot-id", "a" * 64,
        "--project-root", str(tmp_path), "--json",
    ])

    error = json.loads(capsys.readouterr().err)
    assert code == 2
    assert error == {
        "status": "FAIL", "error_code": "Security Master snapshot is unavailable",
    }
