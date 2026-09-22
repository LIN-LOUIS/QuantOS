"""Manifest-gated replay persistence and failure artifacts."""

from datetime import date, datetime
import json

import pytest

from quantos.config import MARKET_TIMEZONE, Settings
from quantos.commands.status import inspect_status
from quantos.replay import (
    HistoricalDatasetManifest, ReplayCampaignManifest, ReplayFailureRecord,
    ReplayResult, ReplaySummary, ReplayUniverse,
)
from quantos.schemas import SecurityMaster
from quantos.replay.storage import (
    HistoricalDatasetRepository, ReplayCampaignRepository, ReplayStorageError,
)
from quantos.storage import SecurityMasterRepository


NOW = datetime(2026, 8, 5, 18, tzinfo=MARKET_TIMEZONE)


def dataset(status="PASS"):
    return HistoricalDatasetManifest.build(
        provider="tushare", market="A_SHARE", symbols=("600519.SH",),
        start_date=date(2026, 8, 3), end_date=date(2026, 8, 3),
        trading_dates=(date(2026, 8, 3),), record_refs=("daily:1",),
        imported_at=NOW, status=status,
    )


def test_only_successful_dataset_manifest_is_visible(tmp_path):
    repository = HistoricalDatasetRepository(Settings.from_project_root(tmp_path))
    visible = dataset()
    failed = dataset("FAIL")
    repository.save(visible)
    repository.save(failed)

    assert repository.load_visible(visible.dataset_id) == visible
    with pytest.raises(ReplayStorageError, match="not visible"):
        repository.load_visible(failed.dataset_id)


def test_orphan_market_file_does_not_create_dataset_visibility(tmp_path):
    settings = Settings.from_project_root(tmp_path)
    orphan = settings.normalized_market_dir / "trade_date=2026-08-03" / "orphan.parquet"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"physical but uncommitted")

    repository = HistoricalDatasetRepository(settings)

    assert repository.list_visible() == ()
    assert orphan.is_file()


def test_status_requires_pit_overlap_before_replay_is_ready(tmp_path):
    settings = Settings.from_project_root(tmp_path)
    HistoricalDatasetRepository(settings).save(dataset())
    SecurityMasterRepository(settings).write_snapshot((SecurityMaster(
        "600519.SH", "贵州茅台", "SHSE", date(2001, 8, 27), NOW,
        source="tushare",
    ),), provider_id="tushare", observed_at=NOW)

    replay = inspect_status(tmp_path)["data_availability"]["historical_replay"]

    assert replay["availability"] == "UNAVAILABLE"
    assert replay["reason_code"] == "IDENTITY_HISTORY_UNAVAILABLE"


def test_campaign_round_trip_writes_summary_and_structured_failures(tmp_path):
    settings = Settings.from_project_root(tmp_path)
    universe = ReplayUniverse(
        ("600519.SH",), "A_SHARE", date(2026, 8, 3), date(2026, 8, 3),
        (date(2026, 8, 3),),
    )
    result = ReplayResult(
        "a" * 64, NOW, "600519.SH", "FAILED", False, False, False, False,
        False, False, (), (), ("IDENTITY_HISTORY_UNAVAILABLE",), None, None,
        1.0, 0, 0,
    )
    failure = ReplayFailureRecord.from_result(result, stage="identity")
    summary = ReplaySummary.from_results((result,), deterministic_mismatch_count=0)
    manifest = ReplayCampaignManifest.build(
        created_at=NOW, universe=universe, dataset_refs=("b" * 64,),
        security_snapshot_refs=(), configuration_hash="c" * 64,
        code_version="deadbeef", dirty=True, results=(result,), summary=summary,
    )
    repository = ReplayCampaignRepository(settings)

    repository.save(manifest, failures=(failure,))

    assert repository.load(manifest.campaign_id) == manifest
    assert repository.load_failures(manifest.campaign_id) == (failure,)
    summary_payload = json.loads(repository.summary_path(manifest.campaign_id).read_text())
    assert summary_payload["failed_points"] == 1
    assert summary_payload["pit_rejection_count"] == 0
