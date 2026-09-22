"""Read-only application resources reused by the Research API."""

from __future__ import annotations

from datetime import date
import re
from typing import Any

from quantos.commands.status import inspect_status
from quantos.config import Settings
from quantos.replay import IdentityTemporalSemantics, ReplayMode
from quantos.replay.storage import ReplayCampaignRepository, ReplayStorageError
from quantos.reporting import report_to_dict
from quantos.storage import (
    AskTraceRepository, AskTraceStorageError, DailyReportRepository, StorageError,
)


_TRACE_ID = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{64})$")
_REPORT_ID = re.compile(r"^[0-9a-f]{64}$")
_CAMPAIGN_ID = re.compile(r"^[0-9a-f]{32}$")


class InvalidResourceId(ValueError):
    pass


class ResourceNotFound(LookupError):
    pass


class ResourceConflict(RuntimeError):
    pass


class ResearchResources:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.traces = AskTraceRepository(settings=settings)
        self.reports = DailyReportRepository(settings, read_only=True)
        self.replays = ReplayCampaignRepository(settings)

    def status(self) -> dict[str, Any]:
        value = inspect_status(self.settings.project_root)
        evidence = _availability(value["evidence"]["readiness"])
        knowledge = _availability(value["knowledge"]["readiness"])
        reports = _availability(value["daily_report"]["availability"])
        replay = value["data_availability"]["historical_replay"]
        return {
            "providers": [self._provider_status(item) for item in value["providers"]],
            "data_availability": value["data_availability"],
            "research_availability": {
                "evidence": evidence,
                "attribution": "PARTIAL" if evidence == "READY" else "UNAVAILABLE",
                "knowledge": knowledge,
                "reports": reports,
            },
            "replay_availability": {
                "status": replay["availability"],
                "reason_code": replay["reason_code"],
                "dataset_count": replay["dataset_count"],
            },
        }

    @staticmethod
    def _provider_status(item: dict[str, Any]) -> dict[str, Any]:
        """Expose health and capability state without configuration identifiers."""

        allowed = (
            "provider_id", "provider_type", "availability", "capabilities",
            "credential_configured", "last_success", "last_failure", "health_reason",
        )
        return {key: item[key] for key in allowed if key in item}

    def trace(self, trace_id: str) -> dict[str, Any]:
        if not _TRACE_ID.fullmatch(trace_id):
            raise InvalidResourceId("INVALID_TRACE_ID")
        try:
            return self.traces.load(trace_id).to_dict()
        except AskTraceStorageError:
            raise ResourceNotFound("TRACE_NOT_FOUND") from None

    def list_reports(
        self, *, report_type: str, trade_date: date | None, symbol: str | None,
        limit: int, offset: int,
    ) -> dict[str, Any]:
        if report_type != "daily":
            raise InvalidResourceId("UNSUPPORTED_REPORT_TYPE")
        found = {}
        for path in sorted(self.settings.report_dir.glob("trade_date=*/*.json")):
            try:
                report = self.reports.read_json(path)
            except StorageError:
                continue
            if trade_date is not None and report.trade_date != trade_date:
                continue
            if symbol is not None and all(
                item.symbol != symbol for item in report.candidate_briefs
            ):
                continue
            existing = found.get(report.report_id)
            if (existing is not None
                    and report_to_dict(existing) != report_to_dict(report)):
                raise ResourceConflict("REPORT_ID_CONFLICT")
            found[report.report_id] = report
        values = sorted(found.values(), key=lambda item: (
            item.trade_date, item.generated_at, item.report_id,
        ), reverse=True)
        items = values[offset:offset + limit]
        return {
            "items": [self._report_summary(item) for item in items],
            "pagination": {"limit": limit, "offset": offset, "total": len(values)},
        }

    def report(self, report_id: str) -> dict[str, Any]:
        if not _REPORT_ID.fullmatch(report_id):
            raise InvalidResourceId("INVALID_REPORT_ID")
        matches = []
        for path in sorted(self.settings.report_dir.glob("trade_date=*/*.json")):
            try:
                report = self.reports.read_json(path)
            except StorageError:
                continue
            if report.report_id == report_id:
                matches.append(report)
        if not matches:
            raise ResourceNotFound("REPORT_NOT_FOUND")
        first = matches[0]
        if any(report_to_dict(item) != report_to_dict(first) for item in matches[1:]):
            raise ResourceConflict("REPORT_ID_CONFLICT")
        return report_to_dict(first)

    def replay_campaign(self, campaign_id: str) -> dict[str, Any]:
        manifest = self._campaign(campaign_id)
        return {
            **manifest.to_dict(),
            "identity_temporal_semantics": _identity_semantics(manifest.replay_mode),
        }

    def list_replay_campaigns(
        self, *, mode: ReplayMode | None, limit: int, offset: int,
    ) -> dict[str, Any]:
        values = []
        if self.replays.root.is_dir():
            for path in sorted(self.replays.root.glob("campaign_id=*")):
                campaign_id = path.name.removeprefix("campaign_id=")
                if not _CAMPAIGN_ID.fullmatch(campaign_id):
                    continue
                try:
                    manifest = self.replays.load(campaign_id)
                except ReplayStorageError:
                    continue
                if mode is not None and manifest.replay_mode is not mode:
                    continue
                values.append(manifest)
        values.sort(key=lambda item: (item.created_at, item.campaign_id), reverse=True)
        items = values[offset:offset + limit]
        return {
            "items": [{
                "campaign_id": item.campaign_id,
                "created_at": item.created_at.isoformat(),
                "replay_mode": item.replay_mode.value,
                "identity_temporal_semantics": _identity_semantics(item.replay_mode),
                "universe": item.universe.to_dict(),
                "summary": item.summary.to_dict(),
            } for item in items],
            "pagination": {"limit": limit, "offset": offset, "total": len(values)},
        }

    def replay_failures(self, campaign_id: str) -> dict[str, Any]:
        manifest = self._campaign(campaign_id)
        try:
            failures = self.replays.load_failures(campaign_id)
        except ReplayStorageError:
            raise ResourceNotFound("REPLAY_FAILURES_NOT_FOUND") from None
        return {
            "campaign_id": campaign_id,
            "replay_mode": manifest.replay_mode.value,
            "identity_temporal_semantics": _identity_semantics(manifest.replay_mode),
            "failures": [item.to_dict() for item in failures],
        }

    def _campaign(self, campaign_id: str):
        if not _CAMPAIGN_ID.fullmatch(campaign_id):
            raise InvalidResourceId("INVALID_CAMPAIGN_ID")
        try:
            return self.replays.load(campaign_id)
        except ReplayStorageError:
            raise ResourceNotFound("REPLAY_CAMPAIGN_NOT_FOUND") from None

    @staticmethod
    def _report_summary(report) -> dict[str, Any]:
        return {
            "report_id": report.report_id, "type": "daily",
            "trade_date": report.trade_date.isoformat(),
            "as_of_time": report.as_of_time.isoformat(),
            "generated_at": report.generated_at.isoformat(), "mode": report.mode,
            "schema_version": report.schema_version,
            "refs": [f"R:{report.report_id}"],
        }


def _availability(value: str) -> str:
    return {
        "READY": "READY", "AVAILABLE": "READY",
        "PARTIAL": "PARTIAL", "DEGRADED": "DEGRADED",
        "DISABLED": "DISABLED", "UNCONFIGURED": "UNCONFIGURED",
        "CORRUPT": "DEGRADED", "EMPTY": "UNAVAILABLE",
        "NONE": "UNAVAILABLE", "UNAVAILABLE": "UNAVAILABLE",
    }.get(value, "UNKNOWN")


def _identity_semantics(mode: ReplayMode) -> str:
    return (
        IdentityTemporalSemantics.OBSERVED_KNOWLEDGE.value
        if mode is ReplayMode.STRICT_OPERATIONAL_PIT
        else IdentityTemporalSemantics.RETROSPECTIVE_EFFECTIVE_TRUTH.value
    )
