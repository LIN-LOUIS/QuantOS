"""CNINFO official announcement adapter using ordinary public HTTP requests."""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Any, Callable, Mapping, Sequence

from quantos.collectors.base import (
    ProviderPayloadError,
    ProviderResponseError,
    ProviderTransportError,
)
from quantos.config import MARKET_TIMEZONE
from quantos.news import stable_announcement_id
from quantos.schemas import AnnouncementRecord
from quantos.schemas._validation import require_aware

CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_PDF_BASE = "https://static.cninfo.com.cn/"
_PIT_MODES = {"strict_live", "source_timestamp_proxy"}
_COLUMN_SUFFIXES = {"szse": ".SZ", "sse": ".SH", "bj": ".BJ"}

JsonTransport = Callable[[str, Mapping[str, str], float], Mapping[str, Any]]


class CninfoAnnouncementProvider:
    """Map CNINFO disclosure payloads without storage or entity-linking side effects."""

    def __init__(
        self,
        *,
        transport: JsonTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout_seconds: float = 15.0,
        page_size: int = 30,
        max_pages: int = 10,
        columns: Sequence[str] = ("szse", "sse"),
    ) -> None:
        if timeout_seconds <= 0 or page_size < 1 or max_pages < 1:
            raise ValueError("CNINFO request limits must be positive")
        if not columns or any(column not in _COLUMN_SUFFIXES for column in columns):
            raise ValueError("CNINFO columns are invalid")
        self._transport = transport or _public_json_post
        self._clock = clock or (lambda: datetime.now(MARKET_TIMEZONE))
        self._timeout = timeout_seconds
        self._page_size = min(page_size, 30)
        self._max_pages = max_pages
        self._columns = tuple(columns)

    def fetch_announcements(
        self,
        *,
        start_date: date,
        end_date: date,
        pit_mode: str = "strict_live",
        symbols: Sequence[str] | None = None,
    ) -> list[AnnouncementRecord]:
        if start_date > end_date:
            raise ValueError("start_date cannot be after end_date")
        if pit_mode not in _PIT_MODES:
            raise ValueError("pit_mode is invalid")
        collected_at = self._clock()
        require_aware(collected_at, "collected_at")
        requested_symbols = set(symbols or ())
        output: dict[str, AnnouncementRecord] = {}
        for column in self._columns:
            page = 1
            while True:
                payload = self._query_page(column, page, start_date, end_date)
                rows = payload.get("announcements") or []
                if not isinstance(rows, list):
                    raise ProviderPayloadError("CNINFO announcements are malformed")
                for index, row in enumerate(rows):
                    record = self._map_row(
                        row, column=column, collected_at=collected_at,
                        pit_mode=pit_mode, index=index,
                    )
                    if requested_symbols and record.symbol not in requested_symbols:
                        continue
                    output.setdefault(record.announcement_id, record)
                total_pages = _positive_int(payload.get("totalpages", 0), "totalpages")
                has_more = bool(payload.get("hasMore")) or page < total_pages
                if not has_more:
                    break
                if page >= self._max_pages:
                    raise ProviderResponseError(
                        "CNINFO result exceeds configured max_pages; refusing partial data"
                    )
                page += 1
        return sorted(output.values(), key=lambda item: (item.published_at, item.announcement_id))

    def _query_page(
        self, column: str, page: int, start_date: date, end_date: date
    ) -> Mapping[str, Any]:
        form = {
            "pageNum": str(page), "pageSize": str(self._page_size),
            "column": column, "tabName": "fulltext", "plate": "",
            "stock": "", "searchkey": "", "secid": "", "category": "",
            "trade": "", "seDate": f"{start_date.isoformat()}~{end_date.isoformat()}",
            "sortName": "", "sortType": "", "isHLtitle": "true",
        }
        try:
            payload = self._transport(CNINFO_QUERY_URL, form, self._timeout)
        except ProviderTransportError:
            raise
        except Exception as exc:
            raise ProviderTransportError("CNINFO announcement request failed") from exc
        if not isinstance(payload, Mapping):
            raise ProviderPayloadError("CNINFO response is malformed")
        return payload

    @staticmethod
    def _map_row(
        row: Any,
        *,
        column: str,
        collected_at: datetime,
        pit_mode: str,
        index: int,
    ) -> AnnouncementRecord:
        if not isinstance(row, Mapping):
            raise ProviderPayloadError(f"CNINFO row {index} is malformed")
        try:
            code = str(row["secCode"]).strip()
            if not re.fullmatch(r"\d{6}", code):
                raise ValueError("invalid security code")
            symbol = code + _COLUMN_SUFFIXES[column]
            company = str(row["secName"]).strip()
            title = _clean_title(str(row["announcementTitle"]))
            published_at = datetime.fromtimestamp(
                int(row["announcementTime"]) / 1000,
                tz=MARKET_TIMEZONE,
            )
            path = str(row.get("adjunctUrl", "")).strip().lstrip("/")
            url = urllib.parse.urljoin(CNINFO_PDF_BASE, path) if path else None
            native_id = str(row.get("announcementId", "")).strip()
            identity = native_id or stable_announcement_id(
                "cninfo", symbol, published_at, title, url
            )
            return AnnouncementRecord(
                announcement_id=identity, symbol=symbol,
                company_name=company, title=title, url=url,
                published_at=published_at, collected_at=collected_at,
                available_at=max(published_at, collected_at),
                source="cninfo", provider="cninfo",
                provider_record_id=identity, pit_mode=pit_mode,
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ProviderPayloadError(f"CNINFO row {index} is invalid") from exc


def _public_json_post(
    url: str, form: Mapping[str, str], timeout_seconds: float
) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json",
            "User-Agent": "QuantOS/0.1 (+official-public-disclosure-client)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise ProviderTransportError("CNINFO announcement request failed") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProviderPayloadError("CNINFO response is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ProviderPayloadError("CNINFO response is malformed")
    return payload


def _clean_title(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _positive_int(value: Any, field: str) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ProviderPayloadError(f"CNINFO {field} is invalid") from exc
    if result < 0:
        raise ProviderPayloadError(f"CNINFO {field} is invalid")
    return result
