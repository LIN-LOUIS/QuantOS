"""Product views of an executed run, not new market or evidence algorithms."""

from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import Enum
import hashlib
from typing import Any, Mapping

from ._validation import require_aware
from .report import _reject_forbidden_fields
from .run import ArtifactReference
from .synthesis import KnowledgeBackgroundStatement

TIME_SLICE_SCHEMA_VERSION = "time-slice-intelligence-v1"
KNOWLEDGE_TIME_SLICE_SCHEMA_VERSION = "time-slice-intelligence-v2"
TIME_SLICE_SCHEMA_VERSIONS = frozenset({
    TIME_SLICE_SCHEMA_VERSION, KNOWLEDGE_TIME_SLICE_SCHEMA_VERSION,
})
TEMPORAL_POLICY_VERSION = "time-slice-windows-v1"


class ReportType(str, Enum):
    PRE_OPEN_BRIEF = "PRE_OPEN_BRIEF"
    INTRADAY_BRIEF = "INTRADAY_BRIEF"
    POST_CLOSE_REPORT = "POST_CLOSE_REPORT"


class TemporalBucket(str, Enum):
    PREVIOUS_ATTRIBUTION = "PREVIOUS_ATTRIBUTION"
    POST_EVENT_CONTEXT = "POST_EVENT_CONTEXT"
    OVERNIGHT_CONTEXT = "OVERNIGHT_CONTEXT"
    PRE_OPEN_CONTEXT = "PRE_OPEN_CONTEXT"
    SINCE_OPEN_CONTEXT = "SINCE_OPEN_CONTEXT"


class ProductAvailability(str, Enum):
    READY = "READY"
    UNAVAILABLE = "UNAVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class EvidenceView:
    evidence_id: str
    symbol: str
    provider: str
    temporal_bucket: TemporalBucket
    published_at: datetime | None
    available_at: datetime
    strict_pit: bool
    historical_proxy_used: bool

    def __post_init__(self):
        require_aware(self.available_at, "available_at")
        if self.published_at is not None:
            require_aware(self.published_at, "published_at")
        if not isinstance(self.temporal_bucket, TemporalBucket):
            raise ValueError("explicit temporal bucket required")
        if self.strict_pit and self.historical_proxy_used:
            raise ValueError("proxy cannot be strict")


@dataclass(frozen=True, slots=True)
class EvidenceWindowResult:
    views: tuple[EvidenceView, ...]
    bucket_counts: Mapping[str, int]
    providers: tuple[Mapping[str, Any], ...]

    def __post_init__(self):
        expected = {bucket.value: len({(x.provider, x.evidence_id) for x in self.views if x.temporal_bucket == bucket})
                    for bucket in TemporalBucket}
        if self.bucket_counts != expected:
            raise ValueError("evidence bucket counts disagree with views")


@dataclass(frozen=True, slots=True)
class WatchlistEntry:
    rank: int
    symbol: str
    name: str
    previous_tier: int
    previous_signal_types: tuple[str, ...]
    previous_market_facts: Mapping[str, Any]
    overnight_evidence_count: int
    pre_open_evidence_count: int
    watch_reason_codes: tuple[str, ...]

    def __post_init__(self):
        allowed = {"PREVIOUS_PRICE_ANOMALY", "PREVIOUS_VOLUME_ANOMALY", "PREVIOUS_AMOUNT_ANOMALY",
                   "OVERNIGHT_EVENT_PRESENT", "PRE_OPEN_EVENT_PRESENT"}
        if self.rank < 1 or not 1 <= self.previous_tier <= 5:
            raise ValueError("invalid previous candidate rank/tier")
        if min(self.overnight_evidence_count, self.pre_open_evidence_count) < 0:
            raise ValueError("negative context count")
        if not set(self.watch_reason_codes) <= allowed:
            raise ValueError("invalid watch reason")


@dataclass(frozen=True, slots=True)
class PreviousKnowledgeBrief:
    rank: int
    symbol: str
    synthesis_input_bundle_id: str
    synthesis_cache_identity: str | None
    historical_knowledge_background: tuple[KnowledgeBackgroundStatement, ...]
    retrospective_research_context: tuple[KnowledgeBackgroundStatement, ...]
    supplied_context_id: str
    supplied_knowledge_refs: tuple[str, ...]

    def __post_init__(self):
        if self.rank < 1 or not self.symbol:
            raise ValueError("invalid previous Knowledge candidate")
        for digest, name in (
            (self.synthesis_input_bundle_id, "synthesis input identity"),
            (self.supplied_context_id, "supplied context identity"),
        ):
            if len(digest) != 64:
                raise ValueError(f"invalid {name}")
            int(digest, 16)
        if self.synthesis_cache_identity is None or len(
            self.synthesis_cache_identity
        ) != 64:
            raise ValueError("invalid synthesis cache identity")
        int(self.synthesis_cache_identity, 16)
        for values, name in (
            (self.historical_knowledge_background, "historical Knowledge background"),
            (self.retrospective_research_context, "retrospective research context"),
        ):
            if type(values) is not tuple or any(
                not isinstance(item, KnowledgeBackgroundStatement) for item in values
            ):
                raise ValueError(f"invalid {name}")
        if type(self.supplied_knowledge_refs) is not tuple or len(
            self.supplied_knowledge_refs
        ) != len(set(self.supplied_knowledge_refs)) or any(
            not isinstance(ref, str) or not ref.startswith("K")
            or not ref[1:].isdigit() or int(ref[1:]) < 1
            for ref in self.supplied_knowledge_refs
        ):
            raise ValueError("invalid supplied Knowledge references")
        claimed = {
            ref
            for statement in (
                *self.historical_knowledge_background,
                *self.retrospective_research_context,
            )
            for ref in statement.knowledge_refs
        }
        if not claimed <= set(self.supplied_knowledge_refs):
            raise ValueError("previous Knowledge references were not supplied")


@dataclass(frozen=True, slots=True)
class PreOpenPayload:
    previous_market_overview: Mapping[str, Any] | None
    previous_anomaly_overview: Mapping[str, Any] | None
    watchlist: tuple[WatchlistEntry, ...]
    overnight_evidence_overview: EvidenceWindowResult
    availability_summary: Mapping[str, ProductAvailability]
    previous_knowledge_briefs: tuple[PreviousKnowledgeBrief, ...] = ()


@dataclass(frozen=True, slots=True)
class IntradayPayload:
    market_data_status: Mapping[str, Any]
    previous_market_context: Mapping[str, Any] | None
    watchlist: tuple[WatchlistEntry, ...]
    evidence_since_open: EvidenceWindowResult
    candidate_event_updates: tuple[Mapping[str, Any], ...]
    availability_summary: Mapping[str, ProductAvailability]


@dataclass(frozen=True, slots=True)
class PostClosePayload:
    daily_intelligence_report_ref: ArtifactReference | None
    availability_summary: Mapping[str, ProductAvailability]
    run_manifest_ref: ArtifactReference


@dataclass(frozen=True, slots=True)
class TimeSliceIntelligenceReport:
    schema_version: str
    report_type: ReportType
    target_trade_date: date
    market_basis_trade_date: date | None
    as_of_time: datetime
    generated_at: datetime
    mode: str
    universe_name: str
    run_context_ref: ArtifactReference
    payload: PreOpenPayload | IntradayPayload | PostClosePayload
    data_quality: Mapping[str, Any]
    provenance: Mapping[str, Any]

    def __post_init__(self):
        require_aware(self.as_of_time, "as_of_time")
        require_aware(self.generated_at, "generated_at")
        if self.schema_version not in TIME_SLICE_SCHEMA_VERSIONS or self.mode not in {"research", "strict_live"}:
            raise ValueError("invalid time-slice schema or mode")
        expected = {ReportType.PRE_OPEN_BRIEF: PreOpenPayload, ReportType.INTRADAY_BRIEF: IntradayPayload,
                    ReportType.POST_CLOSE_REPORT: PostClosePayload}
        if not isinstance(self.report_type, ReportType) or type(self.payload) is not expected[self.report_type]:
            raise ValueError("report type and payload disagree")
        if isinstance(self.payload, PostClosePayload) and self.payload.run_manifest_ref != self.run_context_ref:
            raise ValueError("run manifest identity references disagree")
        if self.market_basis_trade_date is not None:
            if self.market_basis_trade_date > self.target_trade_date:
                raise ValueError("future market basis")
            if self.report_type != ReportType.POST_CLOSE_REPORT and self.market_basis_trade_date >= self.target_trade_date:
                raise ValueError("background must precede target date")
        if self.mode == "strict_live" and self.data_quality["historical_proxy_used"]:
            raise ValueError("strict report cannot use proxy")
        if set(self.payload.availability_summary) != {
            "market_context", "sector_context", "fund_flow", "evidence", "attribution", "synthesis",
        } or any(not isinstance(x, ProductAvailability) for x in self.payload.availability_summary.values()):
            raise ValueError("invalid availability summary")
        if isinstance(self.payload, IntradayPayload) and self.payload.market_data_status != {
            "canonical_intraday_supported": False, "status": "UNSUPPORTED_IN_V1",
        }:
            raise ValueError("canonical intraday data is unsupported")
        if isinstance(self.payload, PreOpenPayload):
            if type(self.payload.previous_knowledge_briefs) is not tuple or any(
                not isinstance(item, PreviousKnowledgeBrief)
                for item in self.payload.previous_knowledge_briefs
            ):
                raise ValueError("invalid previous Knowledge product surface")
            if self.schema_version == TIME_SLICE_SCHEMA_VERSION and self.payload.previous_knowledge_briefs:
                raise ValueError("legacy time-slice schema cannot contain Knowledge product fields")
            if self.schema_version == KNOWLEDGE_TIME_SLICE_SCHEMA_VERSION and not self.payload.previous_knowledge_briefs:
                raise ValueError("Knowledge time-slice schema requires previous Knowledge context")
            if self.mode == "strict_live" and any(
                item.retrospective_research_context
                for item in self.payload.previous_knowledge_briefs
            ):
                raise ValueError("strict pre-open product cannot contain retrospective research context")
        elif self.schema_version == KNOWLEDGE_TIME_SLICE_SCHEMA_VERSION:
            raise ValueError("Knowledge time-slice schema is only used for pre-open reuse")
        if not isinstance(self.payload, PostClosePayload):
            ranks = [x.rank for x in self.payload.watchlist]
            if any(a >= b for a, b in zip(ranks, ranks[1:])):
                raise ValueError("watchlist must retain previous rank order")
            window = (self.payload.overnight_evidence_overview if isinstance(self.payload, PreOpenPayload)
                      else self.payload.evidence_since_open)
            allowed_buckets = ({TemporalBucket.OVERNIGHT_CONTEXT, TemporalBucket.PRE_OPEN_CONTEXT}
                               if isinstance(self.payload, PreOpenPayload) else {TemporalBucket.SINCE_OPEN_CONTEXT})
            if any(x.temporal_bucket not in allowed_buckets or x.available_at > self.as_of_time for x in window.views):
                raise ValueError("invalid evidence window")
            if self.mode == "strict_live" and any(not x.strict_pit or x.historical_proxy_used for x in window.views):
                raise ValueError("strict view cannot use proxy")
            _reject_realtime_fields(asdict(self.payload))
        _reject_forbidden_fields(self)

    @property
    def report_id(self):
        from quantos.serialization import canonical_identity_json_bytes

        value = asdict(self)
        value.pop("generated_at")
        if self.schema_version == TIME_SLICE_SCHEMA_VERSION and isinstance(
            self.payload, PreOpenPayload
        ):
            value["payload"].pop("previous_knowledge_briefs", None)
        # A new manifest generation must not itself change the product identity.
        value["run_context_ref"] = self.run_context_ref.artifact_id
        if isinstance(self.payload, PostClosePayload):
            value["payload"]["run_manifest_ref"] = self.run_context_ref.artifact_id
            daily_ref = self.payload.daily_intelligence_report_ref
            value["payload"]["daily_intelligence_report_ref"] = (
                None if daily_ref is None else {
                    "artifact_id": daily_ref.artifact_id,
                    "version": daily_ref.version,
                }
            )
        return hashlib.sha256(canonical_identity_json_bytes(value)).hexdigest()


def _reject_realtime_fields(value):
    forbidden = {"today_market_overview", "current_price", "current_return", "current_volume",
                 "intraday_anomaly", "intraday_zscore", "current_sector_return", "intraday_sector_return"}
    if isinstance(value, Mapping):
        if forbidden.intersection(value):
            raise ValueError("current market data is unsupported")
        for item in value.values():
            _reject_realtime_fields(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _reject_realtime_fields(item)
