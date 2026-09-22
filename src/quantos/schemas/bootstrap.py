"""Closed contracts for provider health and real-data bootstrap audit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import re
from typing import Any

from quantos.serialization import canonical_identity_json_bytes

from ._validation import require_aware, require_non_empty
from .securities import SecurityMaster


SECURITY_MASTER_SNAPSHOT_SCHEMA_VERSION = "quantos-security-master-snapshot-v1"
BOOTSTRAP_MANIFEST_SCHEMA_VERSION = "quantos-data-bootstrap-v1"
PROVIDER_HEALTH_SCHEMA_VERSION = "quantos-provider-health-v1"

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ProviderAvailability(str, Enum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    UNCONFIGURED = "UNCONFIGURED"
    DISABLED = "DISABLED"


class ProviderFailureCode(str, Enum):
    UNCONFIGURED = "UNCONFIGURED"
    AUTH_FAILED = "AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_FAILED = "NETWORK_FAILED"
    REMOTE_ERROR = "REMOTE_ERROR"
    EMPTY_RESULT = "EMPTY_RESULT"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    NORMALIZATION_FAILED = "NORMALIZATION_FAILED"
    STORAGE_FAILED = "STORAGE_FAILED"


@dataclass(frozen=True, slots=True)
class ProviderContract:
    provider_id: str
    provider_type: str
    capabilities: tuple[str, ...]
    configuration_requirements: tuple[str, ...]
    credential_configured: bool
    availability: ProviderAvailability
    last_success: datetime | None = None
    last_failure: datetime | None = None
    health_reason: str | None = None

    def __post_init__(self) -> None:
        require_non_empty(self.provider_id, "provider_id")
        require_non_empty(self.provider_type, "provider_type")
        if not self.capabilities or tuple(sorted(set(self.capabilities))) != self.capabilities:
            raise ValueError("provider capabilities must be unique and sorted")
        if tuple(sorted(set(self.configuration_requirements))) != self.configuration_requirements:
            raise ValueError("provider configuration requirements must be unique and sorted")
        if not isinstance(self.availability, ProviderAvailability):
            raise ValueError("invalid provider availability")
        for value, name in ((self.last_success, "last_success"),
                            (self.last_failure, "last_failure")):
            if value is not None:
                require_aware(value, name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_type": self.provider_type,
            "availability": self.availability.value,
            "capabilities": list(self.capabilities),
            "configuration_requirements": list(self.configuration_requirements),
            "credential_configured": self.credential_configured,
            "last_success": _iso(self.last_success),
            "last_failure": _iso(self.last_failure),
            "health_reason": self.health_reason,
        }


@dataclass(frozen=True, slots=True)
class ProviderHealthRecord:
    provider_id: str
    availability: ProviderAvailability
    checked_at: datetime
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure_code: ProviderFailureCode | None = None
    schema_version: str = PROVIDER_HEALTH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROVIDER_HEALTH_SCHEMA_VERSION:
            raise ValueError("unsupported provider health schema")
        require_non_empty(self.provider_id, "provider_id")
        require_aware(self.checked_at, "checked_at")
        for value, name in ((self.last_success_at, "last_success_at"),
                            (self.last_failure_at, "last_failure_at")):
            if value is not None:
                require_aware(value, name)
        if not isinstance(self.availability, ProviderAvailability):
            raise ValueError("invalid provider availability")
        if self.last_failure_code is not None and not isinstance(
            self.last_failure_code, ProviderFailureCode
        ):
            raise ValueError("invalid provider failure code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "availability": self.availability.value,
            "checked_at": self.checked_at.isoformat(),
            "last_success_at": _iso(self.last_success_at),
            "last_failure_at": _iso(self.last_failure_at),
            "last_failure_code": (
                self.last_failure_code.value if self.last_failure_code else None
            ),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProviderHealthRecord:
        return cls(
            value["provider_id"], ProviderAvailability(value["availability"]),
            datetime.fromisoformat(value["checked_at"]),
            _datetime(value["last_success_at"]),
            _datetime(value["last_failure_at"]),
            ProviderFailureCode(value["last_failure_code"])
            if value["last_failure_code"] else None,
            value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class SecurityIdentityRecord:
    canonical_security_id: str
    security: SecurityMaster

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.canonical_security_id):
            raise ValueError("canonical_security_id must be a SHA-256 digest")
        if not isinstance(self.security, SecurityMaster):
            raise ValueError("SecurityMaster required")


@dataclass(frozen=True, slots=True)
class SecurityMasterSnapshot:
    snapshot_id: str
    provider_id: str
    observed_at: datetime
    records: tuple[SecurityIdentityRecord, ...]
    schema_version: str = SECURITY_MASTER_SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SECURITY_MASTER_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("unsupported Security Master snapshot schema")
        if not _DIGEST.fullmatch(self.snapshot_id):
            raise ValueError("snapshot_id must be a SHA-256 digest")
        require_non_empty(self.provider_id, "provider_id")
        require_aware(self.observed_at, "observed_at")
        if not self.records:
            raise ValueError("Security Master snapshot cannot be empty")
        symbols = tuple(item.security.symbol for item in self.records)
        if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
            raise ValueError("Security Master records must be unique and sorted")


@dataclass(frozen=True, slots=True)
class BootstrapManifest:
    bootstrap_id: str
    operation: str
    started_at: datetime
    completed_at: datetime
    providers: tuple[str, ...]
    datasets: tuple[str, ...]
    records_received: int
    records_valid: int
    records_rejected: int
    artifacts: tuple[str, ...]
    status: str
    reason_codes: tuple[str, ...]
    schema_version: str = BOOTSTRAP_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BOOTSTRAP_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported bootstrap manifest schema")
        if not re.fullmatch(r"[0-9a-f]{32}", self.bootstrap_id):
            raise ValueError("bootstrap_id must be a UUID hex value")
        if self.operation not in {"bootstrap", "refresh"}:
            raise ValueError("invalid bootstrap operation")
        require_aware(self.started_at, "started_at")
        require_aware(self.completed_at, "completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("bootstrap completion precedes start")
        if self.status not in {"PASS", "PARTIAL", "FAIL"}:
            raise ValueError("invalid bootstrap status")
        if min(self.records_received, self.records_valid, self.records_rejected) < 0:
            raise ValueError("bootstrap counts cannot be negative")
        if self.records_valid + self.records_rejected != self.records_received:
            raise ValueError("bootstrap counts do not reconcile")
        if not self.providers or not self.datasets:
            raise ValueError("bootstrap provider and dataset are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "bootstrap_id": self.bootstrap_id,
            "operation": self.operation,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "providers": list(self.providers),
            "datasets": list(self.datasets),
            "records_received": self.records_received,
            "records_valid": self.records_valid,
            "records_rejected": self.records_rejected,
            "artifacts": list(self.artifacts),
            "status": self.status,
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BootstrapManifest:
        return cls(
            value["bootstrap_id"], value["operation"],
            datetime.fromisoformat(value["started_at"]),
            datetime.fromisoformat(value["completed_at"]),
            tuple(value["providers"]), tuple(value["datasets"]),
            value["records_received"], value["records_valid"],
            value["records_rejected"], tuple(value["artifacts"]),
            value["status"], tuple(value["reason_codes"]), value["schema_version"],
        )


def canonical_security_id(value: SecurityMaster) -> str:
    identity = {
        "market": "CN_A_SHARE",
        "exchange": value.exchange,
        "initial_symbol": value.symbol,
        "list_date": value.effective_from.isoformat(),
    }
    return hashlib.sha256(canonical_identity_json_bytes(identity)).hexdigest()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
