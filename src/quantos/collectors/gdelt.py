"""GDELT DOC API adapter for structured news metadata only."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from quantos.collectors.base import ProviderPayloadError, ProviderTransportError
from quantos.config import MARKET_TIMEZONE
from quantos.schemas import NewsRecord
from quantos.schemas._validation import require_aware

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
JsonTransport = Callable[[str, Mapping[str, str], float], Mapping[str, Any]]


class GdeltNewsProvider:
    def __init__(
        self,
        *,
        transport: JsonTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout_seconds: float = 15.0,
        request_interval_seconds: float = 0.25,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or request_interval_seconds < 0:
            raise ValueError("GDELT request limits are invalid")
        self._transport = transport or _public_json_get
        self._clock = clock or (lambda: datetime.now(MARKET_TIMEZONE))
        self._timeout = timeout_seconds
        self._interval = request_interval_seconds
        self._sleep = sleeper or time.sleep
        self._request_count = 0

    def search_company_news(
        self,
        *,
        query_symbol: str,
        query_term: str,
        start_time: datetime,
        end_time: datetime,
        max_records: int = 50,
        pit_mode: str = "source_timestamp_proxy",
    ) -> list[NewsRecord]:
        del query_symbol  # relationship provenance is retained by DiscoveryMention.
        for name, value in (("start_time", start_time), ("end_time", end_time)):
            require_aware(value, name)
        if start_time > end_time:
            raise ValueError("start_time cannot be after end_time")
        if not query_term.strip() or not 1 <= max_records <= 50:
            raise ValueError("GDELT query and max_records are invalid")
        if pit_mode not in {"strict_live", "source_timestamp_proxy"}:
            raise ValueError("pit_mode is invalid")
        if self._request_count and self._interval:
            self._sleep(self._interval)
        self._request_count += 1
        parameters = {
            "query": f'"{query_term.strip()}"', "mode": "artlist",
            "format": "json", "maxrecords": str(max_records),
            "startdatetime": start_time.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S"),
            "enddatetime": end_time.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S"),
        }
        try:
            payload = self._transport(GDELT_DOC_URL, parameters, self._timeout)
        except ProviderTransportError:
            raise
        except Exception as exc:
            raise ProviderTransportError("GDELT DOC request failed") from exc
        articles = payload.get("articles", []) if isinstance(payload, Mapping) else None
        if not isinstance(articles, list):
            raise ProviderPayloadError("GDELT articles are malformed")
        collected_at = self._clock()
        require_aware(collected_at, "collected_at")
        records: dict[str, NewsRecord] = {}
        for index, article in enumerate(articles):
            if not isinstance(article, Mapping):
                raise ProviderPayloadError(f"GDELT article {index} is malformed")
            try:
                url = _canonical_url(str(article["url"]))
                title = str(article["title"]).strip()
                provider_time = _parse_gdelt_time(article.get("seendate"))
                domain = str(article.get("domain") or urllib.parse.urlsplit(url).hostname or "").lower()
                identity = hashlib.sha256(f"gdelt|{url}".encode("utf-8")).hexdigest()
                records.setdefault(identity, NewsRecord(
                    news_id=identity, source=domain or "gdelt",
                    source_type="news", published_at=provider_time,
                    collected_at=collected_at,
                    available_at=max(provider_time, collected_at), title=title,
                    content=None, url=url, channels=(), provider="gdelt",
                    provider_record_id=identity, pit_mode=pit_mode,
                    domain=domain or None,
                    language=str(article.get("language") or "").strip() or None,
                    timestamp_basis="provider_reported",
                    evidence_basis="structured_news_metadata",
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderPayloadError(f"GDELT article {index} is invalid") from exc
        return sorted(records.values(), key=lambda item: (item.published_at, item.news_id))


def _public_json_get(url: str, parameters: Mapping[str, str], timeout: float) -> Mapping[str, Any]:
    endpoint = f"{url}?{urllib.parse.urlencode(parameters)}"
    request = urllib.request.Request(
        endpoint,
        headers={"Accept": "application/json", "User-Agent": "QuantOS/0.1 (+structured-news-metadata-client)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise ProviderTransportError("GDELT DOC request failed") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProviderPayloadError("GDELT response is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ProviderPayloadError("GDELT response is malformed")
    return payload


def _parse_gdelt_time(value: Any) -> datetime:
    text = str(value).strip()
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc).astimezone(MARKET_TIMEZONE)
        except ValueError:
            continue
    raise ValueError("GDELT seendate is invalid")


def _canonical_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("article URL is invalid")
    query = urllib.parse.urlencode(sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)))
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", query, ""))
