"""Bounded adapters from Ask capabilities to existing local QuantOS products."""

from __future__ import annotations

from datetime import datetime
from quantos.commands.status import inspect_status
from quantos.config import (
    DEFAULT_SETTINGS, KnowledgeIntegrationSettings, Settings,
)
from quantos.knowledge_retrieval import retrieve_knowledge
from quantos.schemas import DailyIntelligenceReport
from quantos.schemas.knowledge import KnowledgeQueryMode
from quantos.schemas.knowledge_retrieval import KnowledgeRetrievalRequest
from quantos.storage import (
    DailyReportRepository, KnowledgeLexicalIndexRepository, KnowledgeRepository,
)

from .capabilities import (
    AttributionLookupResult, KnowledgeQueryResult, ReportLookupResult,
    SectorPerformanceResult, SystemStatusResult,
)
from .contracts import AskIntent
from .registry import Capability, CapabilityAvailability


class LocalAskCapabilities:
    """Read-only adapters over persisted QuantOS artifacts and indexes."""

    def __init__(self, *, settings: Settings = DEFAULT_SETTINGS,
                 knowledge_settings: KnowledgeIntegrationSettings | None = None) -> None:
        self.settings = settings
        self.knowledge_settings = knowledge_settings or KnowledgeIntegrationSettings.from_env()

    def availability(self, name: str) -> CapabilityAvailability:
        if name == "system.status":
            return CapabilityAvailability.READY
        if name == "knowledge.query":
            if not self.knowledge_settings.enabled:
                return CapabilityAvailability.DISABLED
            version = self.knowledge_settings.lexical_index_version
            path = (self.settings.data_root / "knowledge" / "indexes" / "lexical"
                    / f"lexical_index_version={version}.json")
            documents = self.settings.data_root / "knowledge" / "documents"
            return (CapabilityAvailability.READY if path.is_file()
                    and any(documents.rglob("*.json"))
                    else CapabilityAvailability.UNAVAILABLE)
        if name in {"report.lookup", "attribution.lookup", "sector.performance"}:
            return (CapabilityAvailability.READY
                    if any(self.settings.report_dir.glob("trade_date=*/*.json"))
                    else CapabilityAvailability.UNAVAILABLE)
        return CapabilityAvailability.UNAVAILABLE

    def capabilities(self) -> tuple[Capability, ...]:
        entity_schema = (("symbol", str), ("as_of_time", datetime))
        query_schema = (("query", str), ("symbol", str), ("as_of_time", datetime))
        return (
            Capability(
                "attribution.lookup", entity_schema,
                (AskIntent.ENTITY_MOVE_EXPLANATION, AskIntent.ATTRIBUTION_LOOKUP),
                self.attribution_lookup, AttributionLookupResult,
                availability=self.availability("attribution.lookup"),
            ),
            Capability(
                "knowledge.query", query_schema, (AskIntent.KNOWLEDGE_LOOKUP,),
                self.knowledge_query, KnowledgeQueryResult,
                availability=self.availability("knowledge.query"),
            ),
            Capability(
                "report.lookup", query_schema, (AskIntent.REPORT_LOOKUP,),
                self.report_lookup, ReportLookupResult,
                availability=self.availability("report.lookup"),
            ),
            Capability(
                "sector.performance", (("query", str), ("as_of_time", datetime)),
                (AskIntent.SECTOR_PERFORMANCE,), self.sector_performance,
                SectorPerformanceResult,
                availability=self.availability("sector.performance"),
            ),
            Capability(
                "system.status", (("as_of_time", datetime),),
                (AskIntent.SYSTEM_STATUS,), self.system_status, SystemStatusResult,
            ),
        )

    def report_lookup(self, args) -> ReportLookupResult:
        report = self._latest_report(args["as_of_time"], symbol=args["symbol"])
        if report is None:
            return ReportLookupResult((), ())
        reference = f"R:{report.report_id}"
        return ReportLookupResult((reference,), ({
            "ref": reference,
            "trade_date": report.trade_date.isoformat(),
            "as_of_time": report.as_of_time.isoformat(),
            "mode": report.mode,
            "candidate_count": len(report.candidate_briefs),
        },), _run_id(report))

    def attribution_lookup(self, args) -> AttributionLookupResult:
        report = self._latest_report(args["as_of_time"], symbol=args["symbol"])
        if report is None:
            return AttributionLookupResult(limitation="ATTRIBUTION_UNAVAILABLE")
        brief = next(item for item in report.candidate_briefs if item.symbol == args["symbol"])
        reference = f"R:{report.report_id}"
        refs = tuple(brief.synthesis.used_evidence_ids)
        eligible = refs if brief.evidence_stats.eligible_attribution_count else ()
        causal = bool(
            brief.evidence_stats.strict_attribution_count
            and brief.synthesis.status == "PASS"
            and brief.synthesis.insufficient_evidence is False
        )
        limitation = None
        if not causal:
            limitation = ("NON_STRICT_TEMPORAL_EVIDENCE"
                          if brief.evidence_stats.research_attribution_count else
                          "ATTRIBUTION_INSUFFICIENT")
        return AttributionLookupResult(
            refs, eligible, (reference,) if causal else (), causal,
            (reference,), limitation,
        )

    def knowledge_query(self, args) -> KnowledgeQueryResult:
        if self.availability("knowledge.query") is not CapabilityAvailability.READY:
            return KnowledgeQueryResult((), ())
        version = self.knowledge_settings.lexical_index_version
        repository = KnowledgeRepository(settings=self.settings)
        index = KnowledgeLexicalIndexRepository(settings=self.settings).load(version)
        request = KnowledgeRetrievalRequest(
            query=args["query"], mode=KnowledgeQueryMode.STRICT_LIVE,
            as_of_time=args["as_of_time"], entity_refs=(args["symbol"],),
            limit=self.knowledge_settings.retrieval_limit,
        )
        result = retrieve_knowledge(
            repository, index, request, generated_at=args["as_of_time"],
        )
        refs = tuple(f"K{index}" for index in range(1, len(result.hits) + 1))
        facts = tuple({
            "ref": ref, "document_id": hit.document_id, "chunk_id": hit.chunk_id,
            "title": hit.title, "source": hit.source,
            "available_at": hit.available_at.isoformat(),
            "content": hit.content[:500],
        } for ref, hit in zip(refs, result.hits))
        return KnowledgeQueryResult(refs, facts)

    def sector_performance(self, args) -> SectorPerformanceResult:
        report = self._latest_report(args["as_of_time"])
        if report is None or not report.sector_overview:
            return SectorPerformanceResult((), ())
        reference = f"R:{report.report_id}"
        return SectorPerformanceResult((reference,), ({
            "ref": reference,
            "trade_date": report.trade_date.isoformat(),
            "sector_overview": dict(report.sector_overview),
        },))

    def system_status(self, args) -> SystemStatusResult:
        value = inspect_status(self.settings.project_root)
        facts = (
            {"capability": "market.snapshot",
             "availability": _readiness(value["market"]["readiness"])},
            {"capability": "evidence.retrieve",
             "availability": _readiness(value["evidence"]["readiness"])},
            {"capability": "knowledge.query",
             "availability": self.availability("knowledge.query").value},
            {"capability": "report.lookup",
             "availability": self.availability("report.lookup").value},
            {"capability": "sector.performance",
             "availability": self.availability("sector.performance").value},
            {"capability": "historical.query", "availability": "UNAVAILABLE"},
        )
        return SystemStatusResult(facts)

    def _latest_report(self, as_of_time: datetime,
                       symbol: str | None = None) -> DailyIntelligenceReport | None:
        repository = DailyReportRepository(self.settings, read_only=True)
        reports = []
        for path in sorted(self.settings.report_dir.glob("trade_date=*/*.json")):
            try:
                report = repository.read_json(path)
            except Exception:
                continue
            if report.as_of_time > as_of_time or report.generated_at > as_of_time:
                continue
            if symbol is not None and all(
                item.symbol != symbol for item in report.candidate_briefs
            ):
                continue
            reports.append(report)
        if not reports:
            return None
        return max(reports, key=lambda item: (
            item.as_of_time, item.generated_at, item.report_id,
        ))


def _readiness(value: str) -> str:
    return "READY" if value == "READY" else "UNAVAILABLE"


def _run_id(report: DailyIntelligenceReport) -> str | None:
    value = dict(report.provenance).get("run_id")
    return value if isinstance(value, str) and value else None
