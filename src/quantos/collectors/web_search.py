"""Provider-neutral web search contract and metadata-only API adapters."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping, Protocol

from quantos.collectors.base import ProviderConfigurationError, ProviderPayloadError, ProviderResponseError, ProviderTransportError
from quantos.config import MARKET_TIMEZONE
from quantos.schemas import WebSearchResult
from quantos.schemas._validation import require_aware

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
EXA_SEARCH_URL = "https://api.exa.ai/search"
JsonTransport = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Mapping[str, Any]]
_TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src"}


class WebSearchProvider(Protocol):
    def search(
        self,
        *,
        query: str,
        query_symbol: str,
        start_time: datetime,
        end_time: datetime,
        max_results: int,
        pit_mode: str = "source_timestamp_proxy",
    ) -> list[WebSearchResult]: ...


def normalize_web_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("web search URL is invalid")
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port and not (
        (scheme == "http" and parsed.port == 80) or (scheme == "https" and parsed.port == 443)
    ) else ""
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    pairs = [(key, val) for key, val in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMETERS]
    return urllib.parse.urlunsplit((scheme, host + port, path, urllib.parse.urlencode(sorted(pairs)), ""))


def stable_web_result_id(url: str) -> str:
    return hashlib.sha256(normalize_web_url(url).encode("utf-8")).hexdigest()


class _BaseWebSearchProvider:
    provider_name: str
    endpoint: str
    environment_variable: str

    def __init__(self, *, api_key: str | None = None, transport: JsonTransport | None = None,
                 clock: Callable[[], datetime] | None = None, timeout_seconds: float = 20.0,
                 request_interval_seconds: float = 0.25,
                 sleeper: Callable[[float], None] | None = None) -> None:
        if timeout_seconds <= 0 or request_interval_seconds < 0:
            raise ValueError("web search request limits are invalid")
        self._api_key = api_key if api_key is not None else os.environ.get(self.environment_variable)
        self._transport = transport or _json_post
        self._clock = clock or (lambda: datetime.now(MARKET_TIMEZONE))
        self._timeout = timeout_seconds
        self._interval = request_interval_seconds
        self._sleep = sleeper or time.sleep
        self._request_count = 0

    @property
    def request_count(self) -> int:
        return self._request_count

    def search(self, *, query: str, query_symbol: str, start_time: datetime,
               end_time: datetime, max_results: int,
               pit_mode: str = "source_timestamp_proxy") -> list[WebSearchResult]:
        for name, value in (("start_time", start_time), ("end_time", end_time)):
            require_aware(value, name)
        if start_time > end_time or not query.strip() or not query_symbol.strip():
            raise ValueError("web search query parameters are invalid")
        if not 1 <= max_results <= 50:
            raise ValueError("max_results is invalid")
        if pit_mode not in {"strict_live", "source_timestamp_proxy"}:
            raise ValueError("pit_mode is invalid")
        if not self._api_key:
            raise ProviderConfigurationError(f"{self.provider_name} credential is missing")
        if self._request_count and self._interval:
            self._sleep(self._interval)
        self._request_count += 1
        try:
            payload = self._transport(self.endpoint, self._headers(self._api_key),
                                      self._request_body(query.strip(), start_time, end_time, max_results),
                                      self._timeout)
        except (ProviderTransportError, ProviderResponseError):
            raise
        except Exception as exc:
            raise ProviderTransportError(f"{self.provider_name} search request failed") from exc
        return self._map_payload(payload, query.strip(), query_symbol.strip(), pit_mode)

    def _headers(self, key: str) -> Mapping[str, str]:
        raise NotImplementedError

    def _request_body(self, query: str, start: datetime, end: datetime, maximum: int) -> Mapping[str, Any]:
        raise NotImplementedError

    def _map_payload(self, payload: Mapping[str, Any], query: str, symbol: str,
                     pit_mode: str) -> list[WebSearchResult]:
        values = payload.get("results") if isinstance(payload, Mapping) else None
        if not isinstance(values, list):
            raise ProviderPayloadError(f"{self.provider_name} results are malformed")
        collected = self._clock()
        require_aware(collected, "collected_at")
        records: dict[str, WebSearchResult] = {}
        for value in values:
            if not isinstance(value, Mapping):
                raise ProviderPayloadError(f"{self.provider_name} result item is malformed")
            try:
                url = normalize_web_url(str(value["url"]))
                title = str(value["title"]).strip()
                if not title:
                    raise ValueError("empty title")
                published = _parse_datetime(value.get("published_date") or value.get("publishedDate"))
                result_id = stable_web_result_id(url)
                records.setdefault(result_id, WebSearchResult(
                    result_id=result_id, query=query, query_symbol=symbol, title=title,
                    snippet=self._snippet(value), url=url,
                    domain=urllib.parse.urlsplit(url).hostname or "unknown",
                    published_at=published,
                    timestamp_basis="provider_reported" if published else "unknown",
                    collected_at=collected, available_at=collected, provider=self.provider_name,
                    provider_record_id=str(value.get("id") or value.get("url")),
                    provider_score=_optional_float(value.get("score")),
                    pit_mode=pit_mode,
                ))
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ProviderPayloadError(f"{self.provider_name} result item is invalid") from exc
        return sorted(records.values(), key=lambda item: item.result_id)

    def _snippet(self, value: Mapping[str, Any]) -> str:
        return str(value.get("content") or value.get("text") or "").strip()


class TavilyWebSearchProvider(_BaseWebSearchProvider):
    provider_name, endpoint, environment_variable = "tavily", TAVILY_SEARCH_URL, "TAVILY_API_KEY"

    def _headers(self, key: str) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _request_body(self, query: str, start: datetime, end: datetime, maximum: int) -> Mapping[str, Any]:
        return {"query": query, "topic": "news", "search_depth": "basic", "max_results": maximum,
                "include_answer": False, "include_raw_content": False, "include_images": False,
                "start_date": start.date().isoformat(), "end_date": end.date().isoformat()}


class ExaWebSearchProvider(_BaseWebSearchProvider):
    provider_name, endpoint, environment_variable = "exa", EXA_SEARCH_URL, "EXA_API_KEY"

    def _headers(self, key: str) -> Mapping[str, str]:
        return {"x-api-key": key, "Content-Type": "application/json"}

    def _request_body(self, query: str, start: datetime, end: datetime, maximum: int) -> Mapping[str, Any]:
        return {"query": query, "type": "auto", "numResults": maximum,
                "startPublishedDate": start.astimezone(timezone.utc).isoformat(),
                "endPublishedDate": end.astimezone(timezone.utc).isoformat()}

    def _snippet(self, value: Mapping[str, Any]) -> str:
        highlights = value.get("highlights")
        if isinstance(highlights, list):
            return " ".join(str(item) for item in highlights if item is not None).strip()
        return str(value.get("text") or "").strip()


def _json_post(url: str, headers: Mapping[str, str], body: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        category = "authentication" if exc.code in {401, 403} else ("rate_limit" if exc.code == 429 else "server" if exc.code >= 500 else "request")
        raise ProviderResponseError(f"web search HTTP {category} error") from None
    except (OSError, urllib.error.URLError) as exc:
        raise ProviderTransportError("web search transport failed") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProviderPayloadError("web search response is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ProviderPayloadError("web search response is malformed")
    return payload


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            raise ValueError("provider timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=MARKET_TIMEZONE)
    return parsed.astimezone(MARKET_TIMEZONE)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if result == result else None
