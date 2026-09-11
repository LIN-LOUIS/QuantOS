"""Provider-neutral orchestration schemas for multi-source evidence discovery."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from ._validation import require_aware, require_non_empty
from .candidates import AnomalyCandidate
from .moneyflow import FundFlowEvidence
from .news import AnnouncementRecord

_SYMBOL = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_TIMESTAMP_BASES = {"publisher_reported", "provider_reported", "collected_only", "unknown"}


@dataclass(frozen=True, slots=True)
class EvidenceDiscoveryRequest:
    target_trade_date: date
    market_event_time: datetime
    as_of_time: datetime
    symbols: tuple[str, ...]
    company_names: tuple[str, ...]
    company_aliases: tuple[tuple[str, ...], ...] = ()
    before_hours: int = 24
    after_hours: int = 12
    top_n: int = 20
    mode: str = "proxy_research"
    max_results_per_candidate: int = 50
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        require_aware(self.market_event_time, "market_event_time")
        require_aware(self.as_of_time, "as_of_time")
        if self.mode not in {"strict", "proxy_research"}:
            raise ValueError("discovery mode is invalid")
        if len(self.symbols) != len(self.company_names):
            raise ValueError("symbols and company_names must align")
        if self.company_aliases and len(self.company_aliases) != len(self.symbols):
            raise ValueError("company_aliases and symbols must align")
        if not self.symbols or len(self.symbols) > self.top_n:
            raise ValueError("symbols must be non-empty and within top_n")
        if any(not _SYMBOL.fullmatch(symbol) for symbol in self.symbols):
            raise ValueError("symbols must be canonical")
        if any(not name.strip() for name in self.company_names):
            raise ValueError("company_names cannot be empty")
        if min(self.before_hours, self.after_hours) < 0:
            raise ValueError("discovery window cannot be negative")
        if self.top_n < 1 or not 1 <= self.max_results_per_candidate <= 50:
            raise ValueError("discovery result limits are invalid")


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    result_id: str
    query: str
    query_symbol: str
    title: str
    snippet: str
    url: str
    domain: str
    published_at: datetime | None
    timestamp_basis: str
    collected_at: datetime
    available_at: datetime
    provider: str
    provider_record_id: str
    provider_score: float | None = None
    fetch_status: str = "metadata_only"
    evidence_basis: str = "search_result_metadata"
    pit_mode: str = "source_timestamp_proxy"
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        for name in ("result_id", "query", "query_symbol", "title", "url", "domain", "provider", "provider_record_id"):
            require_non_empty(getattr(self, name), name)
        if not _SYMBOL.fullmatch(self.query_symbol):
            raise ValueError("query_symbol must be canonical")
        if self.timestamp_basis not in _TIMESTAMP_BASES:
            raise ValueError("timestamp_basis is invalid")
        if self.fetch_status not in {"metadata_only", "not_attempted", "blocked"}:
            raise ValueError("fetch_status is invalid")
        if self.evidence_basis != "search_result_metadata":
            raise ValueError("WebSearchResult evidence_basis is invalid")
        if self.pit_mode not in {"strict_live", "source_timestamp_proxy"}:
            raise ValueError("pit_mode is invalid")
        require_aware(self.collected_at, "collected_at")
        require_aware(self.available_at, "available_at")
        if self.published_at is not None:
            require_aware(self.published_at, "published_at")
        if self.available_at < self.collected_at:
            raise ValueError("available_at cannot precede collected_at")
        if self.published_at is not None and self.available_at < self.published_at:
            raise ValueError("available_at cannot precede published_at")

    @property
    def strict_pit(self) -> bool:
        return self.pit_mode == "strict_live"


@dataclass(frozen=True, slots=True)
class DiscoveryMention:
    evidence_id: str
    symbol: str
    provider: str
    query_term: str
    match_method: str
    matched_term: str
    evidence_tier: int
    discovered_at: datetime
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        for name in ("evidence_id", "provider", "query_term", "match_method", "matched_term"):
            require_non_empty(getattr(self, name), name)
        if not _SYMBOL.fullmatch(self.symbol):
            raise ValueError("symbol must be canonical")
        if self.evidence_tier not in {1, 2, 3}:
            raise ValueError("evidence_tier is invalid")
        require_aware(self.discovered_at, "discovered_at")


@dataclass(frozen=True, slots=True)
class ProviderDiscoveryResult:
    provider_name: str
    status: str
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    requested_candidates: int
    successful_candidates: int
    failed_candidates: tuple[str, ...]
    raw_result_count: int
    normalized_record_count: int
    mention_count: int
    request_count: int = 0
    records: tuple[Any, ...] = ()
    mentions: tuple[DiscoveryMention, ...] = ()
    safe_error_type: str | None = None
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        require_non_empty(self.provider_name, "provider_name")
        if self.status not in {"pass", "partial", "unavailable", "failed"}:
            raise ValueError("provider discovery status is invalid")
        require_aware(self.started_at, "started_at")
        require_aware(self.completed_at, "completed_at")
        if self.completed_at < self.started_at or self.duration_ms < 0:
            raise ValueError("provider discovery timing is invalid")
        counts = (self.requested_candidates, self.successful_candidates, self.raw_result_count, self.normalized_record_count, self.mention_count, self.request_count)
        if any(value < 0 for value in counts):
            raise ValueError("provider discovery counts cannot be negative")


@dataclass(frozen=True, slots=True)
class CanonicalDiscoveredEvidence:
    evidence_id: str
    evidence_type: str
    title: str
    url: str | None
    domain: str | None
    published_at: datetime | None
    timestamp_basis: str
    evidence_basis: str
    pit_mode: str
    strict_pit: bool
    discovered_by: tuple[str, ...]
    discovery_queries: tuple[str, ...]
    source_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateCoverage:
    symbol: str
    has_official_announcement: bool
    has_gdelt_news: bool
    has_web_search: bool
    total_evidence_count: int


@dataclass(frozen=True, slots=True)
class ProviderCoverage:
    provider_name: str
    provider_status: str
    candidates_with_evidence: int
    evidence_count: int


@dataclass(frozen=True, slots=True)
class EvidenceCoverageReport:
    candidate_count: int
    providers: tuple[ProviderCoverage, ...]
    candidates: tuple[CandidateCoverage, ...]


@dataclass(frozen=True, slots=True)
class ProviderBenchmarkReport:
    provider_name: str
    requested_candidates: int
    successful_candidates: int
    raw_results: int
    deduped_results: int
    linked_mentions: int
    candidates_with_evidence: int
    coverage_ratio: float
    title_exact_matches: int
    title_alias_matches: int
    title_symbol_matches: int
    snippet_company_matches: int
    snippet_alias_matches: int
    query_only_matches: int
    unlinked_result_count: int
    unknown_timestamp_count: int
    no_url_count: int
    duplicate_with_other_provider_count: int
    median_results_per_candidate: float
    request_count: int
    duration_ms: int


@dataclass(frozen=True, slots=True)
class EvidenceQualityFlags:
    evidence_id: str
    symbol: str
    provider: str
    evidence_tier: int
    match_method: str
    entity_evidence_basis: str
    has_title_entity_match: bool
    has_snippet_entity_match: bool
    query_only_match: bool
    timestamp_quality: str
    time_delta_seconds: int | None
    inside_event_window: bool | None
    has_canonical_url: bool
    cross_provider_confirmed: bool
    eligible_for_event_evidence: bool
    eligibility_reason: str


@dataclass(frozen=True, slots=True)
class ProviderQualityMetrics:
    provider_name: str
    discovery_count: int
    event_eligible_count: int
    title_exact_ratio: float
    snippet_exact_ratio: float
    query_only_ratio: float
    known_timestamp_ratio: float
    inside_window_ratio: float
    event_eligible_ratio: float


@dataclass(frozen=True, slots=True)
class CrossProviderDedupAudit:
    exact_url_shared: int
    normalized_url_shared: int
    title_domain_shared: int
    title_only_shared: int
    redirect_like_url_count: int


@dataclass(frozen=True, slots=True)
class AttributionEvidenceFact:
    evidence_id: str
    symbol: str
    provider: str
    evidence_tier: int
    match_method: str
    entity_strength: str
    source_temporal_relation: str
    knowledge_temporal_relation: str
    time_delta_seconds: int | None
    research_attribution_eligible: bool
    strict_attribution_eligible: bool
    attribution_reason: str

    @property
    def temporal_relation(self) -> str:
        """Backward-compatible alias; new callers should use source_temporal_relation."""
        return self.source_temporal_relation

    @property
    def attribution_eligible(self) -> bool:
        """Backward-compatible alias for research eligibility only."""
        return self.research_attribution_eligible


@dataclass(frozen=True, slots=True)
class AttributionEvidenceBundle:
    candidate: AnomalyCandidate
    fund_flow_evidence: FundFlowEvidence | None
    research_attribution_evidence: tuple[WebSearchResult, ...]
    strict_attribution_evidence: tuple[WebSearchResult, ...]
    post_event_context: tuple[WebSearchResult, ...]

    @property
    def attribution_evidence(self) -> tuple[WebSearchResult, ...]:
        """Backward-compatible research view; strict consumers must choose explicitly."""
        return self.research_attribution_evidence


@dataclass(frozen=True, slots=True)
class MultiSourceCandidateEvidenceBundle:
    candidate: AnomalyCandidate
    fund_flow_evidence: FundFlowEvidence | None
    announcement_evidence: tuple[AnnouncementRecord, ...]
    news_evidence: tuple[CanonicalDiscoveredEvidence, ...]
    search_evidence: tuple[WebSearchResult, ...]
