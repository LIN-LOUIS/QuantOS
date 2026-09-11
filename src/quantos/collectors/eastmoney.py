"""Eastmoney A-share historical K-line adapter.

Provider field names are deliberately decoded here and never exposed through
the normalized business schema.
"""

from __future__ import annotations

import json
import logging
import random
import socket
import time
from http.client import RemoteDisconnected
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from quantos.config import MARKET_TIMEZONE
from quantos.schemas._validation import require_aware

from .base import (
    ProviderPayloadError,
    ProviderResponseError,
    ProviderTransportError,
    RawMarketRecord,
)

EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


class JsonTransport(Protocol):
    def get_json(self, url: str, params: Mapping[str, str]) -> Mapping[str, Any]: ...


class UrllibJsonTransport:
    _MAX_ATTEMPTS = 3
    _BASE_DELAY_SECONDS = 0.25
    _RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self,
        timeout_seconds: float = 10.0,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._sleeper = sleeper
        self._jitter = jitter or (lambda: random.uniform(0.0, 0.10))
        self._logger = logger or logging.getLogger("quantos.collectors.eastmoney")

    def get_json(self, url: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        request = Request(
            f"{url}?{urlencode(params)}",
            headers={"User-Agent": "QuantOS/0.1 market-data collector"},
        )
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    if not 200 <= response.status < 300:
                        if response.status in self._RETRYABLE_HTTP_STATUS:
                            exc = HTTPError(
                                request.full_url,
                                response.status,
                                f"HTTP {response.status}",
                                response.headers,
                                None,
                            )
                            if attempt < self._MAX_ATTEMPTS:
                                self._retry(attempt, exc)
                                continue
                            raise ProviderTransportError(
                                "Eastmoney request failed after "
                                f"{self._MAX_ATTEMPTS} attempts"
                            ) from exc
                        raise ProviderTransportError(
                            f"Eastmoney returned HTTP {response.status}"
                        )
                    response_body = response.read()
            except HTTPError as exc:
                if exc.code not in self._RETRYABLE_HTTP_STATUS:
                    raise ProviderTransportError(
                        f"Eastmoney returned HTTP {exc.code}"
                    ) from exc
                if attempt < self._MAX_ATTEMPTS:
                    self._retry(attempt, exc)
                    continue
                raise ProviderTransportError(
                    f"Eastmoney request failed after {self._MAX_ATTEMPTS} attempts"
                ) from exc
            except (
                RemoteDisconnected,
                URLError,
                TimeoutError,
                ConnectionResetError,
                ConnectionRefusedError,
                socket.timeout,
            ) as exc:
                if attempt < self._MAX_ATTEMPTS:
                    self._retry(attempt, exc)
                    continue
                raise ProviderTransportError(
                    f"Eastmoney request failed after {self._MAX_ATTEMPTS} attempts"
                ) from exc
            except OSError as exc:
                raise ProviderTransportError("Eastmoney request failed") from exc

            try:
                payload = json.loads(response_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProviderResponseError("Eastmoney returned invalid JSON") from exc
            if not isinstance(payload, Mapping):
                raise ProviderResponseError("Eastmoney JSON root must be an object")
            return payload
        raise AssertionError("unreachable Eastmoney retry state")

    def _retry(self, failed_attempt: int, exc: BaseException) -> None:
        delay = self._BASE_DELAY_SECONDS * (2 ** (failed_attempt - 1))
        delay += max(0.0, self._jitter())
        try:
            self._logger.warning(
                "provider_retry",
                extra={
                    "provider": "eastmoney",
                    "attempt": failed_attempt + 1,
                    "max_attempts": self._MAX_ATTEMPTS,
                    "delay_ms": round(delay * 1000, 3),
                    "error_type": type(exc).__name__,
                },
            )
        except Exception:
            # Logging must not alter provider or financial pipeline behavior.
            pass
        self._sleeper(delay)


class EastmoneyMarketCollector:
    """Fetch A-share K-lines while keeping vendor details at the boundary."""

    _FREQUENCIES = {
        "1m": "1",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "60m": "60",
        "1d": "101",
    }

    def __init__(
        self,
        transport: JsonTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport = transport or UrllibJsonTransport()
        self._clock = clock or (lambda: datetime.now(MARKET_TIMEZONE))

    def fetch_bars(
        self,
        *,
        symbols: Sequence[str],
        frequency: str,
        start_time: datetime,
        end_time: datetime,
        as_of_time: datetime,
    ) -> list[RawMarketRecord]:
        for name, value in (
            ("start_time", start_time),
            ("end_time", end_time),
            ("as_of_time", as_of_time),
        ):
            require_aware(value, name)
        if start_time > end_time:
            raise ValueError("start_time cannot be after end_time")
        if end_time > as_of_time:
            raise ValueError("end_time cannot be after as_of_time")
        try:
            klt = self._FREQUENCIES[frequency]
        except KeyError as exc:
            raise ValueError(f"unsupported frequency: {frequency}") from exc

        records: list[RawMarketRecord] = []
        for symbol in symbols:
            secid = self._to_secid(symbol)
            payload = self._transport.get_json(
                EASTMONEY_KLINE_URL,
                {
                    "secid": secid,
                    "klt": klt,
                    "fqt": "0",
                    "beg": start_time.strftime("%Y%m%d"),
                    "end": end_time.strftime("%Y%m%d"),
                    "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                },
            )
            records.extend(
                self._decode_payload(
                    payload=payload,
                    symbol=symbol,
                    frequency=frequency,
                    received_at=self._clock(),
                    start_time=start_time,
                    end_time=end_time,
                )
            )
        return records

    @staticmethod
    def _to_secid(symbol: str) -> str:
        try:
            code, exchange = symbol.split(".")
        except ValueError as exc:
            raise ValueError(f"invalid canonical symbol: {symbol}") from exc
        if len(code) != 6 or not code.isdigit() or exchange not in {"SH", "SZ", "BJ"}:
            raise ValueError(f"invalid canonical symbol: {symbol}")
        return f"{'1' if exchange == 'SH' else '0'}.{code}"

    @staticmethod
    def _decode_payload(
        *,
        payload: Mapping[str, Any],
        symbol: str,
        frequency: str,
        received_at: datetime,
        start_time: datetime,
        end_time: datetime,
    ) -> list[RawMarketRecord]:
        require_aware(received_at, "received_at")
        if payload.get("rc") not in (0, "0", None):
            raise ProviderResponseError(
                f"Eastmoney provider error rc={payload.get('rc')}"
            )
        data = payload.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("klines"), list):
            raise ProviderPayloadError("Eastmoney response is missing data.klines")

        decoded: list[RawMarketRecord] = []
        for index, line in enumerate(data["klines"]):
            if not isinstance(line, str):
                raise ProviderPayloadError(f"kline {index} must be a string")
            values = line.split(",")
            if len(values) < 7:
                raise ProviderPayloadError(f"kline {index} has too few fields")
            timestamp_text = values[0]
            try:
                timestamp = datetime.strptime(
                    timestamp_text,
                    "%Y-%m-%d %H:%M" if " " in timestamp_text else "%Y-%m-%d",
                ).replace(tzinfo=end_time.tzinfo)
            except ValueError as exc:
                raise ProviderPayloadError(
                    f"kline {index} has invalid timestamp"
                ) from exc
            if timestamp < start_time or timestamp > end_time:
                continue
            decoded.append(
                RawMarketRecord(
                    provider="eastmoney",
                    symbol=symbol,
                    provider_record_id=f"{symbol}:{frequency}:{timestamp.isoformat()}",
                    received_at=received_at,
                    fields={
                        "timestamp": timestamp_text,
                        "frequency": frequency,
                        "open": values[1],
                        "close": values[2],
                        "high": values[3],
                        "low": values[4],
                        "volume": values[5],
                        "amount": values[6],
                    },
                )
            )
        return decoded
