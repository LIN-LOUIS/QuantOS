"""Atomic replay dataset and campaign artifact persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from quantos.config import DEFAULT_SETTINGS, Settings
from quantos.serialization import canonical_json_bytes

from .contracts import (
    HistoricalDatasetManifest, ReplayCampaignManifest, ReplayFailureRecord,
)


class ReplayStorageError(RuntimeError):
    pass


class HistoricalDatasetRepository:
    def __init__(self, settings: Settings = DEFAULT_SETTINGS) -> None:
        self.root = settings.data_root / "derived" / "historical_datasets"

    def save(self, value: HistoricalDatasetManifest) -> Path:
        target = self.root / f"dataset_id={value.dataset_id}.json"
        payload = canonical_json_bytes(value.to_dict()) + b"\n"
        if target.is_file():
            if target.read_bytes() != payload:
                raise ReplayStorageError("historical dataset identity collision")
            return target
        _write_new(target, payload)
        return target

    def load(self, dataset_id: str) -> HistoricalDatasetManifest:
        if not isinstance(dataset_id, str) or len(dataset_id) != 64:
            raise ReplayStorageError("invalid historical dataset id")
        path = self.root / f"dataset_id={dataset_id}.json"
        try:
            value = HistoricalDatasetManifest.from_dict(json.loads(path.read_text()))
            if value.dataset_id != dataset_id:
                raise ValueError("dataset path mismatch")
            return value
        except Exception:
            raise ReplayStorageError("historical dataset is missing or corrupt") from None

    def load_visible(self, dataset_id: str) -> HistoricalDatasetManifest:
        value = self.load(dataset_id)
        if value.status != "PASS":
            raise ReplayStorageError("historical dataset is not visible")
        return value

    def list_visible(self) -> tuple[HistoricalDatasetManifest, ...]:
        if not self.root.is_dir():
            return ()
        values = tuple(self.load(path.name.removeprefix("dataset_id=").removesuffix(".json"))
                       for path in sorted(self.root.glob("dataset_id=*.json")))
        return tuple(item for item in values if item.status == "PASS")


class ReplayCampaignRepository:
    def __init__(self, settings: Settings = DEFAULT_SETTINGS) -> None:
        self.root = settings.data_root / "derived" / "replay_campaigns"

    def campaign_dir(self, campaign_id: str) -> Path:
        if not isinstance(campaign_id, str) or len(campaign_id) != 32:
            raise ReplayStorageError("invalid replay campaign id")
        return self.root / f"campaign_id={campaign_id}"

    def summary_path(self, campaign_id: str) -> Path:
        return self.campaign_dir(campaign_id) / "summary.json"

    def save(self, value: ReplayCampaignManifest, *,
             failures: tuple[ReplayFailureRecord, ...]) -> Path:
        directory = self.campaign_dir(value.campaign_id)
        target = directory / "manifest.json"
        if target.exists():
            raise ReplayStorageError("replay campaign already exists")
        temporary = directory.parent / f".{directory.name}.tmp"
        try:
            temporary.mkdir(parents=True, exist_ok=False)
            (temporary / "manifest.json").write_bytes(
                canonical_json_bytes(value.to_dict()) + b"\n"
            )
            (temporary / "summary.json").write_bytes(
                canonical_json_bytes(value.summary.to_dict()) + b"\n"
            )
            (temporary / "failures.json").write_bytes(canonical_json_bytes({
                "campaign_id": value.campaign_id,
                "failures": [item.to_dict() for item in failures],
            }) + b"\n")
            directory.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, directory)
            return target
        except Exception as error:
            if temporary.is_dir():
                for path in temporary.iterdir():
                    path.unlink(missing_ok=True)
                temporary.rmdir()
            if isinstance(error, ReplayStorageError):
                raise
            raise ReplayStorageError("failed to persist replay campaign") from None

    def load(self, campaign_id: str) -> ReplayCampaignManifest:
        try:
            value = ReplayCampaignManifest.from_dict(json.loads(
                (self.campaign_dir(campaign_id) / "manifest.json").read_text()
            ))
            if value.campaign_id != campaign_id:
                raise ValueError("campaign path mismatch")
            return value
        except Exception:
            raise ReplayStorageError("replay campaign is missing or corrupt") from None

    def load_failures(self, campaign_id: str) -> tuple[ReplayFailureRecord, ...]:
        try:
            value = json.loads((self.campaign_dir(campaign_id) / "failures.json").read_text())
            if value["campaign_id"] != campaign_id:
                raise ValueError("failure dataset campaign mismatch")
            return tuple(ReplayFailureRecord.from_dict(item) for item in value["failures"])
        except Exception:
            raise ReplayStorageError("replay failure dataset is missing or corrupt") from None


def _write_new(path: Path, payload: bytes) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}-", suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError:
        raise ReplayStorageError("replay artifact identity collision") from None
    except Exception:
        raise ReplayStorageError("failed to persist replay artifact") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
