"""Tushare Pro daily adapter with canonical unit and PIT conversion."""

from __future__ import annotations

import os
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from quantos.config import MARKET_TIMEZONE
from quantos.schemas import MoneyFlowRecord, SecurityMaster
from quantos.schemas._validation import require_aware

from .base import (
    ProviderConfigurationError,
    ProviderPayloadError,
    ProviderTransportError,
    RawMarketRecord,
)
from .symbols import canonical_to_tushare, tushare_to_canonical


class TushareProClient(Protocol):
    def daily(self, **kwargs: str) -> Any: ...
    def stock_basic(self, **kwargs: str) -> Any: ...
    def trade_cal(self, **kwargs: str) -> Any: ...
    def moneyflow(self, **kwargs: str) -> Any: ...


class TushareMarketCollector:
    """Collect unadjusted Tushare daily bars into canonical raw fields."""

    _AVAILABLE_TIME = time(18, 0)
    _REQUIRED_FIELDS = {
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "change",
        "pct_chg",
        "vol",
        "amount",
    }
    _STOCK_BASIC_FIELDS = (
        "ts_code",
        "symbol",
        "name",
        "area",
        "industry",
        "market",
        "exchange",
        "list_status",
        "list_date",
        "delist_date",
    )
    _EXCHANGES = {"SSE": "SHSE", "SZSE": "SZSE", "BSE": "BSE"}

    def __init__(
        self,
        *,
        client: TushareProClient | None = None,
        environ: Mapping[str, str] | None = None,
        client_factory: Callable[[str], TushareProClient] | None = None,
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
        factory = client_factory or _default_client_factory
        try:
            self._client = factory(token)
        except ProviderConfigurationError:
            raise
        except Exception as exc:
            raise ProviderTransportError(
                "Tushare client initialization failed"
            ) from exc

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
        if frequency != "1d":
            raise ValueError("TushareMarketCollector supports only 1d frequency")

        received_at = self._clock()
        require_aware(received_at, "received_at")
        records: list[RawMarketRecord] = []
        for requested_symbol in symbols:
            ts_code = canonical_to_tushare(requested_symbol)
            try:
                response = self._client.daily(
                    ts_code=ts_code,
                    start_date=start_time.strftime("%Y%m%d"),
                    end_date=end_time.strftime("%Y%m%d"),
                )
            except Exception as exc:
                raise ProviderTransportError("Tushare daily request failed") from exc
            for index, row in enumerate(_response_rows(response)):
                records.append(
                    self._decode_row(
                        row=row,
                        index=index,
                        requested_symbol=requested_symbol,
                        received_at=received_at,
                        start_time=start_time,
                        end_time=end_time,
                        as_of_time=as_of_time,
                    )
                )
        return records

    def fetch_trade_dates(
        self, *, start_date: date, end_date: date, as_of_time: datetime
    ) -> list[date]:
        """Return open SSE dates in ascending order for deterministic backfill."""

        require_aware(as_of_time, "as_of_time")
        if start_date > end_date:
            raise ValueError("start_date cannot be after end_date")
        try:
            response = self._client.trade_cal(
                exchange="SSE",
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
                is_open="1",
                fields="cal_date,is_open",
            )
        except Exception as exc:
            raise ProviderTransportError("Tushare trade_cal request failed") from exc
        result: set[date] = set()
        for index, row in enumerate(_response_rows(response)):
            if "cal_date" not in row or "is_open" not in row:
                raise ProviderPayloadError(
                    f"Tushare trade_cal row {index} is missing fields"
                )
            try:
                trade_date = datetime.strptime(
                    str(row["cal_date"]), "%Y%m%d"
                ).date()
            except ValueError as exc:
                raise ProviderPayloadError(
                    f"Tushare trade_cal row {index} contains invalid values"
                ) from exc
            if str(row["is_open"]) != "1":
                continue
            available_at = datetime.combine(
                trade_date, self._AVAILABLE_TIME, tzinfo=MARKET_TIMEZONE
            )
            if start_date <= trade_date <= end_date and available_at <= as_of_time:
                result.add(trade_date)
        return sorted(result)

    def fetch_market_bars_by_trade_date(
        self, *, trade_date: date, as_of_time: datetime
    ) -> list[RawMarketRecord]:
        """Fetch one completed trade date for the whole SHSE/SZSE market."""

        require_aware(as_of_time, "as_of_time")
        available_at = datetime.combine(
            trade_date, self._AVAILABLE_TIME, tzinfo=MARKET_TIMEZONE
        )
        if available_at > as_of_time:
            raise ValueError("trade_date is not complete as of the run cutoff")
        received_at = self._clock()
        require_aware(received_at, "received_at")
        try:
            response = self._client.daily(trade_date=trade_date.strftime("%Y%m%d"))
        except Exception as exc:
            raise ProviderTransportError(
                "Tushare market-wide daily request failed"
            ) from exc
        start_time = datetime.combine(trade_date, time.min, tzinfo=MARKET_TIMEZONE)
        end_time = datetime.combine(trade_date, time.max, tzinfo=MARKET_TIMEZONE)
        records: list[RawMarketRecord] = []
        for index, row in enumerate(_response_rows(response)):
            requested_symbol = str(row.get("ts_code", ""))
            if requested_symbol.endswith(".BJ"):
                continue
            if not requested_symbol.endswith((".SH", ".SZ")):
                raise ProviderPayloadError(
                    f"Tushare daily row {index} has an unsupported symbol"
                )
            records.append(
                self._decode_row(
                    row=row,
                    index=index,
                    requested_symbol=requested_symbol,
                    received_at=received_at,
                    start_time=start_time,
                    end_time=end_time,
                    as_of_time=as_of_time,
                )
            )
        return sorted(records, key=lambda record: record.symbol)

    def fetch_listed_securities(
        self, *, as_of_time: datetime
    ) -> list[SecurityMaster]:
        """Fetch currently listed securities for universe cross-check only."""

        require_aware(as_of_time, "as_of_time")
        collected_at = self._clock()
        require_aware(collected_at, "collected_at")
        try:
            response = self._client.stock_basic(
                exchange="",
                list_status="L",
                fields=",".join(self._STOCK_BASIC_FIELDS),
            )
        except Exception as exc:
            raise ProviderTransportError("Tushare stock_basic request failed") from exc
        records = [
            self._decode_stock_basic_row(row, index, collected_at)
            for index, row in enumerate(_response_rows(response))
        ]
        return sorted(records, key=lambda record: record.symbol)

    def _decode_stock_basic_row(
        self,
        row: Mapping[str, Any],
        index: int,
        collected_at: datetime,
    ) -> SecurityMaster:
        missing = sorted(set(self._STOCK_BASIC_FIELDS).difference(row))
        if missing:
            raise ProviderPayloadError(
                f"Tushare stock_basic row {index} is missing fields: "
                f"{', '.join(missing)}"
            )
        try:
            symbol = tushare_to_canonical(str(row["ts_code"]))
            bare_symbol = str(row["symbol"])
            exchange = self._EXCHANGES[str(row["exchange"])]
            list_date = datetime.strptime(str(row["list_date"]), "%Y%m%d").date()
            raw_delist_date = _optional_stock_text(row["delist_date"])
            delist_date = (
                datetime.strptime(raw_delist_date, "%Y%m%d").date()
                if raw_delist_date
                else None
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderPayloadError(
                f"Tushare stock_basic row {index} contains invalid values"
            ) from exc
        if bare_symbol != symbol[:6]:
            raise ProviderPayloadError(
                f"Tushare stock_basic row {index} has inconsistent symbols"
            )
        expected_suffix = {"SHSE": ".SH", "SZSE": ".SZ", "BSE": ".BJ"}[exchange]
        if not symbol.endswith(expected_suffix):
            raise ProviderPayloadError(
                f"Tushare stock_basic row {index} has inconsistent exchange"
            )
        if str(row["list_status"]) != "L" or delist_date is not None:
            raise ProviderPayloadError(
                f"Tushare stock_basic row {index} is not currently listed"
            )
        industry = _optional_stock_text(row["industry"])
        try:
            return SecurityMaster(
                symbol=symbol,
                company_name=str(row["name"]),
                exchange=exchange,
                effective_from=list_date,
                effective_to=None,
                available_at=collected_at,
                is_active=True,
                source="tushare",
                asset_type="stock",
                status="listed",
                industry=industry,
                industry_classification=(
                    "Tushare stock_basic" if industry is not None else None
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ProviderPayloadError(
                f"Tushare stock_basic row {index} contains invalid values"
            ) from exc

    def _decode_row(
        self,
        *,
        row: Mapping[str, Any],
        index: int,
        requested_symbol: str,
        received_at: datetime,
        start_time: datetime,
        end_time: datetime,
        as_of_time: datetime,
    ) -> RawMarketRecord:
        missing = sorted(self._REQUIRED_FIELDS.difference(row))
        if missing:
            raise ProviderPayloadError(
                f"Tushare daily row {index} is missing fields: {', '.join(missing)}"
            )
        try:
            symbol = tushare_to_canonical(str(row["ts_code"]))
            trade_date = datetime.strptime(str(row["trade_date"]), "%Y%m%d").date()
            timestamp = datetime.combine(
                trade_date, time(15, 0), tzinfo=MARKET_TIMEZONE
            )
            available_at = datetime.combine(
                trade_date, self._AVAILABLE_TIME, tzinfo=MARKET_TIMEZONE
            )
            volume = _scaled_integer(row["vol"], Decimal("100"), "vol")
            amount = _scaled_decimal(row["amount"], Decimal("1000"), "amount")
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ProviderPayloadError(
                f"Tushare daily row {index} contains invalid values"
            ) from exc
        if symbol != requested_symbol:
            raise ProviderPayloadError(
                f"Tushare daily row {index} returned an unexpected symbol"
            )
        if timestamp < start_time or timestamp > end_time:
            raise ProviderPayloadError(
                f"Tushare daily row {index} is outside the requested window"
            )
        if available_at > as_of_time:
            raise ProviderPayloadError(
                f"Tushare daily row {index} is not available as of the run cutoff"
            )
        return RawMarketRecord(
            provider="tushare",
            symbol=symbol,
            provider_record_id=f"tushare:{symbol}:1d:{trade_date.isoformat()}",
            received_at=received_at,
            fields={
                "timestamp": trade_date.isoformat(),
                "available_at": available_at.isoformat(),
                "frequency": "1d",
                "open": str(row["open"]),
                "high": str(row["high"]),
                "low": str(row["low"]),
                "close": str(row["close"]),
                "prev_close": str(row["pre_close"]),
                "volume": str(volume),
                "amount": str(amount),
                "change": str(row["change"]),
                "pct_chg": str(row["pct_chg"]),
            },
        )


class TushareMoneyFlowCollector:
    """Collect market-wide Tushare moneyflow with conversion at the boundary."""

    _AVAILABLE_TIME = time(19, 0)
    _FLOW_FIELDS = (
        "buy_sm_vol",
        "buy_sm_amount",
        "sell_sm_vol",
        "sell_sm_amount",
        "buy_md_vol",
        "buy_md_amount",
        "sell_md_vol",
        "sell_md_amount",
        "buy_lg_vol",
        "buy_lg_amount",
        "sell_lg_vol",
        "sell_lg_amount",
        "buy_elg_vol",
        "buy_elg_amount",
        "sell_elg_vol",
        "sell_elg_amount",
        "net_mf_vol",
        "net_mf_amount",
    )

    def __init__(
        self,
        *,
        client: TushareProClient | None = None,
        environ: Mapping[str, str] | None = None,
        client_factory: Callable[[str], TushareProClient] | None = None,
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
        factory = client_factory or _default_client_factory
        try:
            self._client = factory(token)
        except ProviderConfigurationError:
            raise
        except Exception as exc:
            raise ProviderTransportError(
                "Tushare client initialization failed"
            ) from exc

    def fetch_by_trade_date(
        self, *, trade_date: date, as_of_time: datetime
    ) -> list[MoneyFlowRecord]:
        require_aware(as_of_time, "as_of_time")
        available_at = datetime.combine(
            trade_date, self._AVAILABLE_TIME, tzinfo=MARKET_TIMEZONE
        )
        if available_at > as_of_time:
            raise ValueError("moneyflow is not available as of the run cutoff")
        collected_at = self._clock()
        require_aware(collected_at, "collected_at")
        try:
            response = self._client.moneyflow(
                trade_date=trade_date.strftime("%Y%m%d")
            )
        except Exception as exc:
            raise ProviderTransportError("Tushare moneyflow request failed") from exc
        records: list[MoneyFlowRecord] = []
        required = {"ts_code", "trade_date", *self._FLOW_FIELDS}
        for index, row in enumerate(_response_rows(response)):
            missing = sorted(required.difference(row))
            if missing:
                raise ProviderPayloadError(
                    f"Tushare moneyflow row {index} is missing fields: "
                    f"{', '.join(missing)}"
                )
            try:
                symbol = tushare_to_canonical(str(row["ts_code"]))
                row_date = datetime.strptime(str(row["trade_date"]), "%Y%m%d").date()
                if symbol.endswith(".BJ"):
                    continue
                if row_date != trade_date:
                    raise ValueError("unexpected trade date")
                values: dict[str, int | Decimal] = {}
                for field in self._FLOW_FIELDS:
                    if field.endswith("_vol"):
                        values[field] = _scaled_integer(
                            row[field], Decimal("100"), field
                        )
                    else:
                        values[field] = _scaled_decimal(
                            row[field], Decimal("10000"), field
                        )
                records.append(
                    MoneyFlowRecord(
                        symbol=symbol,
                        trade_date=trade_date,
                        buy_sm_volume=values["buy_sm_vol"],
                        buy_sm_amount=values["buy_sm_amount"],
                        sell_sm_volume=values["sell_sm_vol"],
                        sell_sm_amount=values["sell_sm_amount"],
                        buy_md_volume=values["buy_md_vol"],
                        buy_md_amount=values["buy_md_amount"],
                        sell_md_volume=values["sell_md_vol"],
                        sell_md_amount=values["sell_md_amount"],
                        buy_lg_volume=values["buy_lg_vol"],
                        buy_lg_amount=values["buy_lg_amount"],
                        sell_lg_volume=values["sell_lg_vol"],
                        sell_lg_amount=values["sell_lg_amount"],
                        buy_elg_volume=values["buy_elg_vol"],
                        buy_elg_amount=values["buy_elg_amount"],
                        sell_elg_volume=values["sell_elg_vol"],
                        sell_elg_amount=values["sell_elg_amount"],
                        net_mf_volume=values["net_mf_vol"],
                        net_mf_amount=values["net_mf_amount"],
                        source="tushare",
                        available_at=available_at,
                        collected_at=collected_at,
                    )
                )
            except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
                raise ProviderPayloadError(
                    f"Tushare moneyflow row {index} contains invalid values"
                ) from exc
        return sorted(records, key=lambda record: record.symbol)


def _default_client_factory(token: str) -> TushareProClient:
    try:
        import tushare  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ProviderConfigurationError("tushare SDK is not installed") from exc
    return tushare.pro_api(token)


def _response_rows(response: Any) -> list[Mapping[str, Any]]:
    if response is None:
        raise ProviderPayloadError("Tushare daily response is missing")
    if isinstance(response, list):
        rows = response
    elif hasattr(response, "to_dict"):
        try:
            rows = response.to_dict("records")
        except Exception as exc:
            raise ProviderPayloadError("Tushare daily response is malformed") from exc
    else:
        raise ProviderPayloadError("Tushare daily response is malformed")
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise ProviderPayloadError("Tushare daily response rows are malformed")
    return rows


def _scaled_decimal(value: Any, scale: Decimal, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value)) * scale
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def _scaled_integer(value: Any, scale: Decimal, field_name: str) -> int:
    scaled = _scaled_decimal(value, scale, field_name)
    result = int(scaled)
    if scaled != result:
        raise ValueError(f"{field_name} does not convert to whole shares")
    return result


def _optional_stock_text(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip()
    return text or None
