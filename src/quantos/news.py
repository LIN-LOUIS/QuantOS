"""Deterministic news identity, entity linking, ranking, and enrichment."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, time, timedelta
from typing import Sequence

from quantos.schemas import (
    AnnouncementRecord,
    AnomalyCandidate,
    CandidateEvidenceBundle,
    CandidateNewsEvidence,
    FundFlowEvidence,
    NewsMention,
    NewsRecord,
    SecurityMaster,
)
from quantos.schemas._validation import require_aware

_COMPANY_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司")
_EVENT_KEYWORDS = (
    ("业绩", "业绩"), ("预告", "预告"), ("回购", "回购"),
    ("增持", "增持"), ("减持", "减持"), ("重大合同", "重大合同"),
    ("中标", "中标"), ("重组", "重组"), ("停牌", "停牌"),
    ("复牌", "复牌"), ("监管", "监管"), ("诉讼", "诉讼"),
    ("处罚", "处罚"), ("融资", "融资"), ("股权激励", "股权激励"),
)


def stable_news_id(source: str, published_at: datetime, title: str, content: str | None) -> str:
    require_aware(published_at, "published_at")
    value = "|".join((
        _normalize_text(source).lower(),
        published_at.isoformat(),
        _normalize_text(title),
        _normalize_text(content or ""),
    ))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_announcement_id(
    provider: str,
    symbol: str,
    published_at: datetime,
    title: str,
    document_url: str | None,
) -> str:
    require_aware(published_at, "published_at")
    value = "|".join((
        _normalize_text(provider).lower(), symbol, published_at.isoformat(),
        _normalize_text(title), _normalize_text(document_url or ""),
    ))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def announcement_timing_label(published_at: datetime) -> str:
    require_aware(published_at, "published_at")
    local_time = published_at.timetz().replace(tzinfo=None)
    if local_time < time(9, 30):
        return "pre_market"
    if local_time <= time(15, 0):
        return "intraday"
    return "post_market"


def normalize_company_alias(company_name: str) -> str:
    value = _normalize_text(company_name)
    for suffix in _COMPANY_SUFFIXES:
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def tag_news_events(title: str, content: str = "") -> tuple[str, ...]:
    text = f"{title}\n{content}"
    return tuple(tag for keyword, tag in _EVENT_KEYWORDS if keyword in text)


def link_news_entities(
    news: Sequence[NewsRecord],
    announcements: Sequence[AnnouncementRecord],
    securities: Sequence[SecurityMaster],
    *,
    as_of_time: datetime,
) -> list[NewsMention]:
    require_aware(as_of_time, "as_of_time")
    visible_securities = _visible_securities(securities, as_of_time)
    mentions: list[NewsMention] = []
    for item in announcements:
        if item.available_at <= as_of_time:
            mentions.append(NewsMention(
                news_id=item.announcement_id, symbol=item.symbol,
                match_method="announcement_symbol", matched_term=item.symbol,
                title_match=False, body_match=False, direct_symbol_link=True,
                evidence_tier=1, published_at=item.published_at,
                available_at=item.available_at,
                event_tags=tag_news_events(item.title),
            ))
    for item in news:
        if item.available_at > as_of_time:
            continue
        for symbol, security in visible_securities.items():
            method, term = _match_news(item, security)
            if method is None or term is None:
                continue
            mentions.append(NewsMention(
                news_id=item.news_id, symbol=symbol, match_method=method,
                matched_term=term,
                title_match=method.startswith("title_"),
                body_match=method == "body_company_name",
                direct_symbol_link=False,
                evidence_tier=2 if method.startswith("title_") else 3,
                published_at=item.published_at, available_at=item.available_at,
                event_tags=tag_news_events(item.title, item.content),
            ))
    return sorted(mentions, key=lambda item: (item.news_id, item.symbol, item.evidence_tier, item.match_method))


def rank_news_mentions(
    mentions: Sequence[NewsMention], *, market_event_time: datetime
) -> list[NewsMention]:
    require_aware(market_event_time, "market_event_time")
    return sorted(mentions, key=lambda item: (
        item.evidence_tier,
        abs(item.published_at - market_event_time),
        -item.published_at.timestamp(),
        item.news_id,
    ))


def enrich_candidates_with_news(
    candidates: Sequence[AnomalyCandidate],
    mentions: Sequence[NewsMention],
    news: Sequence[NewsRecord],
    announcements: Sequence[AnnouncementRecord],
    *,
    market_event_time: datetime,
    as_of_time: datetime,
    before_hours: int = 24,
    after_hours: int = 12,
) -> dict[str, tuple[CandidateNewsEvidence, ...]]:
    require_aware(market_event_time, "market_event_time")
    require_aware(as_of_time, "as_of_time")
    if before_hours < 0 or after_hours < 0:
        raise ValueError("news window hours must be non-negative")
    start = market_event_time - timedelta(hours=before_hours)
    end = market_event_time + timedelta(hours=after_hours)
    records: dict[str, NewsRecord | AnnouncementRecord] = {
        item.news_id: item for item in news if item.available_at <= as_of_time
    }
    records.update({
        item.announcement_id: item
        for item in announcements
        if item.available_at <= as_of_time
    })
    by_symbol: dict[str, list[CandidateNewsEvidence]] = {
        item.symbol: [] for item in candidates
        if item.available_at <= as_of_time and item.as_of_time <= as_of_time
    }
    candidates_by_symbol = {item.symbol: item for item in candidates if item.symbol in by_symbol}
    eligible = [
        item for item in mentions
        if item.symbol in by_symbol
        and item.available_at <= as_of_time
        and start <= item.published_at <= end
        and item.news_id in records
    ]
    for mention in rank_news_mentions(eligible, market_event_time=market_event_time):
        by_symbol[mention.symbol].append(CandidateNewsEvidence(
            candidate=candidates_by_symbol[mention.symbol],
            mention=mention,
            record=records[mention.news_id],
        ))
    return {symbol: tuple(items) for symbol, items in sorted(by_symbol.items())}


def enrich_candidates_with_announcements(
    candidates: Sequence[AnomalyCandidate],
    announcements: Sequence[AnnouncementRecord],
    *,
    market_event_time: datetime,
    as_of_time: datetime,
    before_hours: int = 24,
    after_hours: int = 12,
    max_per_candidate: int = 5,
) -> dict[str, tuple[CandidateNewsEvidence, ...]]:
    if max_per_candidate < 0:
        raise ValueError("max_per_candidate must be non-negative")
    mentions = link_news_entities([], announcements, [], as_of_time=as_of_time)
    enriched = enrich_candidates_with_news(
        candidates, mentions, [], announcements,
        market_event_time=market_event_time, as_of_time=as_of_time,
        before_hours=before_hours, after_hours=after_hours,
    )
    return {
        symbol: evidence[:max_per_candidate]
        for symbol, evidence in enriched.items()
    }


def build_candidate_evidence_bundle(
    candidate: AnomalyCandidate,
    *,
    fund_flow_evidence: FundFlowEvidence | None,
    news_evidence: Sequence[CandidateNewsEvidence],
) -> CandidateEvidenceBundle:
    return CandidateEvidenceBundle(candidate, fund_flow_evidence, tuple(news_evidence))


def _match_news(item: NewsRecord, security: SecurityMaster) -> tuple[str | None, str | None]:
    company = _normalize_text(security.company_name)
    alias = normalize_company_alias(company)
    title = _normalize_text(item.title)
    body = _normalize_text(item.content or "")
    if company and company in title:
        return "title_company_name", company
    if alias and len(alias) >= 2 and alias in title:
        return "title_company_name", alias
    bare_symbol = security.symbol[:6]
    if security.symbol in title or re.search(rf"(?<!\d){re.escape(bare_symbol)}(?!\d)", title):
        return "title_symbol", bare_symbol
    if company and company in body:
        return "body_company_name", company
    if alias and len(alias) >= 2 and alias in body:
        return "body_company_name", alias
    return None, None


def _visible_securities(
    securities: Sequence[SecurityMaster], as_of_time: datetime
) -> dict[str, SecurityMaster]:
    visible: dict[str, SecurityMaster] = {}
    for item in securities:
        if item.available_at > as_of_time:
            continue
        incumbent = visible.get(item.symbol)
        if incumbent is None or (item.available_at, item.effective_from, item.source) > (
            incumbent.available_at, incumbent.effective_from, incumbent.source
        ):
            visible[item.symbol] = item
    return visible


def _normalize_text(value: str) -> str:
    return "".join(str(value).split())
