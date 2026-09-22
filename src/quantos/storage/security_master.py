"""Append-only persistent Security Master snapshots with strict PIT reads."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from quantos.config import DEFAULT_SETTINGS, Settings
from quantos.schemas import (
    SecurityIdentityRecord, SecurityMaster, SecurityMasterSnapshot,
    canonical_security_id,
)
from quantos.schemas._validation import require_aware
from quantos.serialization import canonical_identity_json_bytes, canonical_json_bytes

from .market import StorageError


class SecurityMasterStorageError(StorageError):
    pass


class SecurityMasterUnavailableError(SecurityMasterStorageError):
    pass


@dataclass(frozen=True, slots=True)
class SecuritySnapshotWriteResult:
    snapshot: SecurityMasterSnapshot
    created: bool
    path: Path


class SecurityMasterRepository:
    """Immutable full-provider observations; reads select one visible snapshot."""

    def __init__(self, settings: Settings = DEFAULT_SETTINGS) -> None:
        self.settings = settings
        self.root = settings.data_root / "reference" / "security_master"

    def is_initialized(self) -> bool:
        return any(self.root.glob("provider=*/snapshot_id=*.json")) if self.root.exists() else False

    def write_snapshot(
        self, records: Sequence[SecurityMaster], *, provider_id: str,
        observed_at: datetime,
    ) -> SecuritySnapshotWriteResult:
        require_aware(observed_at, "observed_at")
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("provider_id is required")
        materialized = tuple(records)
        if not materialized or any(not isinstance(item, SecurityMaster) for item in materialized):
            raise ValueError("Security Master snapshot requires records")
        if any(item.source != provider_id for item in materialized):
            raise ValueError("Security Master source does not match provider")
        if any(item.available_at > observed_at for item in materialized):
            raise ValueError("Security Master record was learned after snapshot observation")
        ordered = tuple(sorted(materialized, key=lambda item: item.symbol))
        if len({item.symbol for item in ordered}) != len(ordered):
            raise ValueError("Security Master symbols must be unique within a snapshot")
        identities = tuple(SecurityIdentityRecord(canonical_security_id(item), item)
                           for item in ordered)
        snapshot_id = _snapshot_id(provider_id, identities)
        snapshot = SecurityMasterSnapshot(snapshot_id, provider_id, observed_at, identities)
        directory = self.root / f"provider={provider_id}"
        target = directory / f"snapshot_id={snapshot_id}.json"
        if target.exists():
            existing = self.read(target)
            if _business_record(existing) != _business_record(snapshot):
                raise SecurityMasterStorageError("Security Master snapshot identity collision")
            return SecuritySnapshotWriteResult(existing, False, target)
        temporary = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            payload = canonical_json_bytes(_snapshot_record(snapshot)) + b"\n"
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=directory, prefix=".security-master-", suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
                created = True
            except FileExistsError:
                existing = self.read(target)
                if _business_record(existing) != _business_record(snapshot):
                    raise SecurityMasterStorageError("Security Master snapshot identity collision")
                snapshot, created = existing, False
            return SecuritySnapshotWriteResult(snapshot, created, target)
        except SecurityMasterStorageError:
            raise
        except Exception:
            raise SecurityMasterStorageError("failed to persist Security Master snapshot") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read(self, path: Path) -> SecurityMasterSnapshot:
        try:
            snapshot = _snapshot_from_record(json.loads(Path(path).read_text(encoding="utf-8")))
            expected = self.root / f"provider={snapshot.provider_id}" / f"snapshot_id={snapshot.snapshot_id}.json"
            if Path(path).resolve() != expected.resolve():
                raise ValueError("snapshot path does not match identity")
            return snapshot
        except Exception:
            raise SecurityMasterStorageError("Security Master snapshot is corrupt") from None

    def list_snapshots(self) -> tuple[SecurityMasterSnapshot, ...]:
        if not self.root.exists():
            return ()
        values = tuple(self.read(path) for path in sorted(
            self.root.glob("provider=*/snapshot_id=*.json")
        ))
        return tuple(sorted(values, key=lambda item: (
            item.observed_at, item.provider_id, item.snapshot_id,
        )))

    def snapshot_as_of(self, as_of_time: datetime) -> SecurityMasterSnapshot:
        require_aware(as_of_time, "as_of_time")
        snapshots = self.list_snapshots()
        if not snapshots:
            raise SecurityMasterUnavailableError("Security Master is not initialized")
        visible = tuple(item for item in snapshots if item.observed_at <= as_of_time)
        if not visible:
            raise SecurityMasterUnavailableError("no snapshot is visible at the requested time")
        preferred = tuple(item for item in visible if item.provider_id == "tushare")
        return max(preferred or visible, key=lambda item: (
            item.observed_at, item.snapshot_id,
        ))

    def list_as_of(self, as_of_time: datetime) -> tuple[SecurityMaster, ...]:
        snapshot = self.snapshot_as_of(as_of_time)
        market_date = as_of_time.date()
        records = tuple(
            item for item in snapshot.records
            if item.security.available_at <= as_of_time
            and item.security.effective_from <= market_date
            and (item.security.effective_to is None or item.security.effective_to >= market_date)
            and item.security.is_active
        )
        names: dict[str, set[str]] = {}
        for historical in self.list_snapshots():
            if historical.observed_at > as_of_time:
                continue
            for item in historical.records:
                names.setdefault(item.canonical_security_id, set()).add(
                    item.security.company_name
                )
        output = []
        for item in records:
            aliases = tuple(sorted({
                *item.security.aliases,
                *(names.get(item.canonical_security_id, set()) - {item.security.company_name}),
            }))
            output.append(replace(item.security, aliases=aliases))
        return tuple(sorted(output, key=lambda item: item.symbol))

    def resolve(self, query: str, *, as_of_time: datetime) -> tuple[SecurityMaster, ...]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("identity query is required")
        value = query.strip()
        return tuple(item for item in self.list_as_of(as_of_time)
                     if value in (item.symbol, item.company_name, *item.aliases))

    def snapshot_id_as_of(self, as_of_time: datetime) -> str:
        return self.snapshot_as_of(as_of_time).snapshot_id

    def snapshot_by_id(self, snapshot_id: str) -> SecurityMasterSnapshot:
        matches = tuple(item for item in self.list_snapshots()
                        if item.snapshot_id == snapshot_id)
        if len(matches) != 1:
            raise SecurityMasterUnavailableError("Security Master snapshot is unavailable")
        return matches[0]

    def list_from_snapshot(
        self, snapshot_id: str, *, as_of_time: datetime,
    ) -> tuple[SecurityMaster, ...]:
        require_aware(as_of_time, "as_of_time")
        snapshot = self.snapshot_by_id(snapshot_id)
        if snapshot.observed_at > as_of_time:
            raise SecurityMasterUnavailableError("future Security Master snapshot is not visible")
        market_date = as_of_time.date()
        return tuple(sorted((
            item.security for item in snapshot.records
            if item.security.available_at <= as_of_time
            and item.security.effective_from <= market_date
            and (item.security.effective_to is None
                 or item.security.effective_to >= market_date)
            and item.security.is_active
        ), key=lambda item: item.symbol))


def _snapshot_id(provider_id: str, records: tuple[SecurityIdentityRecord, ...]) -> str:
    import hashlib
    return hashlib.sha256(canonical_identity_json_bytes({
        "provider_id": provider_id,
        "records": [_identity_record(item) for item in records],
    })).hexdigest()


def _business_record(snapshot: SecurityMasterSnapshot) -> dict[str, Any]:
    return {
        "provider_id": snapshot.provider_id,
        "records": [_identity_record(item) for item in snapshot.records],
    }


def _identity_record(item: SecurityIdentityRecord) -> dict[str, Any]:
    value = _security_record(item.security)
    value.pop("available_at")
    return {"canonical_security_id": item.canonical_security_id, "security": value}


def _snapshot_record(snapshot: SecurityMasterSnapshot) -> dict[str, Any]:
    return {
        "schema_version": snapshot.schema_version,
        "snapshot_id": snapshot.snapshot_id,
        "provider_id": snapshot.provider_id,
        "observed_at": snapshot.observed_at.isoformat(),
        "records": [{
            "canonical_security_id": item.canonical_security_id,
            "security": _security_record(item.security),
        } for item in snapshot.records],
    }


def _snapshot_from_record(value: Mapping[str, Any]) -> SecurityMasterSnapshot:
    expected = {"schema_version", "snapshot_id", "provider_id", "observed_at", "records"}
    if not isinstance(value, Mapping) or set(value) != expected or type(value["records"]) is not list:
        raise ValueError("invalid Security Master snapshot record")
    records = tuple(SecurityIdentityRecord(
        item["canonical_security_id"], _security_from_record(item["security"]),
    ) for item in value["records"])
    snapshot = SecurityMasterSnapshot(
        value["snapshot_id"], value["provider_id"],
        datetime.fromisoformat(value["observed_at"]), records, value["schema_version"],
    )
    if snapshot.snapshot_id != _snapshot_id(snapshot.provider_id, snapshot.records):
        raise ValueError("Security Master snapshot identity mismatch")
    return snapshot


def _security_record(value: SecurityMaster) -> dict[str, Any]:
    return {
        "symbol": value.symbol, "company_name": value.company_name,
        "exchange": value.exchange, "effective_from": value.effective_from.isoformat(),
        "available_at": value.available_at.isoformat(), "aliases": list(value.aliases),
        "industry_l1": value.industry_l1, "industry_l2": value.industry_l2,
        "effective_to": value.effective_to.isoformat() if value.effective_to else None,
        "is_active": value.is_active, "source": value.source,
        "asset_type": value.asset_type, "status": value.status,
        "industry": value.industry,
        "industry_classification": value.industry_classification,
        "source_updated_at": (
            value.source_updated_at.isoformat() if value.source_updated_at else None
        ), "schema_version": value.schema_version,
    }


def _security_from_record(value: Mapping[str, Any]) -> SecurityMaster:
    expected = {
        "symbol", "company_name", "exchange", "effective_from", "available_at",
        "aliases", "industry_l1", "industry_l2", "effective_to", "is_active",
        "source", "asset_type", "status", "industry", "industry_classification",
        "source_updated_at", "schema_version",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("invalid Security Master record fields")
    return SecurityMaster(
        symbol=value["symbol"], company_name=value["company_name"],
        exchange=value["exchange"], effective_from=date.fromisoformat(value["effective_from"]),
        available_at=datetime.fromisoformat(value["available_at"]),
        aliases=tuple(value["aliases"]), industry_l1=value["industry_l1"],
        industry_l2=value["industry_l2"],
        effective_to=date.fromisoformat(value["effective_to"]) if value["effective_to"] else None,
        is_active=value["is_active"], source=value["source"],
        asset_type=value["asset_type"], status=value["status"],
        industry=value["industry"],
        industry_classification=value["industry_classification"],
        source_updated_at=(datetime.fromisoformat(value["source_updated_at"])
                           if value["source_updated_at"] else None),
        schema_version=value["schema_version"],
    )
