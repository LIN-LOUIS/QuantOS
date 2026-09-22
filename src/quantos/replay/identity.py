"""Conservative authority for retrospective historical security identity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from quantos.config import DEFAULT_SETTINGS, Settings
from quantos.schemas import SecurityMaster, SecurityMasterSnapshot
from quantos.serialization import canonical_identity_json_bytes, canonical_json_bytes


AUTHORITY_POLICY_VERSION = "historical-identity-authority:v1"
_SCHEMA_VERSION = "quantos-historical-identity-artifact-v1"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_AUTHORITY_FIELDS = ("effective_from", "effective_to", "exchange", "symbol")
_UNVERIFIED_FIELDS = ("aliases", "company_name", "industry")


class HistoricalIdentityUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HistoricalIdentityRecord:
    canonical_security_id: str
    symbol: str
    exchange: str
    effective_from: date
    effective_to: date | None
    source_provider: str
    source_security_snapshot_id: str
    source_record_ref: str
    source_observed_at: datetime
    authority_policy_version: str = AUTHORITY_POLICY_VERSION
    authoritative_fields: tuple[str, ...] = _AUTHORITY_FIELDS
    temporal_unverified_fields: tuple[str, ...] = _UNVERIFIED_FIELDS

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.canonical_security_id):
            raise ValueError("invalid canonical security id")
        if not _SYMBOL.fullmatch(self.symbol) or not self.exchange:
            raise ValueError("invalid historical identity")
        if not _DIGEST.fullmatch(self.source_security_snapshot_id):
            raise ValueError("invalid source snapshot id")
        if not _DIGEST.fullmatch(self.source_record_ref):
            raise ValueError("invalid source record ref")
        if self.source_observed_at.utcoffset() is None:
            raise ValueError("source_observed_at must be timezone-aware")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("invalid identity effective interval")
        if self.authority_policy_version != AUTHORITY_POLICY_VERSION:
            raise ValueError("unsupported identity authority policy")
        if self.authoritative_fields != _AUTHORITY_FIELDS:
            raise ValueError("historical authority fields are closed")
        if self.temporal_unverified_fields != _UNVERIFIED_FIELDS:
            raise ValueError("temporal-unverified identity fields are closed")

    def visible_on(self, value: date) -> bool:
        return self.effective_from <= value and (
            self.effective_to is None or self.effective_to >= value
        )

    def to_security_master(self) -> SecurityMaster:
        return SecurityMaster(
            symbol=self.symbol,
            company_name=self.symbol,
            exchange=self.exchange,
            effective_from=self.effective_from,
            effective_to=self.effective_to,
            available_at=self.source_observed_at,
            aliases=(),
            is_active=True,
            source=f"retrospective:{self.source_provider}",
            asset_type="stock",
            status="historically_listed",
            source_updated_at=self.source_observed_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_security_id": self.canonical_security_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "source_provider": self.source_provider,
            "source_security_snapshot_id": self.source_security_snapshot_id,
            "source_record_ref": self.source_record_ref,
            "source_observed_at": self.source_observed_at.isoformat(),
            "authority_policy_version": self.authority_policy_version,
            "authoritative_fields": list(self.authoritative_fields),
            "temporal_unverified_fields": list(self.temporal_unverified_fields),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> HistoricalIdentityRecord:
        return cls(
            value["canonical_security_id"], value["symbol"], value["exchange"],
            date.fromisoformat(value["effective_from"]),
            date.fromisoformat(value["effective_to"]) if value["effective_to"] else None,
            value["source_provider"], value["source_security_snapshot_id"],
            value["source_record_ref"], datetime.fromisoformat(value["source_observed_at"]),
            value["authority_policy_version"], tuple(value["authoritative_fields"]),
            tuple(value["temporal_unverified_fields"]),
        )


@dataclass(frozen=True, slots=True)
class HistoricalIdentityArtifact:
    schema_version: str
    artifact_id: str
    source_security_snapshot_id: str
    source_provider: str
    generated_at: datetime
    authority_policy_version: str
    derivation_type: str
    records: tuple[HistoricalIdentityRecord, ...]

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION or not _DIGEST.fullmatch(self.artifact_id):
            raise ValueError("invalid historical identity artifact")
        if not _DIGEST.fullmatch(self.source_security_snapshot_id):
            raise ValueError("invalid historical identity source")
        if self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.authority_policy_version != AUTHORITY_POLICY_VERSION:
            raise ValueError("unsupported historical identity policy")
        if self.derivation_type != "RETROSPECTIVE_DERIVATION" or not self.records:
            raise ValueError("invalid historical identity derivation")
        expected = tuple(sorted(self.records, key=lambda item: (
            item.symbol, item.effective_from, item.effective_to or date.max,
            item.canonical_security_id,
        )))
        if self.records != expected:
            raise ValueError("historical identity records must be sorted")
        if any(item.source_security_snapshot_id != self.source_security_snapshot_id
               or item.source_provider != self.source_provider for item in self.records):
            raise ValueError("historical identity provenance mismatch")
        if self.artifact_id != self.semantic_id:
            raise ValueError("historical identity artifact hash mismatch")

    @property
    def semantic_id(self) -> str:
        return hashlib.sha256(canonical_identity_json_bytes(
            self.semantic_payload()
        )).hexdigest()

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_security_snapshot_id": self.source_security_snapshot_id,
            "source_provider": self.source_provider,
            "authority_policy_version": self.authority_policy_version,
            "derivation_type": self.derivation_type,
            "records": [item.to_dict() for item in self.records],
        }

    @classmethod
    def build(cls, *, source_security_snapshot_id: str, source_provider: str,
              generated_at: datetime, authority_policy_version: str,
              records: tuple[HistoricalIdentityRecord, ...]) -> HistoricalIdentityArtifact:
        ordered = tuple(sorted(records, key=lambda item: (
            item.symbol, item.effective_from, item.effective_to or date.max,
            item.canonical_security_id,
        )))
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "source_security_snapshot_id": source_security_snapshot_id,
            "source_provider": source_provider,
            "authority_policy_version": authority_policy_version,
            "derivation_type": "RETROSPECTIVE_DERIVATION",
            "records": [item.to_dict() for item in ordered],
        }
        artifact_id = hashlib.sha256(canonical_identity_json_bytes(payload)).hexdigest()
        return cls(
            _SCHEMA_VERSION, artifact_id, source_security_snapshot_id,
            source_provider, generated_at, authority_policy_version,
            "RETROSPECTIVE_DERIVATION", ordered,
        )

    def resolve_symbol(self, symbol: str, *, as_of_date: date) -> HistoricalIdentityRecord:
        matches = tuple(item for item in self.records
                        if item.symbol == symbol and item.visible_on(as_of_date))
        if not matches:
            raise HistoricalIdentityUnavailableError(
                "authoritative historical identity is unavailable"
            )
        if len(matches) != 1:
            raise HistoricalIdentityUnavailableError(
                "authoritative historical identity is ambiguous"
            )
        return matches[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_payload(), "artifact_id": self.artifact_id,
            "generated_at": self.generated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> HistoricalIdentityArtifact:
        return cls(
            value["schema_version"], value["artifact_id"],
            value["source_security_snapshot_id"], value["source_provider"],
            datetime.fromisoformat(value["generated_at"]),
            value["authority_policy_version"], value["derivation_type"],
            tuple(HistoricalIdentityRecord.from_dict(item) for item in value["records"]),
        )


class HistoricalIdentityAuthority:
    _FIELD_AUDITS = {
        "tushare": {
            "symbol": "HISTORICALLY_EFFECTIVE",
            "exchange": "HISTORICALLY_EFFECTIVE",
            "list_date": "HISTORICALLY_EFFECTIVE",
            "delist_date": "HISTORICALLY_EFFECTIVE",
            "name": "CURRENT_SNAPSHOT_ONLY",
            "industry": "CURRENT_SNAPSHOT_ONLY",
            "area": "TEMPORAL_UNVERIFIED",
            "market": "TEMPORAL_UNVERIFIED",
        },
        "baostock": {
            "code": "HISTORICALLY_EFFECTIVE",
            "exchange": "HISTORICALLY_EFFECTIVE",
            "ipoDate": "HISTORICALLY_EFFECTIVE",
            "outDate": "HISTORICALLY_EFFECTIVE",
            "code_name": "CURRENT_SNAPSHOT_ONLY",
            "industry": "CURRENT_SNAPSHOT_ONLY",
            "industryClassification": "TEMPORAL_UNVERIFIED",
        },
    }

    def field_audit(self, provider: str) -> dict[str, str]:
        try:
            return dict(self._FIELD_AUDITS[provider])
        except KeyError:
            raise ValueError("provider lacks historical identity authority") from None

    def derive(self, snapshot: SecurityMasterSnapshot, *,
               generated_at: datetime) -> HistoricalIdentityArtifact:
        self.field_audit(snapshot.provider_id)
        records = tuple(HistoricalIdentityRecord(
            canonical_security_id=item.canonical_security_id,
            symbol=item.security.symbol,
            exchange=item.security.exchange,
            effective_from=item.security.effective_from,
            effective_to=item.security.effective_to,
            source_provider=snapshot.provider_id,
            source_security_snapshot_id=snapshot.snapshot_id,
            source_record_ref=item.canonical_security_id,
            source_observed_at=snapshot.observed_at,
        ) for item in snapshot.records if item.security.asset_type == "stock")
        return HistoricalIdentityArtifact.build(
            source_security_snapshot_id=snapshot.snapshot_id,
            source_provider=snapshot.provider_id, generated_at=generated_at,
            authority_policy_version=AUTHORITY_POLICY_VERSION, records=records,
        )


class HistoricalIdentityArtifactRepository:
    def __init__(self, settings: Settings = DEFAULT_SETTINGS) -> None:
        self.root = settings.data_root / "derived" / "historical_identity"

    def save(self, value: HistoricalIdentityArtifact) -> Path:
        target = self.root / f"artifact_id={value.artifact_id}.json"
        payload = canonical_json_bytes(value.to_dict()) + b"\n"
        if target.is_file():
            if target.read_bytes() != payload:
                raise HistoricalIdentityUnavailableError("identity artifact collision")
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=target.parent, prefix=f".{target.name}-",
                suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
        except FileExistsError:
            raise HistoricalIdentityUnavailableError("identity artifact collision") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return target

    def load(self, artifact_id: str) -> HistoricalIdentityArtifact:
        if not isinstance(artifact_id, str) or not _DIGEST.fullmatch(artifact_id):
            raise HistoricalIdentityUnavailableError("invalid identity artifact id")
        try:
            value = HistoricalIdentityArtifact.from_dict(json.loads(
                (self.root / f"artifact_id={artifact_id}.json").read_text()
            ))
            if value.artifact_id != artifact_id:
                raise ValueError("identity artifact path mismatch")
            return value
        except Exception:
            raise HistoricalIdentityUnavailableError(
                "historical identity artifact is missing or corrupt"
            ) from None
