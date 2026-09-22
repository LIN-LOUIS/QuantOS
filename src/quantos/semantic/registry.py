"""Closed metric and dimension allowlists for semantic analytics."""

from __future__ import annotations

from .contracts import (
    AvailabilityStatus, DimensionDefinition, MetricDefinition,
    SemanticValidationError,
)


METRIC_REGISTRY_VERSION = "market-metrics:v1"
DIMENSION_REGISTRY_VERSION = "market-dimensions:v1"
_ALL_DIMENSIONS = ("market", "provider", "security", "trading_date")


class MetricRegistry:
    def __init__(self, definitions: tuple[MetricDefinition, ...]) -> None:
        self._definitions = {item.metric_id: item for item in definitions}
        if len(self._definitions) != len(definitions):
            raise SemanticValidationError("DUPLICATE_METRIC")

    @classmethod
    def default(cls) -> MetricRegistry:
        values = (
            ("open", "开盘价", "日线开盘价", "CNY", "decimal"),
            ("high", "最高价", "日线最高价", "CNY", "decimal"),
            ("low", "最低价", "日线最低价", "CNY", "decimal"),
            ("close", "收盘价", "日线收盘价", "CNY", "decimal"),
            ("volume", "成交量", "日线成交量", "shares", "integer"),
            ("price_change", "价格变动", "收盘价减前收盘价", "CNY", "decimal"),
            ("return", "涨跌幅", "相对前收盘价的百分比变动", "percent", "decimal"),
        )
        return cls(tuple(MetricDefinition(
            metric_id, display, description, unit, value_type,
            _ALL_DIMENSIONS, "MARKET_EVENT_TIME_WITH_AVAILABILITY_CUTOFF",
            "market.history", AvailabilityStatus.READY,
        ) for metric_id, display, description, unit, value_type in values))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def definitions(self) -> tuple[MetricDefinition, ...]:
        return tuple(self._definitions[name] for name in self.names())

    def get(self, metric_id: str) -> MetricDefinition:
        try:
            return self._definitions[metric_id]
        except (KeyError, TypeError):
            raise SemanticValidationError("UNKNOWN_METRIC") from None


class DimensionRegistry:
    def __init__(self, definitions: tuple[DimensionDefinition, ...]) -> None:
        self._definitions = {item.dimension_id: item for item in definitions}
        if len(self._definitions) != len(definitions):
            raise SemanticValidationError("DUPLICATE_DIMENSION")

    @classmethod
    def default(cls) -> DimensionRegistry:
        values = (
            ("security", "证券", "canonical security symbol", "string",
             "IDENTITY_EFFECTIVE_TIME"),
            ("trading_date", "交易日", "market event date", "date",
             "MARKET_EVENT_TIME"),
            ("market", "市场", "bounded market identifier", "string",
             "STATIC_CLASSIFICATION"),
            ("provider", "数据源", "normalized record source", "string",
             "SOURCE_LINEAGE"),
        )
        return cls(tuple(DimensionDefinition(
            dimension_id, display, description, value_type, semantics,
            "market.history", AvailabilityStatus.READY,
        ) for dimension_id, display, description, value_type, semantics in values))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def definitions(self) -> tuple[DimensionDefinition, ...]:
        return tuple(self._definitions[name] for name in self.names())

    def get(self, dimension_id: str) -> DimensionDefinition:
        try:
            return self._definitions[dimension_id]
        except (KeyError, TypeError):
            raise SemanticValidationError("UNKNOWN_DIMENSION") from None
