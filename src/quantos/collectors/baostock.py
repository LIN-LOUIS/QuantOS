"""BaoStock SecurityMaster adapter with contained login/logout lifecycle."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime, time
from typing import Any, Protocol

from quantos.config import MARKET_TIMEZONE
from quantos.schemas import SecurityMaster
from quantos.schemas._validation import require_aware

from .base import (
    ProviderConfigurationError,
    ProviderPayloadError,
    ProviderResponseError,
    ProviderTransportError,
)
from .symbols import baostock_to_canonical


class BaoStockClient(Protocol):
    def login(self) -> Any: ...
    def logout(self) -> Any: ...
    def query_stock_basic(self) -> Any: ...
    def query_stock_industry(self) -> Any: ...


class BaoStockSecurityMasterCollector:
    """Collect basic listing and provider-native industry attributes."""

    _ASSET_TYPES = {
        "1": "stock",
        "2": "index",
        "3": "other",
        "4": "convertible_bond",
        "5": "etf",
    }

    def __init__(
        self,
        *,
        client: BaoStockClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client or _default_client()
        self._clock = clock or (lambda: datetime.now(MARKET_TIMEZONE))

    def fetch_security_master(self, *, as_of_time: datetime) -> list[SecurityMaster]:
        require_aware(as_of_time, "as_of_time")
        collected_at = self._clock()
        require_aware(collected_at, "collected_at")
        with self._session():
            basic_rows = _query_rows(
                self._invoke_query("query_stock_basic"), "query_stock_basic"
            )
            industry_rows = _query_rows(
                self._invoke_query("query_stock_industry"),
                "query_stock_industry",
            )
        industry_by_code = {
            str(row.get("code", "")): row for row in industry_rows
        }
        records = [
            self._map_record(
                row,
                industry_by_code.get(str(row.get("code", ""))),
                collected_at,
            )
            for row in basic_rows
        ]
        return sorted(records, key=lambda record: record.symbol)

    def _invoke_query(self, operation: str) -> Any:
        try:
            return getattr(self._client, operation)()
        except Exception as exc:
            raise ProviderTransportError(f"BaoStock {operation} failed") from exc

    @contextmanager
    def _session(self) -> Iterator[None]:
        try:
            login_result = self._client.login()
        except Exception as exc:
            raise ProviderTransportError("BaoStock login failed") from exc
        _require_success(login_result, "BaoStock login")
        try:
            yield
        finally:
            active_exception = sys.exc_info()[0] is not None
            try:
                logout_result = self._client.logout()
                if not active_exception:
                    _require_success(logout_result, "BaoStock logout")
            except Exception as exc:
                if not active_exception:
                    if isinstance(exc, ProviderResponseError):
                        raise
                    raise ProviderTransportError("BaoStock logout failed") from exc

    def _map_record(
        self,
        basic: Mapping[str, Any],
        industry_row: Mapping[str, Any] | None,
        collected_at: datetime,
    ) -> SecurityMaster:
        required = {"code", "code_name", "ipoDate", "outDate", "type", "status"}
        missing = sorted(required.difference(basic))
        if missing:
            raise ProviderPayloadError(
                f"BaoStock basic row is missing fields: {', '.join(missing)}"
            )
        try:
            symbol = baostock_to_canonical(str(basic["code"]))
            list_date = _parse_date(basic["ipoDate"], required=True)
            delist_date = _parse_date(basic["outDate"], required=False)
            source_updated_at = (
                _parse_source_updated_at(industry_row.get("updateDate"))
                if industry_row is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise ProviderPayloadError("BaoStock SecurityMaster row is invalid") from exc
        raw_status = str(basic["status"])
        status = "listed" if raw_status == "1" and delist_date is None else "delisted"
        exchange = "SHSE" if symbol.endswith(".SH") else "SZSE"
        industry = _optional_text(industry_row, "industry")
        classification = _optional_text(industry_row, "industryClassification")
        try:
            return SecurityMaster(
                symbol=symbol,
                company_name=str(basic["code_name"]),
                exchange=exchange,
                effective_from=list_date,
                effective_to=delist_date,
                available_at=collected_at,
                is_active=status == "listed",
                source="baostock",
                asset_type=self._ASSET_TYPES.get(str(basic["type"]), "unknown"),
                status=status,
                industry=industry,
                industry_classification=classification,
                source_updated_at=source_updated_at,
            )
        except (TypeError, ValueError) as exc:
            raise ProviderPayloadError("BaoStock SecurityMaster row is invalid") from exc


def _default_client() -> BaoStockClient:
    try:
        import baostock  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ProviderConfigurationError("baostock SDK is not installed") from exc
    return baostock


def _require_success(result: Any, operation: str) -> None:
    if str(getattr(result, "error_code", "")) != "0":
        raise ProviderResponseError(f"{operation} returned a provider error")


def _query_rows(result: Any, operation: str) -> list[Mapping[str, str]]:
    _require_success(result, f"BaoStock {operation}")
    fields = getattr(result, "fields", None)
    if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
        raise ProviderPayloadError(f"BaoStock {operation} fields are malformed")
    rows: list[Mapping[str, str]] = []
    try:
        while result.next():
            values = result.get_row_data()
            if not isinstance(values, list) or len(values) != len(fields):
                raise ProviderPayloadError(f"BaoStock {operation} row is malformed")
            rows.append(dict(zip(fields, values)))
    except ProviderPayloadError:
        raise
    except Exception as exc:
        raise ProviderResponseError(f"BaoStock {operation} failed") from exc
    _require_success(result, f"BaoStock {operation}")
    return rows


def _parse_date(value: Any, *, required: bool) -> date | None:
    text = str(value).strip()
    if not text:
        if required:
            raise ValueError("date is required")
        return None
    return datetime.strptime(text, "%Y-%m-%d").date()


def _parse_source_updated_at(value: Any) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    parsed_date = datetime.strptime(str(value), "%Y-%m-%d").date()
    return datetime.combine(parsed_date, time.min, tzinfo=MARKET_TIMEZONE)


def _optional_text(row: Mapping[str, Any] | None, field: str) -> str | None:
    if row is None:
        return None
    value = str(row.get(field, "")).strip()
    return value or None
