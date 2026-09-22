"""Versioned, local-development Research API."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import logging
import time
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from quantos import __version__
from quantos.commands import ask as ask_command
from quantos.config import DEFAULT_SETTINGS, MARKET_TIMEZONE, Settings
from quantos.semantic import SemanticQuery, SemanticService, SemanticValidationError
from quantos.replay import ReplayMode

from .models import (
    AnalyticsSchemaResponse, AskBody, AskResponseBody, ErrorResponse, HealthResponse,
    ReplayCampaignListResponse, ReportListResponse, SemanticQueryBody,
    SemanticResultResponse,
)
from .resources import (
    InvalidResourceId, ResearchResources, ResourceConflict, ResourceNotFound,
)


@dataclass(frozen=True, slots=True)
class ApiAuditRecord:
    method: str
    endpoint: str
    status_code: int
    duration_ms: float
    correlation_id: str


AuditSink = Callable[[ApiAuditRecord], None]
AskSessionFactory = Callable[[str, datetime | None, bool], Any]


class _LoggingAuditSink:
    def __init__(self) -> None:
        self.logger = logging.getLogger("quantos.api.audit")

    def __call__(self, item: ApiAuditRecord) -> None:
        self.logger.info(
            "research_api_request",
            extra={"method": item.method, "endpoint": item.endpoint,
                   "status_code": item.status_code,
                   "duration_ms": item.duration_ms,
                   "correlation_id": item.correlation_id},
        )


def create_app(
    *, settings: Settings = DEFAULT_SETTINGS,
    ask_session_factory: AskSessionFactory | None = None,
    semantic_service: SemanticService | None = None,
    audit_sink: AuditSink | None = None,
    clock: Callable[[], datetime] | None = None,
    runtime_mode: Literal["LOCAL", "DEMO"] = "LOCAL",
    build_commit: str = "UNKNOWN",
    workspace_dir: Path | None = None,
) -> FastAPI:
    now = clock or (lambda: datetime.now(MARKET_TIMEZONE))
    resources = ResearchResources(settings)
    semantics = semantic_service or SemanticService(settings=settings, clock=now)
    make_ask = ask_session_factory or (
        lambda symbol, as_of_time, no_research: ask_command.make_session(
            symbol, as_of_time, no_research=no_research, offline=True,
            settings=settings, clock=now,
        )
    )
    emit_audit = audit_sink or _LoggingAuditSink()
    app = FastAPI(
        title="QuantOS Research API", version="1.0.0",
        description="Read-only local research and bounded semantic analytics.",
    )

    @app.middleware("http")
    async def correlation_and_audit(request: Request, call_next):
        correlation_id = uuid4().hex
        request.state.correlation_id = correlation_id
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            response = _error(
                request, 500, "INTERNAL_ERROR", "Internal server error.",
            )
        response.headers["X-Correlation-ID"] = correlation_id
        route = request.scope.get("route")
        endpoint = getattr(route, "path", "UNMATCHED")
        emit_audit(ApiAuditRecord(
            request.method, endpoint, response.status_code,
            (time.monotonic() - started) * 1000, correlation_id,
        ))
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        details = [{
            "field": ".".join(str(item) for item in entry["loc"]),
            "type": entry["type"],
        } for entry in error.errors()]
        return _error(request, 422, "VALIDATION_ERROR", "Request validation failed.", details)

    @app.exception_handler(SemanticValidationError)
    async def semantic_error(request: Request, error: SemanticValidationError):
        return _error(request, 422, str(error), "Semantic query validation failed.")

    @app.exception_handler(InvalidResourceId)
    async def invalid_resource(request: Request, error: InvalidResourceId):
        return _error(request, 400, str(error), "Resource identifier is invalid.")

    @app.exception_handler(ResourceNotFound)
    async def missing_resource(request: Request, error: ResourceNotFound):
        return _error(request, 404, str(error), "Resource was not found.")

    @app.exception_handler(ResourceConflict)
    async def resource_conflict(request: Request, error: ResourceConflict):
        return _error(request, 409, str(error), "Resource state is conflicting.")

    @app.exception_handler(ask_command.AskFailure)
    async def ask_error(request: Request, error: ask_command.AskFailure):
        return _error(request, 400, str(error), "Ask request could not be executed.")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, _error_value: Exception):
        return _error(request, 500, "INTERNAL_ERROR", "Internal server error.")

    @app.get("/v1/health", response_model=HealthResponse)
    def health():
        return {
            "status": "ok", "service": "quantos-research-api", "api_version": "v1",
            "runtime_mode": runtime_mode,
            "data_label": (
                "SYNTHETIC_FIXTURE" if runtime_mode == "DEMO"
                else "LOCAL_PERSISTED_DATA"
            ),
            "quantos_version": __version__, "build_commit": build_commit,
        }

    @app.get("/v1/status")
    def status():
        return resources.status()

    @app.post(
        "/v1/ask", response_model=AskResponseBody,
        responses={400: {"model": ErrorResponse}},
    )
    def ask(body: AskBody):
        session = make_ask(body.symbol, body.as_of_time, body.no_research)
        result = session.ask(body.question)
        return jsonable_encoder({
            "request_id": result.request_id, "trace_id": result.trace_id,
            "status": result.status, "answer": result.answer,
            "intent": result.intent.value,
            "entities": [asdict(item) for item in result.entities],
            "as_of_time": result.as_of_time.isoformat(), "facts": list(result.facts),
            "references": sorted({*result.source_refs, *result.evidence_refs,
                                  *result.knowledge_refs, *result.report_refs}),
            "evidence_refs": list(result.evidence_refs),
            "knowledge_refs": list(result.knowledge_refs),
            "report_refs": list(result.report_refs),
            "limitations": list(result.limitations),
            "reason_codes": list(result.reason_codes),
        })

    @app.get("/v1/traces/{trace_id}")
    def trace(trace_id: str):
        return resources.trace(trace_id)

    @app.get("/v1/reports", response_model=ReportListResponse)
    def reports(
        report_type: Literal["daily"] = Query(default="daily", alias="type"),
        trade_date: date | None = Query(default=None, alias="date"),
        symbol: str | None = Query(default=None, pattern=r"^\d{6}\.(?:SH|SZ|BJ)$"),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10_000),
    ):
        return resources.list_reports(
            report_type=report_type, trade_date=trade_date, symbol=symbol,
            limit=limit, offset=offset,
        )

    @app.get("/v1/reports/{report_id}")
    def report(report_id: str):
        return jsonable_encoder(resources.report(report_id))

    @app.get("/v1/replay/campaigns/{campaign_id}")
    def replay_campaign(campaign_id: str):
        return resources.replay_campaign(campaign_id)

    @app.get("/v1/replay/campaigns/{campaign_id}/failures")
    def replay_failures(campaign_id: str):
        return resources.replay_failures(campaign_id)

    @app.get("/v1/replay/campaigns", response_model=ReplayCampaignListResponse)
    def replay_campaigns(
        mode: ReplayMode | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10_000),
    ):
        return resources.list_replay_campaigns(mode=mode, limit=limit, offset=offset)

    @app.get("/v1/analytics/schema", response_model=AnalyticsSchemaResponse)
    def analytics_schema():
        return semantics.schema()

    @app.post(
        "/v1/analytics/query", response_model=SemanticResultResponse,
        responses={422: {"model": ErrorResponse}},
    )
    def analytics(body: SemanticQueryBody):
        query = SemanticQuery.from_dict(body.model_dump(mode="json"))
        return semantics.execute(query).to_dict()

    if workspace_dir is not None:
        root = Path(workspace_dir).resolve()
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="workspace-assets")

        @app.get("/{workspace_path:path}", include_in_schema=False)
        def workspace(workspace_path: str):
            if workspace_path.startswith("v1/"):
                return JSONResponse(status_code=404, content={
                    "code": "RESOURCE_NOT_FOUND", "message": "Resource was not found.",
                    "request_id": uuid4().hex, "details": [],
                })
            return FileResponse(root / "index.html")

    return app


def _error(
    request: Request, status_code: int, code: str, message: str,
    details: list[dict[str, str]] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "correlation_id", uuid4().hex)
    return JSONResponse(status_code=status_code, content={
        "code": code, "message": message, "request_id": request_id,
        "details": details or [],
    })
