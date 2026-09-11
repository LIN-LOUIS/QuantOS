"""Immutable run control-plane contracts; separate from JobContext and Health."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import hashlib
import json

from ._validation import require_aware, require_non_empty
from quantos.serialization import canonical_identity_json_bytes

RUN_SCHEMA_VERSION = "quantos-run-v1"
KNOWLEDGE_RUN_SCHEMA_VERSION = "quantos-run-v2"
KNOWLEDGE_OPERATIONAL_STATE_SCHEMA_VERSION = "knowledge-operational-state-v1"
RUN_MANIFEST_SCHEMA_VERSIONS = frozenset({
    RUN_SCHEMA_VERSION, KNOWLEDGE_RUN_SCHEMA_VERSION,
})
ORCHESTRATION_POLICY_VERSION = "quantos-run-policy-v1"


class RunType(str, Enum):
    PRE_OPEN = "PRE_OPEN"
    INTRADAY = "INTRADAY"
    POST_CLOSE = "POST_CLOSE"


class ModuleName(str, Enum):
    MARKET_CONTEXT = "MARKET_CONTEXT"
    SECTOR_CONTEXT = "SECTOR_CONTEXT"
    ANOMALY_TRIAGE = "ANOMALY_TRIAGE"
    FUND_FLOW = "FUND_FLOW"
    EVIDENCE = "EVIDENCE"
    ATTRIBUTION = "ATTRIBUTION"
    SYNTHESIS = "SYNTHESIS"
    DAILY_REPORT = "DAILY_REPORT"


class ModuleAvailabilityStatus(str, Enum):
    READY = "READY"
    SKIP_NOT_APPLICABLE = "SKIP_NOT_APPLICABLE"
    SKIP_NOT_AVAILABLE_YET = "SKIP_NOT_AVAILABLE_YET"
    SKIP_DATA_MISSING = "SKIP_DATA_MISSING"
    SKIP_UNSUPPORTED_IN_V1 = "SKIP_UNSUPPORTED_IN_V1"
    SKIP_POLICY_DISABLED = "SKIP_POLICY_DISABLED"
    BLOCKED_DEPENDENCY = "BLOCKED_DEPENDENCY"


class ModuleExecutionStatus(str, Enum):
    PASS = "PASS"
    SKIP = "SKIP"
    FAIL = "FAIL"


class KnowledgeOperationalStatus(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    EMPTY = "EMPTY"
    READY = "READY"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    CORRUPT = "CORRUPT"
    FAILED = "FAILED"
    FAILED_INTEGRITY = "FAILED_INTEGRITY"


@dataclass(frozen=True, slots=True)
class RunContext:
    target_trade_date: date
    market_basis_trade_date: date | None
    as_of_time: datetime
    run_type: RunType
    mode: str
    universe_name: str
    top_n: int
    llm_allowed: bool
    generated_at: datetime
    schema_version: str = RUN_SCHEMA_VERSION

    def __post_init__(self):
        require_aware(self.as_of_time, "as_of_time")
        require_aware(self.generated_at, "generated_at")
        if not isinstance(self.run_type, RunType):
            raise ValueError("run_type must be an explicit RunType")
        if self.mode not in {"research", "strict_live"}:
            raise ValueError("mode must be explicit")
        if type(self.target_trade_date) is not date:
            raise ValueError("target_trade_date must be date")
        if self.market_basis_trade_date is not None:
            if type(self.market_basis_trade_date) is not date:
                raise ValueError("basis must be date")
            if self.market_basis_trade_date > self.target_trade_date:
                raise ValueError("basis cannot be after target date")
            if self.run_type != RunType.POST_CLOSE and self.market_basis_trade_date >= self.target_trade_date:
                raise ValueError("background basis must precede target date")
        if type(self.top_n) is not int or self.top_n < 1:
            raise ValueError("top_n must be positive integer")
        if type(self.llm_allowed) is not bool:
            raise ValueError("llm_allowed must be boolean")
        if self.schema_version != RUN_SCHEMA_VERSION:
            raise ValueError("unsupported run schema")
        require_non_empty(self.universe_name, "universe_name")

    @property
    def run_id(self) -> str:
        identity = {
            "target_trade_date": self.target_trade_date.isoformat(),
            "market_basis_trade_date": self.market_basis_trade_date.isoformat() if self.market_basis_trade_date else None,
            "as_of_time": self.as_of_time, "run_type": self.run_type.value,
            "mode": self.mode, "universe_name": self.universe_name,
            "top_n": self.top_n, "llm_allowed": self.llm_allowed,
            "run_schema_version": self.schema_version,
            "orchestration_policy_version": ORCHESTRATION_POLICY_VERSION,
        }
        return hashlib.sha256(canonical_identity_json_bytes(identity)).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    artifact_id: str
    path: str
    version: str | None


_KNOWLEDGE_REASON_CODES = {
    KnowledgeOperationalStatus.NOT_CONFIGURED: {"KNOWLEDGE_NOT_CONFIGURED"},
    KnowledgeOperationalStatus.EMPTY: {"KNOWLEDGE_EMPTY"},
    KnowledgeOperationalStatus.READY: {"KNOWLEDGE_READY"},
    KnowledgeOperationalStatus.UNAVAILABLE: {"PRODUCT_UNAVAILABLE"},
    KnowledgeOperationalStatus.STALE: {
        "PRODUCT_DATE_MISMATCH", "PRODUCT_NOT_PIT_VISIBLE",
    },
    KnowledgeOperationalStatus.CORRUPT: {
        "PRODUCT_CORRUPT", "PRODUCT_ARTIFACT_HASH_MISMATCH",
        "PRODUCT_IDENTITY_MISMATCH", "PRODUCT_SCHEMA_MISMATCH",
    },
    KnowledgeOperationalStatus.FAILED: {
        "INTERNAL_FAILURE", "PRODUCT_VALIDATION_FAILED",
    },
    KnowledgeOperationalStatus.FAILED_INTEGRITY: {
        "PRODUCT_MODE_MISMATCH", "PRODUCT_SELECTION_AMBIGUOUS",
        "PRODUCT_UNIVERSE_MISMATCH", "PRODUCT_TOP_N_MISMATCH",
    },
}


@dataclass(frozen=True, slots=True)
class KnowledgeOperationalState:
    """Observed validated product state; never claims retrieval was executed."""

    status: KnowledgeOperationalStatus
    reason_code: str
    product_ref: ArtifactReference | None
    source_report_id: str | None
    candidate_count: int
    ready_candidate_count: int
    empty_candidate_count: int
    schema_version: str = KNOWLEDGE_OPERATIONAL_STATE_SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != KNOWLEDGE_OPERATIONAL_STATE_SCHEMA_VERSION:
            raise ValueError("unsupported Knowledge operational state schema")
        if not isinstance(self.status, KnowledgeOperationalStatus):
            raise ValueError("invalid Knowledge operational status")
        if self.reason_code not in _KNOWLEDGE_REASON_CODES[self.status]:
            raise ValueError("invalid Knowledge operational reason code")
        for value in (
            self.candidate_count,
            self.ready_candidate_count,
            self.empty_candidate_count,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("invalid Knowledge operational candidate count")
        successful = self.status in {
            KnowledgeOperationalStatus.NOT_CONFIGURED,
            KnowledgeOperationalStatus.EMPTY,
            KnowledgeOperationalStatus.READY,
        }
        if successful and (self.product_ref is None or self.source_report_id is None):
            raise ValueError("successful Knowledge observation requires a product reference")
        if not successful and self.source_report_id is not None:
            raise ValueError("failed Knowledge observation cannot claim a report identity")
        if self.product_ref is not None:
            if (
                not self.product_ref.path
                or not self.product_ref.version
                or len(self.product_ref.artifact_id) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in self.product_ref.artifact_id
                )
            ):
                raise ValueError("invalid operational product reference")
        if self.source_report_id is not None:
            if len(self.source_report_id) != 64 or any(
                character not in "0123456789abcdef"
                for character in self.source_report_id
            ):
                raise ValueError("invalid operational source report identity")
        if self.status is KnowledgeOperationalStatus.NOT_CONFIGURED:
            if self.ready_candidate_count or self.empty_candidate_count:
                raise ValueError("legacy product cannot claim configured Knowledge candidates")
        elif self.status is KnowledgeOperationalStatus.EMPTY:
            if (
                self.candidate_count < 1
                or self.ready_candidate_count
                or self.empty_candidate_count != self.candidate_count
            ):
                raise ValueError("EMPTY Knowledge aggregation is inconsistent")
        elif self.status is KnowledgeOperationalStatus.READY:
            if self.ready_candidate_count < 1 or (
                self.ready_candidate_count + self.empty_candidate_count
                != self.candidate_count
            ):
                raise ValueError("READY Knowledge aggregation is inconsistent")
        elif any((self.candidate_count, self.ready_candidate_count, self.empty_candidate_count)):
            raise ValueError("failed Knowledge observation cannot claim candidate aggregation")

    @property
    def state_id(self) -> str:
        value = {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "product_artifact_id": (
                self.product_ref.artifact_id if self.product_ref else None
            ),
            "product_version": self.product_ref.version if self.product_ref else None,
            "source_report_id": self.source_report_id,
            "candidate_count": self.candidate_count,
            "ready_candidate_count": self.ready_candidate_count,
            "empty_candidate_count": self.empty_candidate_count,
        }
        return hashlib.sha256(json.dumps(
            value, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()

    @property
    def is_hard_failure(self) -> bool:
        return self.status not in {
            KnowledgeOperationalStatus.NOT_CONFIGURED,
            KnowledgeOperationalStatus.EMPTY,
            KnowledgeOperationalStatus.READY,
        }


@dataclass(frozen=True, slots=True)
class ArtifactReadiness:
    """Fact supplied by a PIT-aware artifact adapter, never an expected publish time."""
    module: ModuleName
    trade_date: date
    available_at: datetime
    reference: ArtifactReference
    completed_daily: bool = True
    artifact_as_of_time: datetime | None = None
    mode: str | None = None
    strict_pit: bool = False
    market_event_time: datetime | None = None
    eligible_evidence_count: int | None = None

    def __post_init__(self):
        require_aware(self.available_at, "available_at")
        for name in ("artifact_as_of_time", "market_event_time"):
            value = getattr(self, name)
            if value is not None:
                require_aware(value, name)
        if self.mode not in {None, "research", "strict_live"}:
            raise ValueError("artifact mode is invalid")
        if self.eligible_evidence_count is not None and self.eligible_evidence_count < 0:
            raise ValueError("evidence count must be non-negative")


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    target_trade_date: date
    as_of_time: datetime
    run_type: RunType
    mode: str
    market_target_available: bool
    market_previous_available: bool
    market_basis_trade_date: date | None
    sector_basis_available: bool
    anomaly_basis_available: bool
    triage_basis_available: bool
    fund_flow_basis_available: bool
    fund_flow_next_available_at: datetime | None
    evidence_available: bool
    attribution_research_available: bool
    attribution_strict_available: bool
    synthesis_cache_available: bool
    eligible_evidence_count: int | None
    ready_artifacts: tuple[ArtifactReadiness, ...]
    future_modules: tuple[ModuleName, ...]
    basis_reason_code: str
    current_intraday_market_data: str = "UNSUPPORTED_IN_V1"
    knowledge_state: KnowledgeOperationalState | None = None


@dataclass(frozen=True, slots=True)
class ModulePlanEntry:
    module: ModuleName
    availability: ModuleAvailabilityStatus
    basis_trade_date: date | None
    reason_code: str
    dependencies: tuple[ModuleName, ...]
    soft_dependencies: tuple[ModuleName, ...]
    will_execute: bool


@dataclass(frozen=True, slots=True)
class ModuleExecutionPlan:
    entries: tuple[ModulePlanEntry, ...]
    policy_version: str = ORCHESTRATION_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class ModuleExecutionResult:
    module: ModuleName
    status: ModuleExecutionStatus
    reason_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    artifact_refs: tuple[ArtifactReference, ...] = ()
    safe_error_code: str | None = None

    def __post_init__(self):
        for name in ("started_at", "finished_at"):
            value = getattr(self, name)
            if value is not None:
                require_aware(value, name)
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("duration must be non-negative")


@dataclass(frozen=True, slots=True)
class RunSummary:
    planned_modules: int
    executed_modules: int
    passed_modules: int
    skipped_modules: int
    failed_modules: int
    real_llm_requests: int
    report_generated: bool
    report_id: str | None
    wall_clock_duration_ms: int | None
    report_reused: bool = False


@dataclass(frozen=True, slots=True)
class QuantOSRunManifest:
    schema_version: str
    run_context: RunContext
    plan: tuple[ModulePlanEntry, ...]
    execution_results: tuple[ModuleExecutionResult, ...]
    summary: RunSummary
    # Only fixed version/status metadata; no arbitrary provider payloads.
    provenance: tuple[tuple[str, str | None], ...]
    knowledge_state: KnowledgeOperationalState | None = None

    def __post_init__(self):
        if self.schema_version not in RUN_MANIFEST_SCHEMA_VERSIONS:
            raise ValueError("unsupported run manifest schema")
        if self.schema_version == RUN_SCHEMA_VERSION and self.knowledge_state is not None:
            raise ValueError("legacy manifest cannot contain Knowledge operational state")
        if (
            self.schema_version == KNOWLEDGE_RUN_SCHEMA_VERSION
            and self.knowledge_state is None
        ):
            raise ValueError("Knowledge-aware manifest requires operational state")
        if (
            self.run_context.run_type is RunType.INTRADAY
            and self.knowledge_state is not None
            and not self.knowledge_state.is_hard_failure
        ):
            raise ValueError("intraday Knowledge is unsupported in V1")

    @property
    def is_success(self) -> bool:
        return self.summary.failed_modules == 0 and not (
            self.knowledge_state is not None
            and self.knowledge_state.is_hard_failure
        )
