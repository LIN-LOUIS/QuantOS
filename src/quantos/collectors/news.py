"""Independent news/announcement provider contracts and Tushare adapters."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping, Protocol, Sequence

from quantos.collectors.base import (
    ProviderConfigurationError,
    ProviderPayloadError,
    ProviderTransportError,
)
from quantos.collectors.symbols import tushare_to_canonical
from quantos.config import MARKET_TIMEZONE
from quantos.news import stable_news_id
from quantos.schemas import AnnouncementRecord, NewsRecord
from quantos.schemas._validation import require_aware


class NewsProvider(Protocol):
    def fetch_news(
        self, *, start_time: datetime, end_time: datetime, source: str,
        pit_mode: str = "strict_live",
    ) -> list[NewsRecord]: ...


class AnnouncementProvider(Protocol):
    def fetch_announcements(
        self, *, start_date: date, end_date: date,
        pit_mode: str = "strict_live",
    ) -> list[AnnouncementRecord]: ...


class TushareNewsClient(Protocol):
    def news(self, **kwargs: str) -> Any: ...
    def anns_d(self, **kwargs: str) -> Any: ...


class TushareNewsProvider:
    def __init__(
        self,
        *,
        client: TushareNewsClient | None = None,
        environ: Mapping[str, str] | None = None,
        client_factory: Callable[[str], TushareNewsClient] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        environment = os.environ if environ is None else environ
        token = environment.get("TUSHARE_TOKEN", "").strip()
        if not token:
            raise ProviderConfigurationError("TUSHARE_TOKEN is not configured")
        self._clock = clock or (lambda: datetime.now(MARKET_TIMEZONE))
        if client is not None:
            self._client = client
            return
        try:
            self._client = (client_factory or _default_client_factory)(token)
        except ProviderConfigurationError:
            raise
        except Exception as exc:
            raise ProviderTransportError("Tushare news client initialization failed") from exc

    def fetch_news(
        self, *, start_time: datetime, end_time: datetime, source: str,
        pit_mode: str = "strict_live",
    ) -> list[NewsRecord]:
        _validate_request_times(start_time, end_time, pit_mode)
        collected_at = self._clock()
        require_aware(collected_at, "collected_at")
        try:
            response = self._client.news(
                src=source,
                start_date=start_time.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=end_time.strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception as exc:
            raise ProviderTransportError("Tushare news request failed") from exc
        output: list[NewsRecord] = []
        for index, row in enumerate(_rows(response)):
            try:
                published = _parse_datetime(row.get("datetime"))
                title = str(row["title"]).strip()
                content = str(row.get("content", ""))
                row_source = str(row.get("src", source)).strip() or source
                identity = stable_news_id(row_source, published, title, content)
                channels = tuple(
                    value.strip() for value in str(row.get("channels", "")).split(",")
                    if value.strip()
                )
                output.append(NewsRecord(
                    news_id=identity, source=row_source, source_type="news",
                    published_at=published, collected_at=collected_at,
                    available_at=max(published, collected_at), title=title,
                    content=content, url=None, channels=channels,
                    provider="tushare", provider_record_id=identity,
                    pit_mode=pit_mode,
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderPayloadError(f"Tushare news row {index} is invalid") from exc
        return sorted(output, key=lambda item: (item.published_at, item.news_id))

    def fetch_announcements(
        self, *, start_date: date, end_date: date,
        pit_mode: str = "strict_live",
    ) -> list[AnnouncementRecord]:
        _validate_pit_mode(pit_mode)
        if start_date > end_date:
            raise ValueError("start_date cannot be after end_date")
        collected_at = self._clock()
        require_aware(collected_at, "collected_at")
        output: list[AnnouncementRecord] = []
        cursor = start_date
        while cursor <= end_date:
            try:
                response = self._client.anns_d(ann_date=cursor.strftime("%Y%m%d"))
            except Exception as exc:
                raise ProviderTransportError("Tushare anns_d request failed") from exc
            for index, row in enumerate(_rows(response)):
                try:
                    symbol = tushare_to_canonical(str(row["ts_code"]))
                    company = str(row["name"]).strip()
                    title = str(row["title"]).strip()
                    published = _parse_announcement_time(row, cursor)
                    source = "tushare_anns_d"
                    identity = stable_news_id(source, published, title, symbol)
                    output.append(AnnouncementRecord(
                        announcement_id=identity, symbol=symbol,
                        company_name=company, title=title,
                        url=_optional_text(row.get("url")), published_at=published,
                        collected_at=collected_at,
                        available_at=max(published, collected_at), source=source,
                        provider="tushare", provider_record_id=identity,
                        pit_mode=pit_mode,
                    ))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ProviderPayloadError(f"Tushare anns_d row {index} is invalid") from exc
            cursor += timedelta(days=1)
        return sorted(output, key=lambda item: (item.published_at, item.announcement_id))


def _default_client_factory(token: str) -> TushareNewsClient:
    try:
        import tushare  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ProviderConfigurationError("tushare SDK is not installed") from exc
    return tushare.pro_api(token)


def _rows(response: Any) -> list[Mapping[str, Any]]:
    if response is None:
        raise ProviderPayloadError("Tushare evidence response is missing")
    if isinstance(response, list):
        rows = response
    elif hasattr(response, "to_dict"):
        try:
            rows = response.to_dict("records")
        except Exception as exc:
            raise ProviderPayloadError("Tushare evidence response is malformed") from exc
    else:
        raise ProviderPayloadError("Tushare evidence response is malformed")
    if not isinstance(rows, list) or not all(isinstance(item, Mapping) for item in rows):
        raise ProviderPayloadError("Tushare evidence rows are malformed")
    return rows


def _parse_datetime(value: Any) -> datetime:
    text = str(value).strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=MARKET_TIMEZONE)
        except ValueError:
            continue
    raise ValueError("datetime is invalid")


def _parse_announcement_time(row: Mapping[str, Any], fallback: date) -> datetime:
    rec_time = _optional_text(row.get("rec_time"))
    if rec_time:
        return _parse_datetime(rec_time)
    ann_date = str(row.get("ann_date", fallback.strftime("%Y%m%d")))
    return datetime.strptime(ann_date, "%Y%m%d").replace(tzinfo=MARKET_TIMEZONE)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _validate_request_times(start_time: datetime, end_time: datetime, pit_mode: str) -> None:
    require_aware(start_time, "start_time")
    require_aware(end_time, "end_time")
    _validate_pit_mode(pit_mode)
    if start_time > end_time:
        raise ValueError("start_time cannot be after end_time")


def _validate_pit_mode(pit_mode: str) -> None:
    if pit_mode not in {"strict_live", "source_timestamp_proxy"}:
        raise ValueError("pit_mode is invalid")
