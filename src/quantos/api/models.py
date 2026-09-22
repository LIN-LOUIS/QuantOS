"""Thin Pydantic transport schemas; domain validation remains canonical."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AskBody(StrictBody):
    symbol: str = Field(pattern=r"^\d{6}\.(?:SH|SZ|BJ)$")
    question: str = Field(min_length=1, max_length=1000)
    as_of_time: datetime | None = None
    no_research: bool = False


class TimeRangeBody(StrictBody):
    start: date
    end: date


class FilterBody(StrictBody):
    dimension: Literal["security", "trading_date", "market", "provider"]
    operator: Literal["EQ"] = "EQ"
    value: str = Field(min_length=1, max_length=200)


class SortBody(StrictBody):
    field: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    direction: Literal["ASC", "DESC"] = "ASC"


class SemanticQueryBody(StrictBody):
    metrics: Annotated[list[str], Field(min_length=1, max_length=5)]
    dimensions: Annotated[list[str], Field(min_length=1, max_length=4)]
    entities: Annotated[list[str], Field(min_length=1, max_length=10)]
    time_range: TimeRangeBody
    filters: Annotated[list[FilterBody], Field(max_length=10)] = Field(
        default_factory=list,
    )
    sort: Annotated[list[SortBody], Field(max_length=5)] = Field(
        default_factory=list,
    )
    limit: int = Field(default=200, ge=1, le=1000)
    as_of_time: datetime | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: Literal["quantos-research-api"]
    api_version: Literal["v1"]
    runtime_mode: Literal["LOCAL", "DEMO"]
    data_label: Literal["LOCAL_PERSISTED_DATA", "SYNTHETIC_FIXTURE"]
    quantos_version: str
    build_commit: str


class AskResponseBody(BaseModel):
    request_id: str
    trace_id: str
    status: Literal["PASS", "PARTIAL", "FAIL", "UNSUPPORTED"]
    answer: str
    intent: str
    entities: list[dict[str, Any]]
    as_of_time: datetime
    facts: list[dict[str, Any]]
    references: list[str]
    evidence_refs: list[str]
    knowledge_refs: list[str]
    report_refs: list[str]
    limitations: list[str]
    reason_codes: list[str]


class AvailabilityResponse(BaseModel):
    status: Literal["READY", "PARTIAL", "UNAVAILABLE"]
    record_count: int | None
    reason_code: str | None


class ProvenanceResponse(BaseModel):
    dataset_refs: list[str]
    providers: list[str]
    metric_definition_version: str
    dimension_definition_version: str
    data_cutoff: datetime
    source_capabilities: list[str]


class InsightResponse(BaseModel):
    title: str
    summary: str
    metrics: list[str]
    comparison: str | None
    references: list[str]
    limitations: list[str]


class ChartResponse(BaseModel):
    chart_type: Literal["line", "bar", "table"]
    x: str | None
    y: list[str]
    series: str | None
    title: str
    unit: str | None


class SemanticResultResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    query_id: str
    plan_hash: str
    trace_id: str
    status: Literal["PASS", "PARTIAL", "FAILED"]
    result_schema: list[dict[str, Any]] = Field(alias="schema")
    rows: list[dict[str, Any]]
    metrics: list[str]
    dimensions: list[str]
    as_of_time: datetime
    availability: dict[str, AvailabilityResponse]
    limitations: list[str]
    reason_codes: list[str]
    provenance: ProvenanceResponse
    insight: InsightResponse
    chart: ChartResponse


class ReportSummaryResponse(BaseModel):
    report_id: str
    type: Literal["daily"]
    trade_date: date
    as_of_time: datetime
    generated_at: datetime
    mode: str
    schema_version: str
    refs: list[str]


class PaginationResponse(BaseModel):
    limit: int
    offset: int
    total: int


class ReportListResponse(BaseModel):
    items: list[ReportSummaryResponse]
    pagination: PaginationResponse


class MetricDefinitionResponse(BaseModel):
    metric_id: str
    display_name: str
    description: str
    unit: str
    value_type: str
    supported_dimensions: list[str]
    time_semantics: str
    source_capability: str
    availability: Literal["READY", "PARTIAL", "UNAVAILABLE"]
    version: str


class DimensionDefinitionResponse(BaseModel):
    dimension_id: str
    display_name: str
    description: str
    value_type: str
    time_semantics: str
    source_capability: str
    availability: Literal["READY", "PARTIAL", "UNAVAILABLE"]
    version: str


class SemanticQueryLimitsResponse(BaseModel):
    max_metrics: int
    max_dimensions: int
    max_entities: int
    max_filters: int
    max_sorts: int
    max_rows: int
    max_date_range_days: int


class AnalyticsSchemaResponse(BaseModel):
    metrics: list[MetricDefinitionResponse]
    dimensions: list[DimensionDefinitionResponse]
    query_limits: SemanticQueryLimitsResponse
    metric_registry_version: str
    dimension_registry_version: str


class ReplayCampaignSummaryResponse(BaseModel):
    campaign_id: str
    created_at: datetime
    replay_mode: Literal["STRICT_OPERATIONAL_PIT", "RETROSPECTIVE_RECONSTRUCTED"]
    identity_temporal_semantics: Literal[
        "OBSERVED_KNOWLEDGE", "RETROSPECTIVE_EFFECTIVE_TRUTH"
    ]
    universe: dict[str, Any]
    summary: dict[str, Any]


class ReplayCampaignListResponse(BaseModel):
    items: list[ReplayCampaignSummaryResponse]
    pagination: PaginationResponse


class ErrorResponse(BaseModel):
    code: str
    message: str
    request_id: str
    details: list[dict[str, str]] = Field(default_factory=list)
