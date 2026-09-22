"""Immutable, serializable contracts for historical replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import hashlib
import re
from typing import Any, Callable
from uuid import uuid4

from quantos.serialization import canonical_identity_json_bytes


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_DATASET_SCHEMA = "quantos-historical-dataset-v1"
_CAMPAIGN_SCHEMA = "quantos-replay-campaign-v1"


class ReplayMode(str, Enum):
    STRICT_OPERATIONAL_PIT = "STRICT_OPERATIONAL_PIT"
    RETROSPECTIVE_RECONSTRUCTED = "RETROSPECTIVE_RECONSTRUCTED"


class IdentityTemporalSemantics(str, Enum):
    OBSERVED_KNOWLEDGE = "OBSERVED_KNOWLEDGE"
    RETROSPECTIVE_EFFECTIVE_TRUTH = "RETROSPECTIVE_EFFECTIVE_TRUTH"


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_identity_json_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ReplayClock:
    as_of_time: datetime

    def __post_init__(self) -> None:
        _aware(self.as_of_time, "as_of_time")

    def now(self) -> datetime:
        return self.as_of_time


@dataclass(frozen=True, slots=True)
class ReplayUniverse:
    symbols: tuple[str, ...]
    market: str
    start_date: date
    end_date: date
    trading_days: tuple[date, ...]

    def __post_init__(self) -> None:
        if (not self.symbols or tuple(sorted(set(self.symbols))) != self.symbols
                or any(not _SYMBOL.fullmatch(item) for item in self.symbols)):
            raise ValueError("invalid replay symbols")
        if self.market != "A_SHARE" or self.start_date > self.end_date:
            raise ValueError("invalid replay market or range")
        if (not self.trading_days
                or tuple(sorted(set(self.trading_days))) != self.trading_days
                or any(day < self.start_date or day > self.end_date for day in self.trading_days)):
            raise ValueError("invalid replay trading days")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols), "market": self.market,
            "start_date": self.start_date.isoformat(), "end_date": self.end_date.isoformat(),
            "trading_days": [item.isoformat() for item in self.trading_days],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReplayUniverse:
        return cls(
            tuple(value["symbols"]), value["market"], date.fromisoformat(value["start_date"]),
            date.fromisoformat(value["end_date"]),
            tuple(date.fromisoformat(item) for item in value["trading_days"]),
        )


@dataclass(frozen=True, slots=True)
class HistoricalDatasetManifest:
    schema_version: str
    dataset_id: str
    provider: str
    market: str
    symbols: tuple[str, ...]
    start_date: date
    end_date: date
    trading_dates: tuple[date, ...]
    record_refs: tuple[str, ...]
    imported_at: datetime
    status: str = "PASS"
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != _DATASET_SCHEMA or not _DIGEST.fullmatch(self.dataset_id):
            raise ValueError("invalid historical dataset identity")
        ReplayUniverse(self.symbols, self.market, self.start_date, self.end_date,
                       self.trading_dates)
        if tuple(sorted(set(self.record_refs))) != self.record_refs or not self.record_refs:
            raise ValueError("historical dataset requires unique record refs")
        _aware(self.imported_at, "imported_at")
        if self.status not in {"PASS", "FAIL"}:
            raise ValueError("invalid historical dataset status")
        if self.dataset_id != self.semantic_id:
            raise ValueError("historical dataset semantic identity mismatch")

    @property
    def semantic_id(self) -> str:
        return _hash(self.semantic_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "provider": self.provider,
            "market": self.market, "symbols": list(self.symbols),
            "start_date": self.start_date.isoformat(), "end_date": self.end_date.isoformat(),
            "trading_dates": [item.isoformat() for item in self.trading_dates],
            "record_refs": list(self.record_refs), "status": self.status,
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def build(cls, *, provider: str, market: str, symbols: tuple[str, ...],
              start_date: date, end_date: date, trading_dates: tuple[date, ...],
              record_refs: tuple[str, ...], imported_at: datetime,
              status: str = "PASS", reason_codes: tuple[str, ...] = ()) -> HistoricalDatasetManifest:
        values = {
            "schema_version": _DATASET_SCHEMA, "provider": provider, "market": market,
            "symbols": list(symbols), "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "trading_dates": [item.isoformat() for item in trading_dates],
            "record_refs": list(tuple(sorted(record_refs))), "status": status,
            "reason_codes": list(reason_codes),
        }
        return cls(_DATASET_SCHEMA, _hash(values), provider, market, symbols,
                   start_date, end_date, trading_dates, tuple(sorted(record_refs)),
                   imported_at, status, reason_codes)

    def to_dict(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "dataset_id": self.dataset_id,
                "imported_at": self.imported_at.isoformat()}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> HistoricalDatasetManifest:
        return cls(
            value["schema_version"], value["dataset_id"], value["provider"], value["market"],
            tuple(value["symbols"]), date.fromisoformat(value["start_date"]),
            date.fromisoformat(value["end_date"]),
            tuple(date.fromisoformat(item) for item in value["trading_dates"]),
            tuple(value["record_refs"]), datetime.fromisoformat(value["imported_at"]),
            value["status"], tuple(value["reason_codes"]),
        )


@dataclass(frozen=True, slots=True)
class ReplayPoint:
    replay_id: str
    symbol: str
    trading_date: date
    as_of_time: datetime
    dataset_ref: str
    security_snapshot_ref: str | None
    replay_mode: ReplayMode = ReplayMode.STRICT_OPERATIONAL_PIT
    identity_authority_ref: str | None = None

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.replay_id) or not _SYMBOL.fullmatch(self.symbol):
            raise ValueError("invalid replay point identity")
        _aware(self.as_of_time, "as_of_time")
        if self.as_of_time.date() != self.trading_date or not _DIGEST.fullmatch(self.dataset_ref):
            raise ValueError("invalid replay point boundary")
        if self.security_snapshot_ref is not None and not _DIGEST.fullmatch(
            self.security_snapshot_ref
        ):
            raise ValueError("invalid Security Master snapshot ref")
        if not isinstance(self.replay_mode, ReplayMode):
            raise ValueError("invalid replay mode")
        if self.replay_mode is ReplayMode.STRICT_OPERATIONAL_PIT:
            if self.identity_authority_ref is not None:
                raise ValueError("strict replay cannot use retrospective identity authority")
        elif (self.identity_authority_ref is None
              or not _DIGEST.fullmatch(self.identity_authority_ref)):
            raise ValueError("retrospective replay requires identity authority")

    @classmethod
    def build(cls, *, symbol: str, trading_date: date, as_of_time: datetime,
              dataset_ref: str, security_snapshot_ref: str | None,
              replay_mode: ReplayMode = ReplayMode.STRICT_OPERATIONAL_PIT,
              identity_authority_ref: str | None = None) -> ReplayPoint:
        identity = _hash({
            "symbol": symbol, "trading_date": trading_date.isoformat(),
            "as_of_time": as_of_time.isoformat(), "dataset_ref": dataset_ref,
            "security_snapshot_ref": security_snapshot_ref,
            "replay_mode": replay_mode.value,
            "identity_authority_ref": identity_authority_ref,
        })
        return cls(identity, symbol, trading_date, as_of_time, dataset_ref,
                   security_snapshot_ref, replay_mode, identity_authority_ref)

    def to_dict(self) -> dict[str, Any]:
        return {
            "replay_id": self.replay_id, "symbol": self.symbol,
            "trading_date": self.trading_date.isoformat(),
            "as_of_time": self.as_of_time.isoformat(), "dataset_ref": self.dataset_ref,
            "security_snapshot_ref": self.security_snapshot_ref,
            "replay_mode": self.replay_mode.value,
            "identity_authority_ref": self.identity_authority_ref,
        }


@dataclass(frozen=True, slots=True)
class ReplayResult:
    replay_id: str
    as_of_time: datetime
    symbol: str
    status: str
    market_available: bool
    identity_available: bool
    evidence_available: bool
    strict_evidence_available: bool
    attribution_available: bool
    knowledge_available: bool
    facts: tuple[dict[str, Any], ...]
    references: tuple[str, ...]
    reason_codes: tuple[str, ...]
    trace_id: str | None
    run_id: str | None
    duration_ms: float
    pit_rejection_count: int
    provider_failure_count: int
    replay_mode: ReplayMode = ReplayMode.STRICT_OPERATIONAL_PIT
    identity_temporal_semantics: IdentityTemporalSemantics = (
        IdentityTemporalSemantics.OBSERVED_KNOWLEDGE
    )
    operational_identity_pit_rejection_count: int = 0
    market_pit_violation_count: int = 0
    evidence_pit_violation_count: int = 0
    report_pit_violation_count: int = 0
    knowledge_pit_violation_count: int = 0

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.replay_id) or not _SYMBOL.fullmatch(self.symbol):
            raise ValueError("invalid replay result identity")
        _aware(self.as_of_time, "as_of_time")
        if self.status not in {"PASS", "PARTIAL", "FAILED", "SKIPPED"}:
            raise ValueError("invalid replay result status")
        if self.duration_ms < 0 or min(self.pit_rejection_count, self.provider_failure_count) < 0:
            raise ValueError("invalid replay result counters")
        counters = (
            self.operational_identity_pit_rejection_count,
            self.market_pit_violation_count, self.evidence_pit_violation_count,
            self.report_pit_violation_count, self.knowledge_pit_violation_count,
        )
        if min(counters) < 0:
            raise ValueError("invalid temporal violation counters")
        if not isinstance(self.replay_mode, ReplayMode):
            raise ValueError("invalid replay result mode")
        expected_semantics = (
            IdentityTemporalSemantics.OBSERVED_KNOWLEDGE
            if self.replay_mode is ReplayMode.STRICT_OPERATIONAL_PIT
            else IdentityTemporalSemantics.RETROSPECTIVE_EFFECTIVE_TRUTH
        )
        if self.identity_temporal_semantics is not expected_semantics:
            raise ValueError("identity temporal semantics do not match replay mode")
        if tuple(sorted(set(self.references))) != self.references:
            raise ValueError("replay references must be unique and sorted")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("replay reason codes must be unique and sorted")

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "replay_id": self.replay_id, "as_of_time": self.as_of_time.isoformat(),
            "symbol": self.symbol, "status": self.status,
            "market_available": self.market_available,
            "identity_available": self.identity_available,
            "evidence_available": self.evidence_available,
            "strict_evidence_available": self.strict_evidence_available,
            "attribution_available": self.attribution_available,
            "knowledge_available": self.knowledge_available,
            "facts": list(self.facts), "references": list(self.references),
            "reason_codes": list(self.reason_codes),
            "pit_rejection_count": self.pit_rejection_count,
            "provider_failure_count": self.provider_failure_count,
            "replay_mode": self.replay_mode.value,
            "identity_temporal_semantics": self.identity_temporal_semantics.value,
            "operational_identity_pit_rejection_count": (
                self.operational_identity_pit_rejection_count
            ),
            "market_pit_violation_count": self.market_pit_violation_count,
            "evidence_pit_violation_count": self.evidence_pit_violation_count,
            "report_pit_violation_count": self.report_pit_violation_count,
            "knowledge_pit_violation_count": self.knowledge_pit_violation_count,
        }

    @property
    def semantic_hash(self) -> str:
        return _hash(self.semantic_payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self.semantic_payload(), "semantic_hash": self.semantic_hash,
                "trace_id": self.trace_id, "run_id": self.run_id,
                "duration_ms": self.duration_ms}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReplayResult:
        legacy = "replay_mode" not in value
        replay_mode = ReplayMode(value.get(
            "replay_mode", ReplayMode.STRICT_OPERATIONAL_PIT.value,
        ))
        result = cls(
            replay_id=value["replay_id"],
            as_of_time=datetime.fromisoformat(value["as_of_time"]),
            symbol=value["symbol"], status=value["status"],
            market_available=value["market_available"],
            identity_available=value["identity_available"],
            evidence_available=value["evidence_available"],
            strict_evidence_available=value["strict_evidence_available"],
            attribution_available=value["attribution_available"],
            knowledge_available=value["knowledge_available"],
            facts=tuple(value["facts"]), references=tuple(value["references"]),
            reason_codes=tuple(value["reason_codes"]), trace_id=value["trace_id"],
            run_id=value.get("run_id"), duration_ms=value["duration_ms"],
            pit_rejection_count=value["pit_rejection_count"],
            provider_failure_count=value["provider_failure_count"],
            replay_mode=replay_mode,
            identity_temporal_semantics=IdentityTemporalSemantics(value.get(
                "identity_temporal_semantics",
                IdentityTemporalSemantics.OBSERVED_KNOWLEDGE.value,
            )),
            operational_identity_pit_rejection_count=value.get(
                "operational_identity_pit_rejection_count", 0,
            ),
            market_pit_violation_count=value.get("market_pit_violation_count", 0),
            evidence_pit_violation_count=value.get("evidence_pit_violation_count", 0),
            report_pit_violation_count=value.get("report_pit_violation_count", 0),
            knowledge_pit_violation_count=value.get("knowledge_pit_violation_count", 0),
        )
        expected_hash = result.semantic_hash
        if legacy:
            expected_hash = _hash({
                key: result.semantic_payload()[key]
                for key in (
                    "replay_id", "as_of_time", "symbol", "status",
                    "market_available", "identity_available", "evidence_available",
                    "strict_evidence_available", "attribution_available",
                    "knowledge_available", "facts", "references", "reason_codes",
                    "pit_rejection_count", "provider_failure_count",
                )
            })
        if value.get("semantic_hash") != expected_hash:
            raise ValueError("replay result semantic hash mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ReplaySummary:
    requested_points: int
    executed_points: int
    pass_points: int
    partial_points: int
    failed_points: int
    skipped_points: int
    identity_covered: int
    market_covered: int
    evidence_covered: int
    strict_evidence_covered: int
    attribution_covered: int
    knowledge_covered: int
    attribution_insufficient_count: int
    no_evidence_count: int
    pit_rejection_count: int
    provider_failure_count: int
    deterministic_mismatch_count: int
    llm_call_count: int = 0
    operational_identity_pit_rejection_count: int = 0
    market_pit_violation_count: int = 0
    evidence_pit_violation_count: int = 0
    report_pit_violation_count: int = 0
    knowledge_pit_violation_count: int = 0
    replay_mode: ReplayMode = ReplayMode.STRICT_OPERATIONAL_PIT

    @classmethod
    def from_results(cls, results: tuple[ReplayResult, ...], *,
                     deterministic_mismatch_count: int) -> ReplaySummary:
        modes = {item.replay_mode for item in results}
        if len(modes) > 1:
            raise ValueError("replay summary cannot mix modes")
        replay_mode = next(iter(modes), ReplayMode.STRICT_OPERATIONAL_PIT)
        return cls(
            len(results), sum(item.status != "SKIPPED" for item in results),
            sum(item.status == "PASS" for item in results),
            sum(item.status == "PARTIAL" for item in results),
            sum(item.status == "FAILED" for item in results),
            sum(item.status == "SKIPPED" for item in results),
            sum(item.identity_available for item in results),
            sum(item.market_available for item in results),
            sum(item.evidence_available for item in results),
            sum(item.strict_evidence_available for item in results),
            sum(item.attribution_available for item in results),
            sum(item.knowledge_available for item in results),
            sum("ATTRIBUTION_INSUFFICIENT" in item.reason_codes for item in results),
            sum("EVIDENCE_UNAVAILABLE" in item.reason_codes for item in results),
            sum(item.pit_rejection_count for item in results),
            sum(item.provider_failure_count for item in results),
            deterministic_mismatch_count, 0,
            sum(item.operational_identity_pit_rejection_count for item in results),
            sum(item.market_pit_violation_count for item in results),
            sum(item.evidence_pit_violation_count for item in results),
            sum(item.report_pit_violation_count for item in results),
            sum(item.knowledge_pit_violation_count for item in results),
            replay_mode,
        )

    def to_dict(self) -> dict[str, Any]:
        values = {name: getattr(self, name) for name in self.__dataclass_fields__}
        values["replay_mode"] = self.replay_mode.value
        values["historical_semantics"] = self.replay_mode.value
        values["semantics_limitation"] = (
            "Reconstructs effective-time identity truth observed after the replay date; "
            "not an operational-knowledge replay."
            if self.replay_mode is ReplayMode.RETROSPECTIVE_RECONSTRUCTED else
            "Represents only information observed by QuantOS at the replay cutoff."
        )
        denominator = self.requested_points or 1
        values.update({
            "replay_coverage": self.executed_points / denominator,
            "pass_rate": self.pass_points / denominator,
            "partial_rate": self.partial_points / denominator,
            "failed_rate": self.failed_points / denominator,
            "identity_coverage": self.identity_covered / denominator,
            "market_coverage": self.market_covered / denominator,
            "evidence_coverage": self.evidence_covered / denominator,
            "strict_evidence_coverage": self.strict_evidence_covered / denominator,
            "attribution_coverage": self.attribution_covered / denominator,
            "knowledge_coverage": self.knowledge_covered / denominator,
        })
        return values

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReplaySummary:
        defaults = {
            "operational_identity_pit_rejection_count": 0,
            "market_pit_violation_count": 0,
            "evidence_pit_violation_count": 0,
            "report_pit_violation_count": 0,
            "knowledge_pit_violation_count": 0,
            "replay_mode": ReplayMode.STRICT_OPERATIONAL_PIT.value,
        }
        values = {
            name: value.get(name, defaults.get(name))
            for name in cls.__dataclass_fields__
        }
        values["replay_mode"] = ReplayMode(values["replay_mode"])
        return cls(**values)


@dataclass(frozen=True, slots=True)
class ReplayFailureRecord:
    replay_id: str
    date: date
    symbol: str
    stage: str
    reason_codes: tuple[str, ...]
    available_inputs: tuple[str, ...]
    missing_inputs: tuple[str, ...]
    trace_id: str | None
    replay_mode: ReplayMode = ReplayMode.STRICT_OPERATIONAL_PIT

    @classmethod
    def from_result(cls, result: ReplayResult, *, stage: str) -> ReplayFailureRecord:
        flags = {
            "identity": result.identity_available, "market": result.market_available,
            "evidence": result.evidence_available,
            "strict_evidence": result.strict_evidence_available,
            "attribution": result.attribution_available,
            "knowledge": result.knowledge_available,
        }
        return cls(
            result.replay_id, result.as_of_time.date(), result.symbol, stage,
            result.reason_codes, tuple(sorted(key for key, ok in flags.items() if ok)),
            tuple(sorted(key for key, ok in flags.items() if not ok)), result.trace_id,
            result.replay_mode,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "replay_id": self.replay_id, "date": self.date.isoformat(),
            "symbol": self.symbol, "stage": self.stage,
            "reason_codes": list(self.reason_codes),
            "available_inputs": list(self.available_inputs),
            "missing_inputs": list(self.missing_inputs), "trace_id": self.trace_id,
            "replay_mode": self.replay_mode.value,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReplayFailureRecord:
        return cls(value["replay_id"], date.fromisoformat(value["date"]), value["symbol"],
                   value["stage"], tuple(value["reason_codes"]),
                   tuple(value["available_inputs"]), tuple(value["missing_inputs"]),
                   value["trace_id"], ReplayMode(value.get(
                       "replay_mode", ReplayMode.STRICT_OPERATIONAL_PIT.value,
                   )))


@dataclass(frozen=True, slots=True)
class ReplayCampaignManifest:
    schema_version: str
    campaign_id: str
    created_at: datetime
    universe: ReplayUniverse
    dataset_refs: tuple[str, ...]
    security_snapshot_refs: tuple[str, ...]
    configuration_hash: str
    code_version: str
    dirty: bool
    results: tuple[ReplayResult, ...]
    summary: ReplaySummary
    replay_mode: ReplayMode = ReplayMode.STRICT_OPERATIONAL_PIT
    identity_authority_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != _CAMPAIGN_SCHEMA or not re.fullmatch(r"[0-9a-f]{32}", self.campaign_id):
            raise ValueError("invalid replay campaign identity")
        _aware(self.created_at, "created_at")
        if any(not _DIGEST.fullmatch(item) for item in (*self.dataset_refs,
                                                        *self.security_snapshot_refs)):
            raise ValueError("invalid campaign dataset reference")
        if not _DIGEST.fullmatch(self.configuration_hash):
            raise ValueError("invalid replay configuration hash")
        if not isinstance(self.replay_mode, ReplayMode):
            raise ValueError("invalid campaign replay mode")
        if any(item.replay_mode is not self.replay_mode for item in self.results):
            raise ValueError("campaign results mix replay modes")
        if self.summary.replay_mode is not self.replay_mode:
            raise ValueError("campaign summary replay mode mismatch")
        if any(not _DIGEST.fullmatch(item) for item in self.identity_authority_refs):
            raise ValueError("invalid identity authority reference")
        if (self.replay_mode is ReplayMode.STRICT_OPERATIONAL_PIT
                and self.identity_authority_refs):
            raise ValueError("strict campaign cannot use retrospective authority")
        if self.summary != ReplaySummary.from_results(
            self.results,
            deterministic_mismatch_count=self.summary.deterministic_mismatch_count,
        ):
            raise ValueError("replay summary mismatch")

    @classmethod
    def build(cls, *, created_at: datetime, universe: ReplayUniverse,
              dataset_refs: tuple[str, ...], security_snapshot_refs: tuple[str, ...],
              configuration_hash: str, code_version: str, dirty: bool,
              results: tuple[ReplayResult, ...], summary: ReplaySummary,
              replay_mode: ReplayMode = ReplayMode.STRICT_OPERATIONAL_PIT,
              identity_authority_refs: tuple[str, ...] = ()) -> ReplayCampaignManifest:
        return cls(_CAMPAIGN_SCHEMA, uuid4().hex, created_at, universe,
                   tuple(sorted(set(dataset_refs))),
                   tuple(sorted(set(security_snapshot_refs))), configuration_hash,
                   code_version, dirty, results, summary, replay_mode,
                   tuple(sorted(set(identity_authority_refs))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "campaign_id": self.campaign_id,
            "created_at": self.created_at.isoformat(), "universe": self.universe.to_dict(),
            "dataset_refs": list(self.dataset_refs),
            "security_snapshot_refs": list(self.security_snapshot_refs),
            "configuration_hash": self.configuration_hash,
            "code_version": self.code_version, "dirty": self.dirty,
            "replay_mode": self.replay_mode.value,
            "identity_authority_refs": list(self.identity_authority_refs),
            "results": [item.to_dict() for item in self.results],
            "summary": self.summary.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReplayCampaignManifest:
        return cls(
            value["schema_version"], value["campaign_id"],
            datetime.fromisoformat(value["created_at"]),
            ReplayUniverse.from_dict(value["universe"]), tuple(value["dataset_refs"]),
            tuple(value["security_snapshot_refs"]), value["configuration_hash"],
            value["code_version"], value["dirty"],
            tuple(ReplayResult.from_dict(item) for item in value["results"]),
            ReplaySummary.from_dict(value["summary"]),
            ReplayMode(value.get("replay_mode", ReplayMode.STRICT_OPERATIONAL_PIT.value)),
            tuple(value.get("identity_authority_refs", ())),
        )
