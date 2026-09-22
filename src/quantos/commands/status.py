"""Read-only local workspace status inspection."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from typing import Iterable

from quantos import __version__
from quantos.config import MARKET_TIMEZONE, Settings
from quantos.data import ProviderRegistry
from quantos.storage import (
    BootstrapManifestRepository, SecurityMasterRepository, StorageError,
)
from quantos.replay.storage import HistoricalDatasetRepository, ReplayStorageError


def inspect_status(project_root: Path | str) -> dict[str, object]:
    """Inspect only local paths; never initialize storage or contact providers."""
    settings = Settings.from_project_root(project_root)
    market_files = _files(settings.normalized_market_dir, "*.parquet")
    evidence_files = tuple(sorted({
        *_files(settings.moneyflow_dir, "*.parquet"),
        *_files(settings.news_dir, "*.parquet"),
        *_files(settings.announcement_dir, "*.parquet"),
        *_files(settings.web_search_dir, "*.parquet"),
    }))
    knowledge_documents = _files(settings.data_root / "knowledge" / "documents", "*.json")
    knowledge_indexes = _files(
        settings.data_root / "knowledge" / "indexes" / "lexical", "*.json"
    )
    daily_files = _files(settings.report_dir, "*.json")
    time_slice_files = _files(
        settings.data_root / "derived" / "time_slice_reports", "*.json"
    )
    scheduler_path = (
        settings.data_root / "runtime" / "scheduler" / "scheduler_runtime.sqlite3"
    )
    security_repository = SecurityMasterRepository(settings)
    security_invalid = False
    try:
        snapshots = security_repository.list_snapshots() if security_repository.is_initialized() else ()
    except StorageError:
        snapshots = ()
        security_invalid = True
    manifests = BootstrapManifestRepository(settings)
    market_manifest_invalid = False
    try:
        recent_manifest = manifests.latest_success("market_recent")
    except StorageError:
        recent_manifest = None
        market_manifest_invalid = True
    providers = tuple(item.to_dict() for item in ProviderRegistry(settings=settings).inspect())
    try:
        replay_datasets = HistoricalDatasetRepository(settings).list_visible()
    except ReplayStorageError:
        replay_datasets = ()
    replay_availability, replay_reason = _historical_replay_availability(
        replay_datasets, snapshots,
    )
    if knowledge_documents and knowledge_indexes:
        knowledge_readiness = "READY"
    elif knowledge_documents or knowledge_indexes:
        knowledge_readiness = "PARTIAL"
    else:
        knowledge_readiness = "EMPTY"
    return {
        "command": "status",
        "status": "OK",
        "offline": True,
        "read_only": True,
        "provider_requests": 0,
        "package_version": __version__,
        "project_root": str(settings.project_root),
        "market": _artifact_status(market_files),
        "evidence": _artifact_status(evidence_files),
        "knowledge": {
            "readiness": knowledge_readiness,
            "document_count": len(knowledge_documents),
            "lexical_index_count": len(knowledge_indexes),
        },
        "daily_report": _product_status(daily_files, "trade_date="),
        "time_slice": _product_status(time_slice_files, "target_trade_date="),
        "scheduler_runtime": {
            "availability": "AVAILABLE" if scheduler_path.is_file() else "NONE",
            "path": str(scheduler_path),
        },
        "providers": providers,
        "data_availability": {
            "security_master": {
                "availability": ("CORRUPT" if security_invalid else
                                 "READY" if snapshots else "UNAVAILABLE"),
                "snapshot_count": len(snapshots),
                "coverage_start": snapshots[0].observed_at.isoformat() if snapshots else None,
                "coverage_end": snapshots[-1].observed_at.isoformat() if snapshots else None,
            },
            "recent_market": {
                "availability": ("CORRUPT" if market_manifest_invalid else
                                 "READY" if recent_manifest else "UNAVAILABLE"),
                "bootstrap_id": recent_manifest.bootstrap_id if recent_manifest else None,
            },
            "historical_market": {"availability": "UNAVAILABLE"},
            "historical_replay": {
                "availability": replay_availability,
                "dataset_count": len(replay_datasets),
                "reason_code": replay_reason,
            },
            "historical_identity": {
                "availability": "PARTIAL" if snapshots else "UNAVAILABLE",
                "limitation": "coverage starts at the first persisted provider observation",
            },
        },
    }


def render_status(value: dict[str, object]) -> str:
    market = value["market"]
    evidence = value["evidence"]
    knowledge = value["knowledge"]
    daily = value["daily_report"]
    time_slice = value["time_slice"]
    scheduler = value["scheduler_runtime"]
    data = value["data_availability"]
    return "\n".join((
        "QuantOS Local Status",
        f"Package Version     {value['package_version']}",
        f"Project Root        {value['project_root']}",
        f"Market Data         {market['readiness']} ({market['file_count']} files)",
        f"Evidence            {evidence['readiness']} ({evidence['file_count']} files)",
        f"Knowledge           {knowledge['readiness']}",
        f"Daily Report        {daily['availability']}",
        f"TimeSlice           {time_slice['availability']}",
        f"Scheduler Runtime   {scheduler['availability']}",
        f"Security Master    {data['security_master']['availability']}",
        f"Recent Market      {data['recent_market']['availability']}",
        f"Historical Market  {data['historical_market']['availability']}",
        f"Historical Replay  {data['historical_replay']['availability']}",
        "Offline              YES",
        "Read only            YES",
    ))


def _files(root: Path, pattern: str) -> tuple[Path, ...]:
    if not root.is_dir():
        return ()
    return tuple(path for path in sorted(root.rglob(pattern)) if path.is_file())


def _artifact_status(files: Iterable[Path]) -> dict[str, object]:
    materialized = tuple(files)
    return {
        "readiness": "READY" if materialized else "EMPTY",
        "file_count": len(materialized),
    }


def _product_status(files: tuple[Path, ...], partition_prefix: str) -> dict[str, object]:
    dates = sorted({
        part.removeprefix(partition_prefix)
        for path in files
        for part in path.parts
        if part.startswith(partition_prefix)
    })
    return {
        "availability": "AVAILABLE" if files else "NONE",
        "file_count": len(files),
        "latest_trade_date": dates[-1] if dates else None,
    }


def _historical_replay_availability(datasets, snapshots) -> tuple[str, str | None]:
    if not datasets:
        return "UNAVAILABLE", "HISTORICAL_DATA_UNAVAILABLE"
    if not snapshots:
        return "UNAVAILABLE", "IDENTITY_HISTORY_UNAVAILABLE"
    for dataset in datasets:
        for day in dataset.trading_dates:
            cutoff = datetime.combine(day, time(18, 0), tzinfo=MARKET_TIMEZONE)
            visible = tuple(item for item in snapshots if item.observed_at <= cutoff)
            preferred = tuple(item for item in visible if item.provider_id == "tushare")
            if not visible:
                continue
            snapshot = max(preferred or visible, key=lambda item: (
                item.observed_at, item.snapshot_id,
            ))
            symbols = {
                item.security.symbol
                for item in snapshot.records
                if item.security.available_at <= cutoff
                and item.security.effective_from <= day
                and (item.security.effective_to is None
                     or item.security.effective_to >= day)
                and item.security.is_active
            }
            if symbols.intersection(dataset.symbols):
                return "READY", None
    return "UNAVAILABLE", "IDENTITY_HISTORY_UNAVAILABLE"
