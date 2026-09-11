"""Deterministic raw-market normalization."""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from quantos.collectors import RawMarketRecord
from quantos.config import MARKET_TIMEZONE
from quantos.schemas import MarketBar


class MarketNormalizationError(ValueError):
    """A raw provider record cannot be converted without guessing."""


class MarketNormalizer:
    def __init__(self, market_timezone: ZoneInfo = MARKET_TIMEZONE) -> None:
        self.market_timezone = market_timezone

    def normalize(self, record: RawMarketRecord) -> MarketBar:
        if record.provider not in {"eastmoney", "tushare"}:
            raise MarketNormalizationError(
                f"unsupported market data provider: {record.provider}"
            )
        fields = record.fields
        required = {
            "timestamp",
            "frequency",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
        }
        missing = sorted(required.difference(fields))
        if missing:
            raise MarketNormalizationError(
                f"raw record is missing fields: {', '.join(missing)}"
            )

        frequency = str(fields["frequency"])
        timestamp = self._timestamp(fields["timestamp"], frequency)
        available_at = (
            self._aware_datetime(fields.get("available_at"), "available_at")
            if record.provider == "tushare"
            else timestamp
        )
        return MarketBar(
            symbol=record.symbol,
            timestamp=timestamp,
            frequency=frequency,
            open=self._decimal(fields["open"], "open"),
            high=self._decimal(fields["high"], "high"),
            low=self._decimal(fields["low"], "low"),
            close=self._decimal(fields["close"], "close"),
            volume=self._integer(fields["volume"], "volume"),
            amount=self._decimal(fields["amount"], "amount"),
            prev_close=(
                self._decimal(fields["prev_close"], "prev_close")
                if fields.get("prev_close") is not None
                else None
            ),
            source=record.provider,
            source_record_id=record.provider_record_id,
            collected_at=record.received_at,
            # Eastmoney timestamps are completed interval endpoints. Tushare
            # daily rows carry the adapter's conservative post-close cutoff.
            available_at=available_at,
        )

    def normalize_many(self, records: list[RawMarketRecord]) -> list[MarketBar]:
        return [self.normalize(record) for record in records]

    def _timestamp(self, value: Any, frequency: str) -> datetime:
        if not isinstance(value, str):
            raise MarketNormalizationError("timestamp must be a provider string")
        try:
            parsed = datetime.strptime(
                value,
                "%Y-%m-%d %H:%M" if " " in value else "%Y-%m-%d",
            )
        except ValueError as exc:
            raise MarketNormalizationError(f"invalid timestamp: {value}") from exc
        if frequency == "1d" and " " not in value:
            parsed = datetime.combine(parsed.date(), time(15, 0))
        return parsed.replace(tzinfo=self.market_timezone)

    @staticmethod
    def _aware_datetime(value: Any, field_name: str) -> datetime:
        if not isinstance(value, str):
            raise MarketNormalizationError(f"{field_name} must be an ISO datetime")
        try:
            result = datetime.fromisoformat(value)
        except ValueError as exc:
            raise MarketNormalizationError(
                f"{field_name} must be an ISO datetime"
            ) from exc
        if result.tzinfo is None or result.utcoffset() is None:
            raise MarketNormalizationError(f"{field_name} must be timezone-aware")
        return result

    @staticmethod
    def _decimal(value: Any, field_name: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise MarketNormalizationError(
                f"{field_name} must be numeric"
            ) from exc
        if not result.is_finite():
            raise MarketNormalizationError(f"{field_name} must be finite")
        return result

    @staticmethod
    def _integer(value: Any, field_name: str) -> int:
        try:
            decimal_value = Decimal(str(value))
            result = int(decimal_value)
        except (InvalidOperation, ValueError, OverflowError) as exc:
            raise MarketNormalizationError(
                f"{field_name} must be an integer"
            ) from exc
        if decimal_value != result:
            raise MarketNormalizationError(f"{field_name} must be an integer")
        return result
