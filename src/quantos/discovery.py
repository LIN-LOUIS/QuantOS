"""Bounded concurrent orchestration for independent evidence providers."""

from __future__ import annotations

import hashlib
import threading
import time
import urllib.parse
from statistics import median
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Callable, Mapping, Sequence

from quantos.collectors import (
    CninfoAnnouncementProvider,
    GdeltNewsProvider,
    ProviderConfigurationError,
    ProviderTransportError,
    WebSearchProvider,
)
from quantos.config import MARKET_TIMEZONE
from quantos.news import normalize_company_alias
from quantos.schemas import (
    AnnouncementRecord,
    AnomalyCandidate,
    CandidateCoverage,
    CanonicalDiscoveredEvidence,
    DiscoveryMention,
    EvidenceCoverageReport,
    EvidenceDiscoveryRequest,
    FundFlowEvidence,
    MultiSourceCandidateEvidenceBundle,
    NewsRecord,
    ProviderCoverage,
    ProviderDiscoveryResult,
    ProviderBenchmarkReport,
    WebSearchResult,
    SecurityMaster,
)


@dataclass(frozen=True, slots=True)
class ProviderPayload:
    records: tuple[object, ...]
    mentions: tuple[DiscoveryMention, ...]
    raw_result_count: int
    successful_symbols: tuple[str, ...]
    failed_symbols: tuple[str, ...] = ()
    request_count: int = 0
    budget_exhausted: bool = False


ProviderRunner = Callable[[EvidenceDiscoveryRequest], ProviderPayload]


@dataclass(frozen=True, slots=True)
class RegisteredEvidenceProvider:
    name: str
    runner: ProviderRunner
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class EvidenceDiscoveryOutput:
    provider_results: tuple[ProviderDiscoveryResult, ...]
    canonical_news: tuple[CanonicalDiscoveredEvidence, ...]
    announcements: tuple[AnnouncementRecord, ...]
    search_results: tuple[WebSearchResult, ...]
    coverage: EvidenceCoverageReport
    bundles: tuple[MultiSourceCandidateEvidenceBundle, ...]
    cross_provider_duplicate_count: int


def discover_candidate_evidence(
    request: EvidenceDiscoveryRequest,
    candidates: Sequence[AnomalyCandidate],
    fund_flows: Sequence[FundFlowEvidence],
    providers: Sequence[RegisteredEvidenceProvider],
    *,
    max_provider_workers: int = 3,
    clock: Callable[[], datetime] | None = None,
) -> EvidenceDiscoveryOutput:
    if max_provider_workers < 1:
        raise ValueError("max_provider_workers must be positive")
    now = clock or (lambda: datetime.now(MARKET_TIMEZONE))
    results: list[ProviderDiscoveryResult] = []
    enabled = [item for item in providers if item.enabled]
    for item in providers:
        if not item.enabled:
            timestamp = now()
            results.append(_empty_result(item.name, "unavailable", timestamp, "DisabledProvider", len(request.symbols)))
    with ThreadPoolExecutor(max_workers=min(max_provider_workers, max(1, len(enabled)))) as executor:
        futures = {
            executor.submit(_run_provider, item, request, now): item.name
            for item in enabled
        }
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item.provider_name)
    all_records = [record for result in results for record in result.records]
    all_mentions = [mention for result in results for mention in result.mentions]
    announcements = tuple(sorted(
        (item for item in all_records if isinstance(item, AnnouncementRecord)
         and item.available_at <= request.as_of_time
         and (request.mode == "proxy_research" or item.strict_pit)),
        key=lambda item: (item.published_at, item.announcement_id),
    ))
    news = [item for item in all_records if isinstance(item, NewsRecord)]
    searches = tuple(sorted(
        (item for item in all_records if isinstance(item, WebSearchResult)),
        key=lambda item: ((item.published_at or item.collected_at), item.result_id),
    ))
    news, searches, all_mentions = _filter_pit_mode(request, news, searches, all_mentions)
    canonical, source_to_canonical = deduplicate_cross_provider_news(news, searches, all_mentions)
    coverage = _coverage(request, results, all_mentions, announcements, source_to_canonical)
    bundles = _bundles(
        request, candidates, fund_flows, announcements, canonical,
        searches, all_mentions, source_to_canonical,
    )
    return EvidenceDiscoveryOutput(
        provider_results=tuple(results), canonical_news=canonical,
        announcements=announcements, search_results=searches,
        coverage=coverage, bundles=bundles,
        cross_provider_duplicate_count=len(news) + len(searches) - len(canonical),
    )


def collect_strict_live_candidate_evidence(
    *, trade_date: date, as_of_time: datetime,
    candidates: Sequence[AnomalyCandidate], securities: Sequence[SecurityMaster],
    providers: Mapping[str, WebSearchProvider], fund_flows: Sequence[FundFlowEvidence] = (),
    max_queries_per_provider_per_run: int = 40, repository=None,
) -> EvidenceDiscoveryOutput:
    """Collect Query-A-only evidence with strict availability and a per-provider budget."""
    if max_queries_per_provider_per_run < 1:
        raise ValueError("max_queries_per_provider_per_run must be positive")
    visible_master = {
        item.symbol: item for item in securities
        if item.available_at <= as_of_time and item.effective_from <= trade_date
        and (item.effective_to is None or item.effective_to >= trade_date)
    }
    selected = tuple(sorted(
        (item for item in candidates if item.trade_date == trade_date and item.symbol in visible_master),
        key=lambda item: item.rank,
    ))
    if not selected:
        raise ValueError("no PIT-visible candidates have SecurityMaster identity")
    request = EvidenceDiscoveryRequest(
        target_trade_date=trade_date,
        market_event_time=datetime.combine(trade_date, datetime_time(15), tzinfo=MARKET_TIMEZONE),
        as_of_time=as_of_time, symbols=tuple(item.symbol for item in selected),
        company_names=tuple(visible_master[item.symbol].company_name for item in selected),
        company_aliases=tuple(visible_master[item.symbol].aliases for item in selected),
        mode="strict", top_n=max(20, len(selected)), max_results_per_candidate=5,
    )
    output = discover_candidate_evidence(
        request, selected, fund_flows,
        [RegisteredEvidenceProvider(
            name, web_search_runner(provider, name, include_safe_alias_query=False,
                                    max_queries_per_run=max_queries_per_provider_per_run)
        ) for name, provider in sorted(providers.items())],
        max_provider_workers=min(2, max(1, len(providers))),
    )
    collected = tuple(
        record for result in output.provider_results for record in result.records
        if isinstance(record, WebSearchResult)
    )
    if any(not item.strict_pit or item.pit_mode != "strict_live" or item.available_at < item.collected_at
           for item in collected):
        raise ValueError("strict-live provider returned invalid PIT metadata")
    if repository is not None:
        repository.write_web_search_results(collected)
    return output


def gdelt_runner(provider: GdeltNewsProvider) -> ProviderRunner:
    def run(request: EvidenceDiscoveryRequest) -> ProviderPayload:
        records: list[NewsRecord] = []
        mentions: list[DiscoveryMention] = []
        successful: list[str] = []
        failed: list[str] = []
        request_count = 0
        start = request.market_event_time - timedelta(hours=request.before_hours)
        end = request.market_event_time + timedelta(hours=request.after_hours)
        mode = "strict_live" if request.mode == "strict" else "source_timestamp_proxy"
        raw = 0
        for symbol, company in zip(request.symbols, request.company_names):
            query = normalize_company_alias(company)
            try:
                found = provider.search_company_news(
                    query_symbol=symbol, query_term=query, start_time=start,
                    end_time=end, max_records=request.max_results_per_candidate,
                    pit_mode=mode,
                )
            except Exception:
                failed.append(symbol)
                continue
            successful.append(symbol)
            raw += len(found)
            records.extend(found)
            for item in found:
                title_match = _normalize(query) in _normalize(item.title)
                mentions.append(DiscoveryMention(
                    evidence_id=item.news_id, symbol=symbol, provider="gdelt",
                    query_term=query,
                    match_method="title_company_name" if title_match else "provider_exact_company_query",
                    matched_term=query, evidence_tier=2 if title_match else 3,
                    discovered_at=item.collected_at,
                ))
        unique = {item.news_id: item for item in records}
        return ProviderPayload(
            records=tuple(sorted(unique.values(), key=lambda item: item.news_id)),
            mentions=tuple(sorted(mentions, key=lambda item: (item.symbol, item.evidence_id))),
            raw_result_count=raw, successful_symbols=tuple(successful),
            failed_symbols=tuple(failed),
        )
    return run


def cninfo_runner(provider: CninfoAnnouncementProvider) -> ProviderRunner:
    def run(request: EvidenceDiscoveryRequest) -> ProviderPayload:
        mode = "strict_live" if request.mode == "strict" else "source_timestamp_proxy"
        records = provider.fetch_announcements(
            start_date=(request.market_event_time - timedelta(hours=request.before_hours)).date(),
            end_date=(request.market_event_time + timedelta(hours=request.after_hours)).date(),
            pit_mode=mode, symbols=request.symbols,
        )
        mentions = tuple(DiscoveryMention(
            evidence_id=item.announcement_id, symbol=item.symbol,
            provider="cninfo", query_term=item.symbol,
            match_method="announcement_symbol", matched_term=item.symbol,
            evidence_tier=1, discovered_at=item.collected_at,
        ) for item in records)
        symbols = tuple(sorted({item.symbol for item in records}))
        return ProviderPayload(tuple(records), mentions, len(records), symbols)
    return run


def web_search_runner(
    provider: WebSearchProvider, provider_name: str, *, include_safe_alias_query: bool = False,
    max_queries_per_run: int | None = None,
) -> ProviderRunner:
    if max_queries_per_run is not None and max_queries_per_run < 1:
        raise ValueError("max_queries_per_run must be positive")
    def run(request: EvidenceDiscoveryRequest) -> ProviderPayload:
        records: list[WebSearchResult] = []
        mentions: list[DiscoveryMention] = []
        successful: list[str] = []
        failed: list[str] = []
        request_count = 0
        start = request.market_event_time - timedelta(hours=request.before_hours)
        end = request.market_event_time + timedelta(hours=request.after_hours)
        aliases_by_symbol = request.company_aliases or tuple(() for _ in request.symbols)
        for symbol, company, aliases in zip(request.symbols, request.company_names, aliases_by_symbol):
            safe_aliases = tuple(alias for alias in aliases if is_safe_company_alias(alias, request))
            queries = [company.strip()]
            if include_safe_alias_query and safe_aliases:
                queries.append(f"{safe_aliases[0]} 股票")
            symbol_ok = False
            for query in queries:
                if max_queries_per_run is not None and request_count >= max_queries_per_run:
                    failed.extend(item for item in request.symbols if item not in successful and item not in failed)
                    unique = {item.result_id: item for item in records}
                    return ProviderPayload(
                        tuple(sorted(unique.values(), key=lambda item: item.result_id)),
                        tuple(sorted(mentions, key=lambda item: (item.symbol, item.evidence_id))),
                        len(records), tuple(successful), tuple(failed), request_count, True,
                    )
                request_count += 1
                try:
                    found = provider.search(query=query, query_symbol=symbol, start_time=start,
                                            end_time=end, max_results=request.max_results_per_candidate,
                                            pit_mode=("strict_live" if request.mode == "strict" else "source_timestamp_proxy"))
                except Exception:
                    continue
                symbol_ok = True
                records.extend(found)
                for item in found:
                    linked = _link_web_result(item, company, safe_aliases, symbol, query, request)
                    if linked is not None:
                        method, term, tier = linked
                        mentions.append(DiscoveryMention(
                            evidence_id=item.result_id, symbol=symbol, provider=provider_name,
                            query_term=query, match_method=method, matched_term=term,
                            evidence_tier=tier, discovered_at=item.collected_at,
                        ))
            (successful if symbol_ok else failed).append(symbol)
        unique = {item.result_id: item for item in records}
        return ProviderPayload(
            tuple(sorted(unique.values(), key=lambda item: item.result_id)),
            tuple(sorted(mentions, key=lambda item: (item.symbol, item.evidence_id))),
            len(records), tuple(successful), tuple(failed), request_count,
        )
    return run


def deduplicate_cross_provider_news(
    news: Sequence[NewsRecord],
    searches: Sequence[WebSearchResult],
    mentions: Sequence[DiscoveryMention],
) -> tuple[tuple[CanonicalDiscoveredEvidence, ...], dict[str, str]]:
    items = [_article_view(item) for item in (*news, *searches)]
    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            if _same_article(items[left], items[right]):
                union(left, right)
    groups: dict[int, list[dict[str, object]]] = {}
    for index, item in enumerate(items):
        groups.setdefault(find(index), []).append(item)
    mention_map: dict[str, list[DiscoveryMention]] = {}
    for mention in mentions:
        mention_map.setdefault(mention.evidence_id, []).append(mention)
    canonical: list[CanonicalDiscoveredEvidence] = []
    source_to_canonical: dict[str, str] = {}
    for group in groups.values():
        group.sort(key=lambda item: str(item["id"]))
        source_ids = tuple(sorted({str(item["id"]) for item in group}))
        canonical_urls = {_canonical_url(item["url"]) for item in group if _canonical_url(item["url"])}
        identity = (next(iter(canonical_urls)) if len(canonical_urls) == 1 else None)
        identity = hashlib.sha256(str(identity or "|".join(source_ids)).encode("utf-8")).hexdigest()
        best = min(group, key=lambda item: (
            {"publisher_reported": 0, "provider_reported": 1, "collected_only": 2, "unknown": 3}[str(item["timestamp_basis"])],
            item["published_at"] or datetime.max.replace(tzinfo=MARKET_TIMEZONE),
            str(item["id"]),
        ))
        providers = tuple(sorted({str(item["provider"]) for item in group}))
        queries = tuple(sorted({
            mention.query_term for source_id in source_ids
            for mention in mention_map.get(source_id, ())
        }))
        strict = all(bool(item["strict_pit"]) for item in group)
        canonical.append(CanonicalDiscoveredEvidence(
            evidence_id=identity, evidence_type="news_article",
            title=str(best["title"]), url=best["url"], domain=best["domain"],
            published_at=best["published_at"], timestamp_basis=str(best["timestamp_basis"]),
            evidence_basis=("structured_news_metadata" if any(item["kind"] == "news" for item in group) else "search_result_metadata"),
            pit_mode="strict_live" if strict else "source_timestamp_proxy",
            strict_pit=strict, discovered_by=providers,
            discovery_queries=queries,
            source_record_ids=tuple(sorted({str(item["source_record_id"]) for item in group})),
        ))
        source_to_canonical.update({source_id: identity for source_id in source_ids})
    canonical.sort(key=lambda item: ((item.published_at or datetime.max.replace(tzinfo=MARKET_TIMEZONE)), item.evidence_id))
    return tuple(canonical), source_to_canonical


def _run_provider(entry, request, clock) -> ProviderDiscoveryResult:
    started = clock()
    monotonic_start = time.monotonic_ns()
    try:
        payload = entry.runner(request)
        if payload.failed_symbols and not payload.successful_symbols:
            status = "unavailable"
        else:
            status = "partial" if payload.failed_symbols else "pass"
        completed = clock()
        return ProviderDiscoveryResult(
            provider_name=entry.name, status=status, started_at=started,
            completed_at=completed,
            duration_ms=max(0, (time.monotonic_ns() - monotonic_start) // 1_000_000),
            requested_candidates=len(request.symbols),
            successful_candidates=len(set(payload.successful_symbols)),
            failed_candidates=tuple(sorted(payload.failed_symbols)),
            raw_result_count=payload.raw_result_count,
            normalized_record_count=len(payload.records),
            mention_count=len(payload.mentions), records=payload.records,
            mentions=payload.mentions,
            request_count=payload.request_count,
            safe_error_type=("QueryBudgetReached" if payload.budget_exhausted else
                             "CandidateRequestsUnavailable" if status == "unavailable" else None),
        )
    except (ProviderConfigurationError, ProviderTransportError, TimeoutError) as exc:
        return _empty_result(entry.name, "unavailable", clock(), type(exc).__name__, len(request.symbols), started, monotonic_start)
    except Exception as exc:
        return _empty_result(entry.name, "failed", clock(), type(exc).__name__, len(request.symbols), started, monotonic_start)


def _empty_result(name, status, completed, error, requested, started=None, monotonic_start=None):
    started = started or completed
    duration = 0 if monotonic_start is None else max(0, (time.monotonic_ns() - monotonic_start) // 1_000_000)
    return ProviderDiscoveryResult(
        name, status, started, completed, duration, requested, 0,
        tuple(), 0, 0, 0, safe_error_type=error,
    )


def _filter_pit_mode(request, news, searches, mentions):
    if request.mode == "strict":
        news = [item for item in news if item.strict_pit and item.available_at <= request.as_of_time]
        searches = tuple(item for item in searches if item.strict_pit and item.available_at <= request.as_of_time)
    else:
        news = [item for item in news if item.available_at <= request.as_of_time]
        searches = tuple(item for item in searches if item.available_at <= request.as_of_time)
    valid_ids = {item.news_id for item in news} | {item.result_id for item in searches}
    mentions = [item for item in mentions if item.evidence_id in valid_ids]
    return news, searches, mentions


def _coverage(request, results, mentions, announcements, source_to_canonical):
    provider_rows = []
    visible_mentions = {(item.provider, item.evidence_id, item.symbol) for item in mentions}
    visible_announcement_ids = {item.announcement_id for item in announcements}
    for result in results:
        result_mentions = [
            item for item in result.mentions
            if (item.provider, item.evidence_id, item.symbol) in visible_mentions
            or item.evidence_id in visible_announcement_ids
        ]
        symbols = {item.symbol for item in result_mentions}
        evidence_ids = {item.evidence_id for item in result_mentions}
        provider_rows.append(ProviderCoverage(
            result.provider_name, result.status, len(symbols), len(evidence_ids)
        ))
    candidates = []
    for symbol in request.symbols:
        symbol_mentions = [item for item in mentions if item.symbol == symbol]
        announcements_for_symbol = [item for item in announcements if item.symbol == symbol]
        canonical_ids = {source_to_canonical[item.evidence_id] for item in symbol_mentions if item.evidence_id in source_to_canonical}
        candidates.append(CandidateCoverage(
            symbol=symbol,
            has_official_announcement=bool(announcements_for_symbol),
            has_gdelt_news=any(item.provider == "gdelt" for item in symbol_mentions),
            has_web_search=any(item.provider not in {"gdelt", "cninfo"} for item in symbol_mentions),
            total_evidence_count=len(announcements_for_symbol) + len(canonical_ids),
        ))
    return EvidenceCoverageReport(len(request.symbols), tuple(provider_rows), tuple(candidates))


def _bundles(request, candidates, fund_flows, announcements, canonical, searches, mentions, source_map):
    flows = {(item.trade_date, item.symbol): item for item in fund_flows}
    canonical_by_id = {item.evidence_id: item for item in canonical}
    search_by_id = {item.result_id: item for item in searches}
    output = []
    for candidate in sorted(candidates, key=lambda item: item.rank):
        if candidate.symbol not in request.symbols:
            continue
        symbol_mentions = [item for item in mentions if item.symbol == candidate.symbol]
        canonical_ids = sorted({source_map[item.evidence_id] for item in symbol_mentions if item.evidence_id in source_map})
        search_ids = sorted({item.evidence_id for item in symbol_mentions if item.evidence_id in search_by_id})
        output.append(MultiSourceCandidateEvidenceBundle(
            candidate=candidate,
            fund_flow_evidence=flows.get((candidate.trade_date, candidate.symbol)),
            announcement_evidence=tuple(item for item in announcements if item.symbol == candidate.symbol),
            news_evidence=tuple(canonical_by_id[item] for item in canonical_ids),
            search_evidence=tuple(search_by_id[item] for item in search_ids),
        ))
    return tuple(output)


def _article_view(item):
    if isinstance(item, NewsRecord):
        return {"id": item.news_id, "kind": "news", "title": item.title, "url": item.url,
                "domain": item.domain or _url_domain(item.url), "published_at": item.published_at,
                "timestamp_basis": item.timestamp_basis, "provider": item.provider,
                "strict_pit": item.strict_pit, "source_record_id": item.provider_record_id}
    return {"id": item.result_id, "kind": "search", "title": item.title, "url": item.url,
            "domain": item.domain, "published_at": item.published_at,
            "timestamp_basis": item.timestamp_basis, "provider": item.provider,
            "strict_pit": item.strict_pit, "source_record_id": item.provider_record_id}


def _same_article(left, right):
    left_url, right_url = _canonical_url(left["url"]), _canonical_url(right["url"])
    if left_url and right_url and left_url == right_url:
        return True
    if _normalize(left["title"]) != _normalize(right["title"]):
        return False
    if left["domain"] and right["domain"] and str(left["domain"]).lower() == str(right["domain"]).lower():
        return True
    if left["published_at"] is not None and right["published_at"] is not None:
        return abs(left["published_at"] - right["published_at"]) <= timedelta(minutes=10)
    return False


def _canonical_url(value):
    if not value:
        return None
    parsed = urllib.parse.urlsplit(str(value))
    if not parsed.hostname:
        return None
    from quantos.collectors.web_search import normalize_web_url
    try:
        return normalize_web_url(str(value))
    except ValueError:
        return None


def _url_domain(value):
    return urllib.parse.urlsplit(value).hostname.lower() if value and urllib.parse.urlsplit(value).hostname else None


def _normalize(value):
    return "".join(str(value).lower().split())


def build_provider_benchmark_reports(output: EvidenceDiscoveryOutput) -> tuple[ProviderBenchmarkReport, ...]:
    """Build deterministic provider metrics without creating a composite score."""
    reports = []
    for result in output.provider_results:
        records = [item for item in result.records if isinstance(item, WebSearchResult)]
        mentions = list(result.mentions)
        methods = [item.match_method for item in mentions]
        linked_ids = {item.evidence_id for item in mentions}
        counts_by_symbol = [sum(item.symbol == symbol for item in mentions)
                            for symbol in sorted({item.symbol for item in mentions})]
        duplicate_count = sum(
            1 for canonical in output.canonical_news
            if result.provider_name in canonical.discovered_by and len(canonical.discovered_by) > 1
        )
        reports.append(ProviderBenchmarkReport(
            provider_name=result.provider_name, requested_candidates=result.requested_candidates,
            successful_candidates=result.successful_candidates, raw_results=result.raw_result_count,
            deduped_results=len({item.result_id for item in records}), linked_mentions=len(mentions),
            candidates_with_evidence=len({item.symbol for item in mentions}),
            coverage_ratio=(len({item.symbol for item in mentions}) / result.requested_candidates
                            if result.requested_candidates else 0.0),
            title_exact_matches=methods.count("title_company_name"),
            title_alias_matches=methods.count("title_alias"), title_symbol_matches=methods.count("title_symbol"),
            snippet_company_matches=methods.count("snippet_company_name"),
            snippet_alias_matches=methods.count("snippet_alias"),
            query_only_matches=methods.count("provider_exact_company_query"),
            unlinked_result_count=len({item.result_id for item in records} - linked_ids),
            unknown_timestamp_count=sum(item.published_at is None for item in records),
            no_url_count=sum(not item.url for item in records),
            duplicate_with_other_provider_count=duplicate_count,
            median_results_per_candidate=float(median(counts_by_symbol)) if counts_by_symbol else 0.0,
            request_count=result.request_count, duration_ms=result.duration_ms,
        ))
    return tuple(sorted(reports, key=lambda item: item.provider_name))


def is_safe_company_alias(alias: str, request: EvidenceDiscoveryRequest) -> bool:
    value = _normalize(alias)
    if len(value) < 4 or value in {"中国", "科技", "股份", "集团", "发展", "银行", "证券"}:
        return False
    identities = [
        index for index, company in enumerate(request.company_names)
        if value == _normalize(company) or value == _normalize(normalize_company_alias(company))
        or (request.company_aliases and value in {_normalize(item) for item in request.company_aliases[index]})
    ]
    return len(identities) == 1


def _link_web_result(item, company, aliases, symbol, query, request):
    title, snippet = _normalize(item.title), _normalize(item.snippet)
    company_value = _normalize(company)
    code = symbol.split(".", 1)[0]
    checks = [
        (company_value in title, "title_company_name", company, 2),
        (next((alias for alias in aliases if _normalize(alias) in title), None), "title_alias", None, 2),
        (code in title, "title_symbol", code, 2),
        (company_value in snippet, "snippet_company_name", company, 3),
        (next((alias for alias in aliases if _normalize(alias) in snippet), None), "snippet_alias", None, 3),
    ]
    for matched, method, fixed_term, tier in checks:
        if matched:
            return method, fixed_term or str(matched), tier
    base_query = query[:-3].strip() if query.endswith(" 股票") else query
    if (_normalize(base_query) == company_value or any(_normalize(base_query) == _normalize(a) for a in aliases)):
        if is_safe_company_alias(base_query, request) or _normalize(base_query) == company_value:
            return "provider_exact_company_query", base_query, 3
    return None
