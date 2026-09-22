"""Framework-neutral contracts for bounded semantic analytics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import hashlib
import re
from typing import Any, Mapping

from quantos.serialization import canonical_identity_json_bytes


_SYMBOL = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_QUERY_FIELDS = {
    "metrics", "dimensions", "entities", "time_range", "filters", "sort",
    "limit", "as_of_time",
}
_DIMENSIONS = frozenset({"security", "trading_date", "market", "provider"})


class SemanticValidationError(ValueError):
    """Stable, payload-free semantic validation failure."""


class AvailabilityStatus(str, Enum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class AvailabilityRecord:
    status: AvailabilityStatus
    record_count: int | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, AvailabilityStatus):
            raise SemanticValidationError("INVALID_AVAILABILITY")
        if self.record_count is not None and self.record_count < 0:
            raise SemanticValidationError("INVALID_AVAILABILITY")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "record_count": self.record_count,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    metric_id: str
    display_name: str
    description: str
    unit: str
    value_type: str
    supported_dimensions: tuple[str, ...]
    time_semantics: str
    source_capability: str
    availability: AvailabilityStatus
    version: str = "market-metrics:v1"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.metric_id):
            raise SemanticValidationError("INVALID_METRIC_DEFINITION")
        if self.source_capability != "market.history":
            raise SemanticValidationError("INVALID_METRIC_SOURCE")
        if not set(self.supported_dimensions) <= _DIMENSIONS:
            raise SemanticValidationError("INVALID_METRIC_DIMENSIONS")
        if not isinstance(self.availability, AvailabilityStatus):
            raise SemanticValidationError("INVALID_AVAILABILITY")

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "display_name": self.display_name,
            "description": self.description,
            "unit": self.unit,
            "value_type": self.value_type,
            "supported_dimensions": list(self.supported_dimensions),
            "time_semantics": self.time_semantics,
            "source_capability": self.source_capability,
            "availability": self.availability.value,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class DimensionDefinition:
    dimension_id: str
    display_name: str
    description: str
    value_type: str
    time_semantics: str
    source_capability: str
    availability: AvailabilityStatus
    version: str = "market-dimensions:v1"

    def __post_init__(self) -> None:
        if self.dimension_id not in _DIMENSIONS:
            raise SemanticValidationError("INVALID_DIMENSION_DEFINITION")
        if self.source_capability != "market.history":
            raise SemanticValidationError("INVALID_DIMENSION_SOURCE")
        if not isinstance(self.availability, AvailabilityStatus):
            raise SemanticValidationError("INVALID_AVAILABILITY")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension_id": self.dimension_id,
            "display_name": self.display_name,
            "description": self.description,
            "value_type": self.value_type,
            "time_semantics": self.time_semantics,
            "source_capability": self.source_capability,
            "availability": self.availability.value,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class SemanticTimeRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise SemanticValidationError("INVALID_TIME_RANGE")
        if (self.end - self.start).days > 366:
            raise SemanticValidationError("QUERY_LIMIT_EXCEEDED")

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True, slots=True)
class SemanticFilter:
    dimension: str
    operator: str
    value: str

    def __post_init__(self) -> None:
        if self.dimension not in _DIMENSIONS or self.operator != "EQ":
            raise SemanticValidationError("INVALID_FILTER")
        if not isinstance(self.value, str) or not self.value.strip() or len(self.value) > 200:
            raise SemanticValidationError("INVALID_FILTER")

    def to_dict(self) -> dict[str, str]:
        return {"dimension": self.dimension, "operator": self.operator,
                "value": self.value}


@dataclass(frozen=True, slots=True)
class SemanticSort:
    field: str
    direction: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.field):
            raise SemanticValidationError("INVALID_SORT")
        if self.direction not in {"ASC", "DESC"}:
            raise SemanticValidationError("INVALID_SORT")

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "direction": self.direction}


@dataclass(frozen=True, slots=True)
class SemanticQuery:
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...]
    entities: tuple[str, ...]
    time_range: SemanticTimeRange
    filters: tuple[SemanticFilter, ...] = ()
    sort: tuple[SemanticSort, ...] = ()
    limit: int = 200
    as_of_time: datetime | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.metrics) <= 5 or len(set(self.metrics)) != len(self.metrics):
            code = "INVALID_METRICS" if not self.metrics else "QUERY_LIMIT_EXCEEDED"
            raise SemanticValidationError(code)
        if not 1 <= len(self.dimensions) <= 4 or len(set(self.dimensions)) != len(self.dimensions):
            raise SemanticValidationError("INVALID_DIMENSIONS")
        if not 1 <= len(self.entities) <= 10:
            raise SemanticValidationError("QUERY_LIMIT_EXCEEDED")
        if any(not _SYMBOL.fullmatch(item) for item in self.entities):
            raise SemanticValidationError("INVALID_ENTITY")
        if len(self.filters) > 10 or len(self.sort) > 5:
            raise SemanticValidationError("QUERY_LIMIT_EXCEEDED")
        allowed_sort = set(self.metrics) | set(self.dimensions)
        if any(item.field not in allowed_sort for item in self.sort):
            raise SemanticValidationError("INVALID_SORT")
        if type(self.limit) is not int or not 1 <= self.limit <= 1000:
            raise SemanticValidationError("QUERY_LIMIT_EXCEEDED")
        if self.as_of_time is not None and self.as_of_time.utcoffset() is None:
            raise SemanticValidationError("TIMEZONE_REQUIRED")

    @property
    def query_id(self) -> str:
        return _hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": list(self.metrics), "dimensions": list(self.dimensions),
            "entities": list(self.entities), "time_range": self.time_range.to_dict(),
            "filters": [item.to_dict() for item in self.filters],
            "sort": [item.to_dict() for item in self.sort], "limit": self.limit,
            "as_of_time": self.as_of_time.isoformat() if self.as_of_time else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SemanticQuery:
        if set(value) != _QUERY_FIELDS:
            raise SemanticValidationError("INVALID_QUERY_FIELDS")
        try:
            time_range = value["time_range"]
            if set(time_range) != {"start", "end"}:
                raise SemanticValidationError("INVALID_TIME_RANGE")
            filters = tuple(_filter_from_dict(item) for item in value["filters"])
            sorts = tuple(_sort_from_dict(item) for item in value["sort"])
            stamp = value["as_of_time"]
            return cls(
                tuple(value["metrics"]), tuple(value["dimensions"]),
                tuple(value["entities"]), SemanticTimeRange(
                    date.fromisoformat(time_range["start"]),
                    date.fromisoformat(time_range["end"]),
                ), filters, sorts, value["limit"],
                datetime.fromisoformat(stamp) if stamp else None,
            )
        except SemanticValidationError:
            raise
        except Exception:
            raise SemanticValidationError("INVALID_SEMANTIC_QUERY") from None


@dataclass(frozen=True, slots=True)
class SemanticStep:
    capability: str
    entities: tuple[str, ...]
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.capability != "market.history":
            raise SemanticValidationError("UNKNOWN_CAPABILITY")

    def to_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "entities": list(self.entities),
                "start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True, slots=True)
class SemanticQueryPlan:
    query_id: str
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...]
    as_of_time: datetime
    steps: tuple[SemanticStep, ...]
    metric_registry_version: str
    dimension_registry_version: str
    capability_availability: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.as_of_time.utcoffset() is None or len(self.query_id) != 64:
            raise SemanticValidationError("INVALID_SEMANTIC_PLAN")
        if self.steps != tuple(sorted(self.steps, key=lambda item: item.capability)):
            raise SemanticValidationError("INVALID_SEMANTIC_PLAN")

    @property
    def plan_hash(self) -> str:
        return _hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id, "metrics": list(self.metrics),
            "dimensions": list(self.dimensions), "as_of_time": self.as_of_time.isoformat(),
            "steps": [item.to_dict() for item in self.steps],
            "metric_registry_version": self.metric_registry_version,
            "dimension_registry_version": self.dimension_registry_version,
            "capability_availability": dict(self.capability_availability),
        }


@dataclass(frozen=True, slots=True)
class ProvenanceBundle:
    dataset_refs: tuple[str, ...]
    providers: tuple[str, ...]
    metric_definition_version: str
    dimension_definition_version: str
    data_cutoff: datetime
    source_capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_refs": list(self.dataset_refs), "providers": list(self.providers),
            "metric_definition_version": self.metric_definition_version,
            "dimension_definition_version": self.dimension_definition_version,
            "data_cutoff": self.data_cutoff.isoformat(),
            "source_capabilities": list(self.source_capabilities),
        }


@dataclass(frozen=True, slots=True)
class InsightCard:
    title: str
    summary: str
    metrics: tuple[str, ...]
    comparison: str | None
    references: tuple[str, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "summary": self.summary,
                "metrics": list(self.metrics), "comparison": self.comparison,
                "references": list(self.references),
                "limitations": list(self.limitations)}


@dataclass(frozen=True, slots=True)
class ChartSpec:
    chart_type: str
    x: str | None
    y: tuple[str, ...]
    series: str | None
    title: str
    unit: str | None

    def __post_init__(self) -> None:
        if self.chart_type not in {"line", "bar", "table"}:
            raise SemanticValidationError("INVALID_CHART_TYPE")

    def to_dict(self) -> dict[str, Any]:
        return {"chart_type": self.chart_type, "x": self.x, "y": list(self.y),
                "series": self.series, "title": self.title, "unit": self.unit}


@dataclass(frozen=True, slots=True)
class SemanticResult:
    query_id: str
    plan_hash: str
    trace_id: str
    status: str
    schema: tuple[Mapping[str, Any], ...]
    rows: tuple[Mapping[str, Any], ...]
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...]
    as_of_time: datetime
    availability: Mapping[str, AvailabilityRecord]
    limitations: tuple[str, ...]
    reason_codes: tuple[str, ...]
    provenance: ProvenanceBundle
    insight: InsightCard
    chart: ChartSpec

    def __post_init__(self) -> None:
        if self.status not in {"PASS", "PARTIAL", "FAILED"}:
            raise SemanticValidationError("INVALID_RESULT_STATUS")
        if self.as_of_time.utcoffset() is None:
            raise SemanticValidationError("TIMEZONE_REQUIRED")
        if len(self.plan_hash) != 64 or not re.fullmatch(r"[0-9a-f]{32}", self.trace_id):
            raise SemanticValidationError("INVALID_RESULT_IDENTITY")

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id, "plan_hash": self.plan_hash,
            "trace_id": self.trace_id, "status": self.status,
            "schema": [dict(item) for item in self.schema],
            "rows": [dict(item) for item in self.rows],
            "metrics": list(self.metrics), "dimensions": list(self.dimensions),
            "as_of_time": self.as_of_time.isoformat(),
            "availability": {key: item.to_dict() for key, item in self.availability.items()},
            "limitations": list(self.limitations), "reason_codes": list(self.reason_codes),
            "provenance": self.provenance.to_dict(), "insight": self.insight.to_dict(),
            "chart": self.chart.to_dict(),
        }


def _filter_from_dict(value: Mapping[str, Any]) -> SemanticFilter:
    if set(value) != {"dimension", "operator", "value"}:
        raise SemanticValidationError("INVALID_FILTER")
    return SemanticFilter(value["dimension"], value["operator"], value["value"])


def _sort_from_dict(value: Mapping[str, Any]) -> SemanticSort:
    if set(value) != {"field", "direction"}:
        raise SemanticValidationError("INVALID_SORT")
    return SemanticSort(value["field"], value["direction"])


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_identity_json_bytes(value)).hexdigest()
