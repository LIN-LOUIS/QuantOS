"""Read-only local workspace status inspection."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from quantos import __version__
from quantos.config import Settings


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
    }


def render_status(value: dict[str, object]) -> str:
    market = value["market"]
    evidence = value["evidence"]
    knowledge = value["knowledge"]
    daily = value["daily_report"]
    time_slice = value["time_slice"]
    scheduler = value["scheduler_runtime"]
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
