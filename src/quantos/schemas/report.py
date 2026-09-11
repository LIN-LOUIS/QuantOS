"""Canonical, immutable Daily Intelligence Report V1 schemas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from ._validation import require_aware, require_non_empty
from .synthesis import KnowledgeBackgroundStatement

REPORT_SCHEMA_VERSION = "daily-intelligence-v1"
KNOWLEDGE_REPORT_SCHEMA_VERSION = "daily-intelligence-v2"
REPORT_SCHEMA_VERSIONS = frozenset({
    REPORT_SCHEMA_VERSION, KNOWLEDGE_REPORT_SCHEMA_VERSION,
})
REPORT_MODES = frozenset({"research", "strict_live"})
SYNTHESIS_STATUSES = frozenset({
    "CACHE_HIT", "GENERATED", "NO_EVIDENCE_FAST_PATH", "FAIL",
})
FORBIDDEN_REPORT_FIELDS = frozenset({
    "prediction", "expected_return", "target_price", "confidence_score",
    "sentiment_score", "buy_sell_hold", "investment_advice", "portfolio_weight",
})


@dataclass(frozen=True, slots=True)
class EvidenceStats:
    discovery_count: int
    event_evidence_count: int
    research_attribution_count: int
    strict_attribution_count: int
    post_event_context_count: int
    eligible_attribution_count: int

    def __post_init__(self) -> None:
        if min(
            self.discovery_count, self.event_evidence_count,
            self.research_attribution_count, self.strict_attribution_count,
            self.post_event_context_count, self.eligible_attribution_count,
        ) < 0:
            raise ValueError("evidence counts must be non-negative")


@dataclass(frozen=True, slots=True)
class SynthesisBrief:
    status: str
    model_provider: str | None
    model_name: str | None
    input_bundle_id: str | None
    cache_identity: str | None
    insufficient_evidence: bool | None
    used_evidence_ids: tuple[str, ...]
    possible_explanations: tuple[Mapping[str, Any], ...]
    contradicting_signals: tuple[str, ...]
    post_event_notes: tuple[str, ...]
    limitations: tuple[str, ...]
    error_category: str | None
    validation_error_code: str | None
    historical_knowledge_background: tuple[KnowledgeBackgroundStatement, ...] = ()
    retrospective_research_context: tuple[KnowledgeBackgroundStatement, ...] = ()
    supplied_context_id: str | None = None
    supplied_knowledge_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in SYNTHESIS_STATUSES:
            raise ValueError("synthesis status is invalid")
        if self.status == "FAIL" and any((
            self.used_evidence_ids, self.possible_explanations,
            self.contradicting_signals, self.post_event_notes, self.limitations,
            self.historical_knowledge_background,
            self.retrospective_research_context,
        )):
            raise ValueError("failed synthesis cannot retain model prose")
        for values, name in (
            (self.historical_knowledge_background, "historical knowledge background"),
            (self.retrospective_research_context, "retrospective research context"),
        ):
            if type(values) is not tuple or any(
                not isinstance(item, KnowledgeBackgroundStatement) for item in values
            ):
                raise ValueError(f"invalid {name}")
        if type(self.supplied_knowledge_refs) is not tuple or len(
            self.supplied_knowledge_refs
        ) != len(set(self.supplied_knowledge_refs)):
            raise ValueError("supplied Knowledge references must be unique")
        if self.supplied_context_id is None:
            if (self.supplied_knowledge_refs or self.historical_knowledge_background
                    or self.retrospective_research_context):
                raise ValueError("Knowledge product surface requires a supplied context")
        else:
            if len(self.supplied_context_id) != 64:
                raise ValueError("supplied_context_id must be a SHA-256 digest")
            int(self.supplied_context_id, 16)
            if self.input_bundle_id is None or len(self.input_bundle_id) != 64:
                raise ValueError("Knowledge product requires a synthesis input identity")
            int(self.input_bundle_id, 16)
            if self.cache_identity is None:
                raise ValueError("Knowledge product requires a synthesis cache identity")
        if self.cache_identity is not None:
            if len(self.cache_identity) != 64:
                raise ValueError("cache_identity must be a SHA-256 digest")
            int(self.cache_identity, 16)
        if any(
            not isinstance(reference, str) or not reference.startswith("K")
            or not reference[1:].isdigit() or int(reference[1:]) < 1
            for reference in self.supplied_knowledge_refs
        ):
            raise ValueError("invalid Knowledge reference namespace")
        claimed = {
            reference
            for statement in (
                *self.historical_knowledge_background,
                *self.retrospective_research_context,
            )
            for reference in statement.knowledge_refs
        }
        if not claimed <= set(self.supplied_knowledge_refs):
            raise ValueError("product Knowledge references were not supplied")


@dataclass(frozen=True, slots=True)
class CandidateBrief:
    rank: int
    symbol: str
    name: str
    tier: int
    signal_types: tuple[str, ...]
    market_facts: Mapping[str, Any]
    sector_context: Mapping[str, Any] | None
    fund_flow: Mapping[str, Any]
    evidence_stats: EvidenceStats
    synthesis: SynthesisBrief

    def __post_init__(self) -> None:
        if self.rank < 1 or not 1 <= self.tier <= 5:
            raise ValueError("candidate rank and tier must be valid")
        require_non_empty(self.symbol, "symbol")
        require_non_empty(self.name, "name")


@dataclass(frozen=True, slots=True)
class DailyIntelligenceReport:
    schema_version: str
    report_id: str
    trade_date: date
    as_of_time: datetime
    generated_at: datetime
    mode: str
    universe_name: str
    market_overview: Mapping[str, Any]
    sector_overview: Mapping[str, Any]
    anomaly_overview: Mapping[str, Any]
    candidate_summary: Mapping[str, Any]
    candidate_briefs: tuple[CandidateBrief, ...]
    evidence_overview: Mapping[str, Any]
    data_quality: Mapping[str, Any]
    synthesis_runtime: Mapping[str, Any]
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.schema_version not in REPORT_SCHEMA_VERSIONS:
            raise ValueError("report schema version is invalid")
        if len(self.report_id) != 64:
            raise ValueError("report_id must be a SHA-256 hex digest")
        int(self.report_id, 16)
        if self.mode not in REPORT_MODES:
            raise ValueError("report mode is invalid")
        require_aware(self.as_of_time, "as_of_time")
        require_aware(self.generated_at, "generated_at")
        require_non_empty(self.universe_name, "universe_name")
        if any(item.rank > int(self.candidate_summary["requested_top_n"])
               for item in self.candidate_briefs):
            raise ValueError("candidate brief is outside requested top_n")
        if tuple(item.rank for item in self.candidate_briefs) != tuple(
            range(1, len(self.candidate_briefs) + 1)
        ):
            raise ValueError("candidate briefs must preserve contiguous rank order")
        if self.mode == "strict_live" and self.data_quality["evidence"]["historical_proxy_used"]:
            raise ValueError("strict_live report cannot use historical proxy evidence")
        if self.mode == "strict_live" and any(
            item.synthesis.retrospective_research_context
            for item in self.candidate_briefs
        ):
            raise ValueError("strict_live report cannot contain retrospective research context")
        has_knowledge = any(
            item.synthesis.supplied_context_id is not None
            for item in self.candidate_briefs
        )
        if self.schema_version == REPORT_SCHEMA_VERSION and has_knowledge:
            raise ValueError("legacy report schema cannot contain Knowledge product fields")
        if self.schema_version == KNOWLEDGE_REPORT_SCHEMA_VERSION and (
            not self.candidate_briefs
            or any(
                item.synthesis.supplied_context_id is None
                for item in self.candidate_briefs
            )
        ):
            raise ValueError(
                "Knowledge report schema requires a supplied context per candidate"
            )
        _reject_forbidden_fields(self)


def _reject_forbidden_fields(value: Any) -> None:
    from dataclasses import fields, is_dataclass

    if is_dataclass(value):
        for field in fields(value):
            if field.name in FORBIDDEN_REPORT_FIELDS:
                raise ValueError("forbidden report field")
            _reject_forbidden_fields(getattr(value, field.name))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if key in FORBIDDEN_REPORT_FIELDS:
                raise ValueError("forbidden report field")
            _reject_forbidden_fields(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _reject_forbidden_fields(item)
