"""Deterministic evidence-quality facts and event-evidence selection."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from quantos.collectors.web_search import normalize_web_url
from quantos.schemas import (
    AnomalyCandidate, AttributionEvidenceBundle, AttributionEvidenceFact,
    CrossProviderDedupAudit, DiscoveryMention, EvidenceQualityFlags,
    FundFlowEvidence, ProviderQualityMetrics, WebSearchResult,
)
from quantos.schemas._validation import require_aware

_TITLE_METHODS = {"title_company_name", "title_alias", "title_symbol"}
_SNIPPET_METHODS = {"snippet_company_name", "snippet_alias"}
_QUERY_METHOD = "provider_exact_company_query"


@dataclass(frozen=True, slots=True)
class EventEvidenceSelection:
    discovery_evidence: tuple[WebSearchResult, ...]
    quality_flags: tuple[EvidenceQualityFlags, ...]
    event_evidence: tuple[WebSearchResult, ...]
    provider_metrics: tuple[ProviderQualityMetrics, ...]
    cross_provider_audit: CrossProviderDedupAudit


@dataclass(frozen=True, slots=True)
class AttributionEvidenceSelection:
    event_selection: EventEvidenceSelection
    attribution_facts: tuple[AttributionEvidenceFact, ...]
    bundles: tuple[AttributionEvidenceBundle, ...]


def calibrate_event_evidence(
    records: Sequence[WebSearchResult], mentions: Sequence[DiscoveryMention], *,
    market_event_time: datetime, before_hours: int = 24, after_hours: int = 12,
) -> EventEvidenceSelection:
    require_aware(market_event_time, "market_event_time")
    if before_hours < 0 or after_hours < 0:
        raise ValueError("event window hours must be non-negative")
    discovery = tuple(sorted(records, key=lambda item: (item.provider, item.query_symbol, item.result_id)))
    mention_map = {(item.provider, item.symbol, item.evidence_id): item for item in mentions}
    shared_urls = _shared_normalized_urls(discovery)
    flags = []
    for item in discovery:
        mention = mention_map.get((item.provider, item.query_symbol, item.result_id))
        method = mention.match_method if mention else "unlinked"
        tier = mention.evidence_tier if mention else 3
        title_match = method in _TITLE_METHODS
        snippet_match = method in _SNIPPET_METHODS
        query_only = method == _QUERY_METHOD
        delta = int((item.published_at - market_event_time).total_seconds()) if item.published_at else None
        inside = (-before_hours * 3600 <= delta <= after_hours * 3600) if delta is not None else None
        if item.published_at is None:
            eligible, reason = False, "timestamp_unknown"
        elif not inside:
            eligible, reason = False, "outside_event_window"
        elif title_match:
            eligible, reason = True, "title_entity_in_window"
        elif snippet_match:
            eligible, reason = True, "snippet_entity_in_window"
        elif query_only:
            eligible, reason = True, "query_only_in_window"
        else:
            eligible, reason = False, "entity_unconfirmed"
        normalized = _safe_normalized_url(item.url)
        flags.append(EvidenceQualityFlags(
            evidence_id=item.result_id, symbol=item.query_symbol, provider=item.provider,
            evidence_tier=tier, match_method=method,
            entity_evidence_basis=("title_entity" if title_match else "snippet_entity" if snippet_match
                                   else "provider_query_only" if query_only else "unconfirmed"),
            has_title_entity_match=title_match, has_snippet_entity_match=snippet_match,
            query_only_match=query_only, timestamp_quality="known" if item.published_at else "unknown",
            time_delta_seconds=delta, inside_event_window=inside,
            has_canonical_url=normalized is not None,
            cross_provider_confirmed=bool(normalized and normalized in shared_urls),
            eligible_for_event_evidence=eligible, eligibility_reason=reason,
        ))
    flag_map = {(item.provider, item.symbol, item.evidence_id): item for item in flags}
    event = tuple(sorted(
        (item for item in discovery if flag_map[(item.provider, item.query_symbol, item.result_id)].eligible_for_event_evidence),
        key=lambda item: _event_sort_key(item, flag_map[(item.provider, item.query_symbol, item.result_id)]),
    ))
    return EventEvidenceSelection(
        discovery, tuple(flags), event, _metrics(flags), audit_cross_provider_dedup(discovery)
    )


def link_stored_query_a_results(records: Sequence[WebSearchResult]) -> tuple[DiscoveryMention, ...]:
    """Recreate deterministic Query-A mentions from canonical stored metadata."""
    symbols_by_query = {}
    for item in records:
        symbols_by_query.setdefault(_text(item.query), set()).add(item.query_symbol)
    mentions = []
    for item in records:
        title, snippet, query = _text(item.title), _text(item.snippet), _text(item.query)
        code = item.query_symbol.split(".", 1)[0]
        if query and query in title:
            method, term, tier = "title_company_name", item.query, 2
        elif code in title:
            method, term, tier = "title_symbol", code, 2
        elif query and query in snippet:
            method, term, tier = "snippet_company_name", item.query, 3
        elif len(symbols_by_query[query]) == 1:
            method, term, tier = "provider_exact_company_query", item.query, 3
        else:
            continue
        mentions.append(DiscoveryMention(
            item.result_id, item.query_symbol, item.provider, item.query,
            method, term, tier, item.collected_at,
        ))
    return tuple(sorted(mentions, key=lambda item: (item.provider, item.symbol, item.evidence_id)))


def calibrate_attribution_evidence(
    event_selection: EventEvidenceSelection, candidates: Sequence[AnomalyCandidate],
    fund_flows: Sequence[FundFlowEvidence], *, market_event_time: datetime,
    as_of_time: datetime,
) -> AttributionEvidenceSelection:
    """Create attribution gates and keep post-event context in a separate channel."""
    require_aware(market_event_time, "market_event_time")
    require_aware(as_of_time, "as_of_time")
    quality = {(item.provider, item.symbol, item.evidence_id): item
               for item in event_selection.quality_flags}
    facts = []
    for item in event_selection.discovery_evidence:
        flags = quality[(item.provider, item.query_symbol, item.result_id)]
        strength = "weak" if flags.query_only_match or flags.match_method == "unlinked" else "strong"
        delta = flags.time_delta_seconds
        source_relation = "unknown" if delta is None else "pre_event" if delta < 0 else "post_event" if delta > 0 else "at_event"
        knowledge_relation = ("known_before_event" if item.available_at < market_event_time else
                              "known_after_event" if item.available_at > market_event_time else
                              "known_at_event")
        if delta is None:
            eligible, reason = False, "timestamp_unknown"
        elif strength == "weak" and flags.query_only_match:
            eligible, reason = False, "query_only_weak_entity"
        elif flags.match_method == "unlinked":
            eligible, reason = False, "entity_unconfirmed"
        elif delta > 0:
            eligible, reason = False, "post_event_followup"
        elif delta < -24 * 3600:
            eligible, reason = False, "outside_attribution_window"
        else:
            eligible, reason = True, "eligible_strong_entity_pre_event"
        strict = bool(eligible and item.strict_pit
                      and item.available_at <= market_event_time
                      and item.available_at <= as_of_time)
        if eligible and item.strict_pit and item.available_at > market_event_time:
            reason = "known_after_market_event"
        elif eligible and item.strict_pit and item.available_at > as_of_time:
            reason = "not_visible_as_of_time"
        facts.append(AttributionEvidenceFact(
            item.result_id, item.query_symbol, item.provider, flags.evidence_tier,
            flags.match_method, strength, source_relation, knowledge_relation,
            delta, eligible, strict, reason,
        ))
    fact_map = {(item.provider, item.symbol, item.evidence_id): item for item in facts}
    flows = {(item.trade_date, item.symbol): item for item in fund_flows}
    bundles = []
    for candidate in sorted(candidates, key=lambda item: item.rank):
        records = [item for item in event_selection.event_evidence if item.query_symbol == candidate.symbol]
        attribution = [item for item in records
                       if fact_map[(item.provider, candidate.symbol, item.result_id)].research_attribution_eligible]
        strict_attribution = [item for item in records
                              if fact_map[(item.provider, candidate.symbol, item.result_id)].strict_attribution_eligible]
        post = [item for item in records
                if fact_map[(item.provider, candidate.symbol, item.result_id)].temporal_relation == "post_event"]
        attribution.sort(key=lambda item: _attribution_sort_key(
            item, fact_map[(item.provider, candidate.symbol, item.result_id)]))
        strict_attribution.sort(key=lambda item: _attribution_sort_key(
            item, fact_map[(item.provider, candidate.symbol, item.result_id)]))
        post.sort(key=lambda item: _post_event_sort_key(
            item, fact_map[(item.provider, candidate.symbol, item.result_id)]))
        bundles.append(AttributionEvidenceBundle(
            candidate, flows.get((candidate.trade_date, candidate.symbol)),
            tuple(attribution), tuple(strict_attribution), tuple(post),
        ))
    return AttributionEvidenceSelection(event_selection, tuple(facts), tuple(bundles))


def audit_cross_provider_dedup(records: Sequence[WebSearchResult]) -> CrossProviderDedupAudit:
    providers = sorted({item.provider for item in records})
    if len(providers) < 2:
        return CrossProviderDedupAudit(0, 0, 0, 0, sum(_redirect_like(item.url) for item in records))
    left = [item for item in records if item.provider == providers[0]]
    right = [item for item in records if item.provider != providers[0]]
    exact = {(item.url, other.url) for item in left for other in right if item.url == other.url}
    normalized = {(a, b) for a in left for b in right if _safe_normalized_url(a.url) == _safe_normalized_url(b.url)}
    title_domain = {(a.result_id, b.result_id) for a in left for b in right
                    if _text(a.title) == _text(b.title) and _domain(a.url) == _domain(b.url)}
    title_only = {(a.result_id, b.result_id) for a in left for b in right if _text(a.title) == _text(b.title)}
    return CrossProviderDedupAudit(
        len(exact), len(normalized), len(title_domain), len(title_only),
        sum(_redirect_like(item.url) for item in records),
    )


def _metrics(flags):
    output = []
    for provider in sorted({item.provider for item in flags}):
        rows = [item for item in flags if item.provider == provider]
        known = [item for item in rows if item.timestamp_quality == "known"]
        count = len(rows)
        ratio = lambda number, denominator=count: number / denominator if denominator else 0.0
        output.append(ProviderQualityMetrics(
            provider, count, sum(item.eligible_for_event_evidence for item in rows),
            ratio(sum(item.has_title_entity_match for item in rows)),
            ratio(sum(item.has_snippet_entity_match for item in rows)),
            ratio(sum(item.query_only_match for item in rows)), ratio(len(known)),
            ratio(sum(item.inside_event_window is True for item in known), len(known)),
            ratio(sum(item.eligible_for_event_evidence for item in rows)),
        ))
    return tuple(output)


def _event_sort_key(item, flags):
    published_desc = -item.published_at.timestamp() if item.published_at else float("inf")
    return (flags.evidence_tier, flags.inside_event_window is not True,
            abs(flags.time_delta_seconds) if flags.time_delta_seconds is not None else float("inf"),
            flags.timestamp_quality != "known", published_desc, item.result_id)


def _attribution_sort_key(item, fact):
    return (fact.evidence_tier, fact.entity_strength != "strong",
            abs(fact.time_delta_seconds) if fact.time_delta_seconds is not None else float("inf"),
            -item.published_at.timestamp() if item.published_at else float("inf"), item.result_id)


def _post_event_sort_key(item, fact):
    return (fact.time_delta_seconds if fact.time_delta_seconds is not None else float("inf"),
            fact.evidence_tier, item.published_at.timestamp() if item.published_at else float("inf"),
            item.result_id)


def _shared_normalized_urls(records):
    providers_by_url = {}
    for item in records:
        value = _safe_normalized_url(item.url)
        if value:
            providers_by_url.setdefault(value, set()).add(item.provider)
    return {url for url, providers in providers_by_url.items() if len(providers) > 1}


def _safe_normalized_url(value):
    try:
        return normalize_web_url(value)
    except ValueError:
        return None


def _text(value):
    return "".join(str(value).lower().split())


def _domain(value):
    host = urllib.parse.urlsplit(value).hostname
    return host.lower().removeprefix("www.") if host else ""


def _redirect_like(value):
    parsed = urllib.parse.urlsplit(value)
    return any(token in parsed.path.lower() for token in ("redirect", "out", "away")) or any(
        key.lower() in {"url", "target", "redirect", "redirect_url"}
        for key, _ in urllib.parse.parse_qsl(parsed.query)
    )
