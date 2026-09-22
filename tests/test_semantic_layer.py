"""Bounded AI-BI semantic contracts and deterministic market execution."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from quantos.config import MARKET_TIMEZONE, Settings
from quantos.schemas import MarketBar
from quantos.semantic import (
    AvailabilityStatus,
    DimensionRegistry,
    MetricRegistry,
    SemanticQuery,
    SemanticService,
    SemanticValidationError,
)


NOW = datetime(2026, 9, 18, 18, tzinfo=MARKET_TIMEZONE)


def query_value(**overrides):
    value = {
        "metrics": ["close"],
        "dimensions": ["trading_date"],
        "entities": ["600519.SH"],
        "time_range": {"start": "2026-08-20", "end": "2026-09-18"},
        "filters": [],
        "sort": [{"field": "trading_date", "direction": "ASC"}],
        "limit": 200,
        "as_of_time": NOW.isoformat(),
    }
    value.update(overrides)
    return value


def bar(day: date, *, close="100", previous="99", source="tushare",
        available_at: datetime | None = None):
    timestamp = datetime(day.year, day.month, day.day, 15, tzinfo=MARKET_TIMEZONE)
    return MarketBar(
        "600519.SH", timestamp, "1d", Decimal("98"), Decimal("102"),
        Decimal("97"), Decimal(close), 1000, Decimal("100000"), source,
        f"daily:{day.isoformat()}", timestamp + timedelta(hours=1),
        available_at or timestamp + timedelta(hours=1), Decimal(previous),
    )


class Bars:
    def __init__(self, values):
        self.values = tuple(values)
        self.calls = []

    def read_by_symbol(self, symbol, *, as_of_time, start_date=None, end_date=None):
        self.calls.append((symbol, as_of_time, start_date, end_date))
        return list(self.values)


def test_default_registries_are_closed_and_contain_only_supported_fields():
    metrics = MetricRegistry.default()
    dimensions = DimensionRegistry.default()

    assert metrics.names() == (
        "close", "high", "low", "open", "price_change", "return", "volume",
    )
    assert dimensions.names() == (
        "market", "provider", "security", "trading_date",
    )
    assert metrics.get("return").source_capability == "market.history"
    assert dimensions.get("trading_date").time_semantics == "MARKET_EVENT_TIME"
    with pytest.raises(SemanticValidationError, match="UNKNOWN_METRIC"):
        metrics.get("arbitrary_sql")
    with pytest.raises(SemanticValidationError, match="UNKNOWN_DIMENSION"):
        dimensions.get("raw_column")


def test_semantic_query_is_exact_serializable_and_rejects_control_plane_fields():
    query = SemanticQuery.from_dict(query_value())

    assert SemanticQuery.from_dict(query.to_dict()) == query
    assert query.query_id == SemanticQuery.from_dict(query_value()).query_id
    for field in ("sql", "expression", "python", "shell", "raw_column", "table", "join"):
        with pytest.raises(SemanticValidationError, match="INVALID_QUERY_FIELDS"):
            SemanticQuery.from_dict({**query_value(), field: "unsafe"})


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"metrics": []}, "INVALID_METRICS"),
        ({"metrics": ["close"] * 6}, "QUERY_LIMIT_EXCEEDED"),
        ({"entities": [f"{index:06d}.SH" for index in range(11)]}, "QUERY_LIMIT_EXCEEDED"),
        ({"limit": 1001}, "QUERY_LIMIT_EXCEEDED"),
        ({"as_of_time": "2026-09-18T18:00:00"}, "TIMEZONE_REQUIRED"),
        ({"time_range": {"start": "2025-01-01", "end": "2026-09-18"}},
         "QUERY_LIMIT_EXCEEDED"),
    ],
)
def test_semantic_query_bounds_fail_closed(overrides, code):
    with pytest.raises(SemanticValidationError, match=code):
        SemanticQuery.from_dict(query_value(**overrides))


def test_semantic_query_validates_filter_and_sort_against_requested_shape():
    with pytest.raises(SemanticValidationError, match="INVALID_FILTER"):
        SemanticQuery.from_dict(query_value(filters=[{
            "dimension": "table", "operator": "EQ", "value": "market",
        }]))
    with pytest.raises(SemanticValidationError, match="INVALID_SORT"):
        SemanticQuery.from_dict(query_value(sort=[{
            "field": "unknown", "direction": "ASC",
        }]))


def test_semantic_service_executes_fixed_market_metrics_with_provenance_and_line_chart(tmp_path):
    repository = Bars((
        bar(date(2026, 9, 17), close="100", previous="98"),
        bar(date(2026, 9, 18), close="101", previous="100"),
    ))
    service = SemanticService(
        settings=Settings.from_project_root(tmp_path), market_repository=repository,
        data_available=lambda: True, clock=lambda: NOW,
    )
    query = SemanticQuery.from_dict(query_value(
        metrics=["close", "price_change", "return", "volume"],
    ))

    plan_one = service.plan(query)
    plan_two = service.plan(query)
    result = service.execute(query)

    assert plan_one.plan_hash == plan_two.plan_hash
    assert plan_one.steps[0].capability == "market.history"
    assert repository.calls == [(
        "600519.SH", NOW, date(2026, 8, 20), date(2026, 9, 18),
    )]
    assert result.status == "PASS"
    assert result.rows[-1] == {
        "trading_date": "2026-09-18", "close": "101",
        "price_change": "1", "return": "1.00", "volume": 1000,
    }
    assert result.chart.chart_type == "line"
    assert result.chart.x == "trading_date"
    assert result.provenance.providers == ("tushare",)
    assert len(result.provenance.dataset_refs) == 2
    assert result.plan_hash == plan_one.plan_hash


def test_semantic_filter_can_use_registered_dimension_not_selected_for_output(tmp_path):
    repository = Bars((
        bar(date(2026, 9, 17), source="tushare"),
        bar(date(2026, 9, 18), source="baostock"),
    ))
    service = SemanticService(
        settings=Settings.from_project_root(tmp_path), market_repository=repository,
        data_available=lambda: True, clock=lambda: NOW,
    )
    result = service.execute(SemanticQuery.from_dict(query_value(filters=[{
        "dimension": "provider", "operator": "EQ", "value": "tushare",
    }])))

    assert result.status == "PASS"
    assert result.rows == ({"trading_date": "2026-09-17", "close": "100"},)


def test_default_semantic_service_initialization_is_filesystem_read_only(tmp_path):
    settings = Settings.from_project_root(tmp_path)

    SemanticService(settings=settings, clock=lambda: NOW)

    assert not settings.data_root.exists()


def test_missing_market_data_is_distinct_from_available_zero_rows(tmp_path):
    settings = Settings.from_project_root(tmp_path)
    unavailable = SemanticService(
        settings=settings, market_repository=Bars(()),
        data_available=lambda: False, clock=lambda: NOW,
    ).execute(SemanticQuery.from_dict(query_value()))
    empty = SemanticService(
        settings=settings, market_repository=Bars(()),
        data_available=lambda: True, clock=lambda: NOW,
    ).execute(SemanticQuery.from_dict(query_value()))

    assert unavailable.status == "PARTIAL"
    assert unavailable.availability["market_data"].status is AvailabilityStatus.UNAVAILABLE
    assert unavailable.reason_codes == ("MARKET_DATA_UNAVAILABLE",)
    assert empty.status == "PASS"
    assert empty.availability["market_data"].status is AvailabilityStatus.READY
    assert empty.availability["market_data"].record_count == 0
    assert empty.reason_codes == ("NO_DATA",)


def test_semantic_service_rejects_future_ranges_and_future_repository_rows(tmp_path):
    service = SemanticService(
        settings=Settings.from_project_root(tmp_path),
        market_repository=Bars((bar(
            date(2026, 9, 18),
            available_at=NOW + timedelta(minutes=1),
        ),)), data_available=lambda: True, clock=lambda: NOW,
    )

    with pytest.raises(SemanticValidationError, match="PIT_BOUNDARY_VIOLATION"):
        service.execute(SemanticQuery.from_dict(query_value(
            time_range={"start": "2026-09-18", "end": "2026-09-19"},
        )))
    with pytest.raises(SemanticValidationError, match="PIT_REJECTED"):
        service.execute(SemanticQuery.from_dict(query_value(
            time_range={"start": "2026-09-18", "end": "2026-09-18"},
        )))
