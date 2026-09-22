"""Persistent bootstrap manifests and provider health without credentials."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile

from quantos.config import DEFAULT_SETTINGS, Settings
from quantos.schemas import (
    BootstrapManifest, ProviderAvailability, ProviderFailureCode,
    ProviderHealthRecord,
)
from quantos.serialization import canonical_json_bytes

from .market import StorageError


_PROVIDER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class BootstrapManifestRepository:
    def __init__(self, settings: Settings = DEFAULT_SETTINGS) -> None:
        self.root = settings.data_root / "derived" / "bootstrap_runs"

    def write(self, manifest: BootstrapManifest) -> Path:
        if not isinstance(manifest, BootstrapManifest):
            raise TypeError("BootstrapManifest required")
        directory = self.root / f"started_date={manifest.started_at.date().isoformat()}"
        target = directory / f"bootstrap_id={manifest.bootstrap_id}.json"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            _write_new(target, canonical_json_bytes(manifest.to_dict()) + b"\n")
            return target
        except Exception:
            raise StorageError("failed to persist bootstrap manifest") from None

    def read(self, path: Path) -> BootstrapManifest:
        try:
            value = BootstrapManifest.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
            expected = self.root / f"started_date={value.started_at.date().isoformat()}" / f"bootstrap_id={value.bootstrap_id}.json"
            if Path(path).resolve() != expected.resolve():
                raise ValueError("bootstrap manifest path mismatch")
            return value
        except Exception:
            raise StorageError("failed to read bootstrap manifest") from None

    def list_manifests(self) -> tuple[BootstrapManifest, ...]:
        if not self.root.exists():
            return ()
        values = tuple(self.read(path) for path in sorted(self.root.glob(
            "started_date=*/bootstrap_id=*.json"
        )))
        return tuple(sorted(values, key=lambda item: (item.started_at, item.bootstrap_id)))

    def latest_success(self, dataset: str) -> BootstrapManifest | None:
        matches = tuple(item for item in self.list_manifests()
                        if item.status == "PASS" and dataset in item.datasets)
        return matches[-1] if matches else None


class ProviderHealthRepository:
    def __init__(self, settings: Settings = DEFAULT_SETTINGS) -> None:
        self.root = settings.data_root / "derived" / "provider_health"

    def maybe_load(self, provider_id: str) -> ProviderHealthRecord | None:
        path = self._path(provider_id)
        return self.load(provider_id) if path.is_file() else None

    def load(self, provider_id: str) -> ProviderHealthRecord:
        path = self._path(provider_id)
        try:
            value = ProviderHealthRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if value.provider_id != provider_id:
                raise ValueError("provider health identity mismatch")
            return value
        except Exception:
            raise StorageError("failed to read provider health") from None

    def record_success(self, provider_id: str, *, observed_at: datetime) -> ProviderHealthRecord:
        previous = self.maybe_load(provider_id)
        value = ProviderHealthRecord(
            provider_id, ProviderAvailability.READY, observed_at,
            last_success_at=observed_at,
            last_failure_at=previous.last_failure_at if previous else None,
            last_failure_code=previous.last_failure_code if previous else None,
        )
        self._write(value)
        return value

    def record_failure(
        self, provider_id: str, *, observed_at: datetime,
        code: ProviderFailureCode,
    ) -> ProviderHealthRecord:
        previous = self.maybe_load(provider_id)
        availability = (
            ProviderAvailability.UNCONFIGURED
            if code is ProviderFailureCode.UNCONFIGURED
            else ProviderAvailability.DEGRADED
            if code is ProviderFailureCode.RATE_LIMITED and previous and previous.last_success_at
            else ProviderAvailability.UNAVAILABLE
        )
        value = ProviderHealthRecord(
            provider_id, availability, observed_at,
            last_success_at=previous.last_success_at if previous else None,
            last_failure_at=observed_at, last_failure_code=code,
        )
        self._write(value)
        return value

    def _path(self, provider_id: str) -> Path:
        if not isinstance(provider_id, str) or not _PROVIDER.fullmatch(provider_id):
            raise ValueError("invalid provider_id")
        return self.root / f"provider={provider_id}.json"

    def _write(self, value: ProviderHealthRecord) -> None:
        target = self._path(value.provider_id)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _replace(target, canonical_json_bytes(value.to_dict()) + b"\n")
        except Exception:
            raise StorageError("failed to persist provider health") from None


def _write_new(target: Path, payload: bytes) -> None:
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _replace(target: Path, payload: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=f".{target.name}-", suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
