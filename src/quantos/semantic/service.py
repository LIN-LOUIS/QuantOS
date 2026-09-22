"""Deterministic semantic planning and PIT-safe repository execution."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable
from uuid import uuid4

from quantos.config import DEFAULT_SETTINGS, MARKET_TIMEZONE, Settings
from quantos.storage import MarketDataRepository

from .contracts import (
    AvailabilityRecord, AvailabilityStatus, ChartSpec, InsightCard,
    ProvenanceBundle, SemanticQuery, SemanticQueryPlan, SemanticResult,
    SemanticStep, SemanticValidationError,
)
from .registry import (
    DIMENSION_REGISTRY_VERSION, METRIC_REGISTRY_VERSION,
    DimensionRegistry, MetricRegistry,
)


class SemanticService:
    def __init__(
        self, *, settings: Settings = DEFAULT_SETTINGS,
        market_repository=None, metric_registry: MetricRegistry | None = None,
        dimension_registry: DimensionRegistry | None = None,
        data_available: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.market = market_repository or MarketDataRepository(settings, read_only=True)
        self.metrics = metric_registry or MetricRegistry.default()
        self.dimensions = dimension_registry or DimensionRegistry.default()
        self._data_available = data_available or self._market_data_available
        self._clock = clock or (lambda: datetime.now(MARKET_TIMEZONE))

    def schema(self) -> dict[str, object]:
        """Describe only the closed semantic contract exposed to clients."""

        return {
            "metrics": [item.to_dict() for item in self.metrics.definitions()],
            "dimensions": [item.to_dict() for item in self.dimensions.definitions()],
            "query_limits": {
                "max_metrics": 5,
                "max_dimensions": 4,
                "max_entities": 10,
                "max_filters": 10,
                "max_sorts": 5,
                "max_rows": 1000,
                "max_date_range_days": 366,
            },
            "metric_registry_version": METRIC_REGISTRY_VERSION,
            "dimension_registry_version": DIMENSION_REGISTRY_VERSION,
        }

    def plan(self, query: SemanticQuery) -> SemanticQueryPlan:
        if not isinstance(query, SemanticQuery):
            raise SemanticValidationError("SEMANTIC_QUERY_REQUIRED")
        cutoff = query.as_of_time or self._clock()
        if cutoff.utcoffset() is None:
            raise SemanticValidationError("TIMEZONE_REQUIRED")
        if query.time_range.end > cutoff.astimezone(MARKET_TIMEZONE).date():
            raise SemanticValidationError("PIT_BOUNDARY_VIOLATION")
        metric_definitions = tuple(self.metrics.get(item) for item in query.metrics)
        tuple(self.dimensions.get(item) for item in query.dimensions)
        for definition in metric_definitions:
            if not set(query.dimensions) <= set(definition.supported_dimensions):
                raise SemanticValidationError("UNSUPPORTED_METRIC_DIMENSION")
        availability = "READY" if self._data_available() else "UNAVAILABLE"
        return SemanticQueryPlan(
            query.query_id, query.metrics, query.dimensions, cutoff,
            (SemanticStep(
                "market.history", query.entities, query.time_range.start,
                query.time_range.end,
            ),), METRIC_REGISTRY_VERSION, DIMENSION_REGISTRY_VERSION,
            (("market.history", availability),),
        )

    def execute(self, query: SemanticQuery) -> SemanticResult:
        plan = self.plan(query)
        ready = dict(plan.capability_availability)["market.history"] == "READY"
        if not ready:
            return self._empty_result(
                query, plan, status="PARTIAL",
                availability=AvailabilityRecord(
                    AvailabilityStatus.UNAVAILABLE, None, "MARKET_DATA_UNAVAILABLE",
                ), limitations=("Persisted market history is unavailable.",),
                reason_codes=("MARKET_DATA_UNAVAILABLE",),
            )
        bars = []
        for symbol in query.entities:
            values = self.market.read_by_symbol(
                symbol, as_of_time=plan.as_of_time,
                start_date=query.time_range.start, end_date=query.time_range.end,
            )
            for item in values:
                if (item.available_at > plan.as_of_time
                        or item.timestamp > plan.as_of_time
                        or not query.time_range.start <= item.timestamp.date() <= query.time_range.end):
                    raise SemanticValidationError("PIT_REJECTED")
                bars.append(item)
        rows = [(item, self._row(item)) for item in bars]
        rows = [item for item in rows if self._matches_filters(item[1], query)]
        sort_fields = query.sort or tuple()
        if sort_fields:
            for spec in reversed(sort_fields):
                rows.sort(key=lambda item, field=spec.field: _sort_value(item[1].get(field)),
                          reverse=spec.direction == "DESC")
        else:
            rows.sort(key=lambda item: tuple(_sort_value(item[1].get(key))
                                             for key in query.dimensions))
        selected = rows[:query.limit]
        output_fields = (*query.dimensions, *query.metrics)
        materialized = tuple({key: row[key] for key in output_fields}
                             for _bar, row in selected)
        references = tuple(sorted({item.source_record_id for item, _row in selected}))
        providers = tuple(sorted({item.source for item, _row in selected}))
        provenance = ProvenanceBundle(
            references, providers, METRIC_REGISTRY_VERSION,
            DIMENSION_REGISTRY_VERSION, plan.as_of_time, ("market.history",),
        )
        chart = self._chart(query)
        limitations = ()
        reasons = ("OK",) if materialized else ("NO_DATA",)
        summary = (
            f"Returned {len(materialized)} PIT-visible market rows."
            if materialized else
            "Market history is available, but no records matched this query."
        )
        insight = InsightCard(
            "QuantOS Market Analytics", summary, query.metrics, None,
            references, limitations,
        )
        return SemanticResult(
            query.query_id, plan.plan_hash, uuid4().hex, "PASS",
            self._schema(query), materialized, query.metrics, query.dimensions,
            plan.as_of_time,
            {"market_data": AvailabilityRecord(
                AvailabilityStatus.READY, len(materialized), None,
            )}, limitations, reasons, provenance, insight, chart,
        )

    def _empty_result(
        self, query: SemanticQuery, plan: SemanticQueryPlan, *, status: str,
        availability: AvailabilityRecord, limitations: tuple[str, ...],
        reason_codes: tuple[str, ...],
    ) -> SemanticResult:
        provenance = ProvenanceBundle(
            (), (), METRIC_REGISTRY_VERSION, DIMENSION_REGISTRY_VERSION,
            plan.as_of_time, ("market.history",),
        )
        insight = InsightCard(
            "QuantOS Market Analytics", "Market data is unavailable for this query.",
            query.metrics, None, (), limitations,
        )
        return SemanticResult(
            query.query_id, plan.plan_hash, uuid4().hex, status,
            self._schema(query), (), query.metrics, query.dimensions,
            plan.as_of_time, {"market_data": availability}, limitations,
            reason_codes, provenance, insight, self._chart(query),
        )

    def _row(self, item) -> dict[str, object]:
        dimensions = {
            "security": item.symbol,
            "trading_date": item.timestamp.date().isoformat(),
            "market": "A_SHARE",
            "provider": item.source,
        }
        change = item.close - item.prev_close if item.prev_close is not None else None
        percent = (
            ((item.close / item.prev_close - Decimal(1)) * 100).quantize(Decimal("0.01"))
            if item.prev_close not in {None, Decimal(0)} else None
        )
        metrics = {
            "open": _decimal(item.open), "high": _decimal(item.high),
            "low": _decimal(item.low), "close": _decimal(item.close),
            "volume": item.volume,
            "price_change": _decimal(change) if change is not None else None,
            "return": _decimal(percent) if percent is not None else None,
        }
        return {**dimensions, **metrics}

    @staticmethod
    def _matches_filters(row: dict[str, object], query: SemanticQuery) -> bool:
        return all(str(row.get(item.dimension)) == item.value for item in query.filters)

    def _schema(self, query: SemanticQuery) -> tuple[dict[str, object], ...]:
        dimensions = tuple({
            "name": item, "kind": "dimension",
            "value_type": self.dimensions.get(item).value_type,
        } for item in query.dimensions)
        metrics = tuple({
            "name": item, "kind": "metric",
            "value_type": self.metrics.get(item).value_type,
            "unit": self.metrics.get(item).unit,
        } for item in query.metrics)
        return dimensions + metrics

    def _chart(self, query: SemanticQuery) -> ChartSpec:
        if "trading_date" in query.dimensions:
            chart_type, x = "line", "trading_date"
        elif len(query.dimensions) == 1 and len(query.metrics) == 1:
            chart_type, x = "bar", query.dimensions[0]
        else:
            chart_type, x = "table", query.dimensions[0] if query.dimensions else None
        units = {self.metrics.get(item).unit for item in query.metrics}
        return ChartSpec(
            chart_type, x, query.metrics, None, "QuantOS Market Analytics",
            next(iter(units)) if len(units) == 1 else None,
        )

    def _market_data_available(self) -> bool:
        root: Path = self.settings.normalized_market_dir
        return root.is_dir() and any(root.rglob("*.parquet"))


def _decimal(value: Decimal) -> str:
    return format(value, "f")


def _sort_value(value):
    return (value is None, value)
