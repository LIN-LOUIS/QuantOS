"""Canonical non-causal news and announcement evidence schemas."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from ._validation import require_aware, require_non_empty
from .candidates import AnomalyCandidate
from .moneyflow import FundFlowEvidence

_SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_PIT_MODES = {"strict_live", "source_timestamp_proxy"}
_MATCH_METHODS = {
    "announcement_symbol",
    "title_company_name",
    "title_symbol",
    "body_company_name",
}


@dataclass(frozen=True, slots=True)
class NewsRecord:
    news_id: str
    source: str
    source_type: str
    published_at: datetime
    collected_at: datetime
    available_at: datetime
    title: str
    content: str | None
    url: str | None
    channels: tuple[str, ...]
    provider: str
    provider_record_id: str
    pit_mode: str
    domain: str | None = None
    language: str | None = None
    timestamp_basis: str = "publisher_reported"
    evidence_basis: str = "publisher_article"
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        for name in ("news_id", "source", "title", "provider", "provider_record_id"):
            require_non_empty(getattr(self, name), name)
        if self.source_type != "news":
            raise ValueError("NewsRecord source_type must be news")
        if self.pit_mode not in _PIT_MODES:
            raise ValueError("pit_mode is invalid")
        if self.timestamp_basis not in {
            "publisher_reported", "provider_reported", "collected_only", "unknown"
        }:
            raise ValueError("timestamp_basis is invalid")
        if self.evidence_basis not in {
            "publisher_article", "structured_news_metadata"
        }:
            raise ValueError("evidence_basis is invalid")
        for name in ("published_at", "collected_at", "available_at"):
            require_aware(getattr(self, name), name)
        if self.available_at < max(self.published_at, self.collected_at):
            raise ValueError("available_at must not precede publication or collection")

    @property
    def strict_pit(self) -> bool:
        return self.pit_mode == "strict_live"


@dataclass(frozen=True, slots=True)
class AnnouncementRecord:
    announcement_id: str
    symbol: str
    company_name: str
    title: str
    url: str | None
    published_at: datetime
    collected_at: datetime
    available_at: datetime
    source: str
    provider: str
    provider_record_id: str
    pit_mode: str
    timestamp_basis: str = "publisher_reported"
    evidence_basis: str = "official_announcement"
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        if not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol must use a canonical SH/SZ symbol")
        for name in ("announcement_id", "company_name", "title", "source", "provider", "provider_record_id"):
            require_non_empty(getattr(self, name), name)
        if self.pit_mode not in _PIT_MODES:
            raise ValueError("pit_mode is invalid")
        if self.timestamp_basis != "publisher_reported":
            raise ValueError("announcement timestamp_basis must be publisher_reported")
        if self.evidence_basis != "official_announcement":
            raise ValueError("announcement evidence_basis must be official_announcement")
        for name in ("published_at", "collected_at", "available_at"):
            require_aware(getattr(self, name), name)
        if self.available_at < max(self.published_at, self.collected_at):
            raise ValueError("available_at must not precede publication or collection")

    @property
    def strict_pit(self) -> bool:
        return self.pit_mode == "strict_live"


@dataclass(frozen=True, slots=True)
class NewsMention:
    news_id: str
    symbol: str
    match_method: str
    matched_term: str
    title_match: bool
    body_match: bool
    direct_symbol_link: bool
    evidence_tier: int
    published_at: datetime
    available_at: datetime
    event_tags: tuple[str, ...] = ()
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        require_non_empty(self.news_id, "news_id")
        require_non_empty(self.matched_term, "matched_term")
        if not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol must use a canonical SH/SZ symbol")
        if self.match_method not in _MATCH_METHODS:
            raise ValueError("match_method is invalid")
        if self.evidence_tier not in {1, 2, 3}:
            raise ValueError("evidence_tier must be 1, 2, or 3")
        require_aware(self.published_at, "published_at")
        require_aware(self.available_at, "available_at")


@dataclass(frozen=True, slots=True)
class CandidateNewsEvidence:
    candidate: AnomalyCandidate
    mention: NewsMention
    record: NewsRecord | AnnouncementRecord

    def __post_init__(self) -> None:
        if self.candidate.symbol != self.mention.symbol:
            raise ValueError("candidate and mention symbols must match")
        record_id = self.record.news_id if isinstance(self.record, NewsRecord) else self.record.announcement_id
        if record_id != self.mention.news_id:
            raise ValueError("mention and record identities must match")


@dataclass(frozen=True, slots=True)
class CandidateEvidenceBundle:
    candidate: AnomalyCandidate
    fund_flow_evidence: FundFlowEvidence | None
    news_evidence: tuple[CandidateNewsEvidence, ...]

    def __post_init__(self) -> None:
        if self.fund_flow_evidence is not None and self.fund_flow_evidence.symbol != self.candidate.symbol:
            raise ValueError("fund flow evidence symbol must match candidate")
        if any(item.candidate != self.candidate for item in self.news_evidence):
            raise ValueError("news evidence candidates must match bundle candidate")
