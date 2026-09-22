"""Offline replay over frozen manifests and the existing canonical Ask core."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import subprocess
import time as runtime_time
from typing import Callable, Protocol

from quantos.ask import AskIntent, AskRequest, AskService, Capability, ToolRegistry
from quantos.ask.adapters import LocalAskCapabilities
from quantos.config import (
    DEFAULT_SETTINGS, MARKET_TIMEZONE, KnowledgeIntegrationSettings, Settings,
)
from quantos.qa import QAMarketFacts
from quantos.storage import (
    AskTraceRepository, MarketDataRepository, SecurityMasterRepository,
    SecurityMasterUnavailableError,
)
from quantos.serialization import canonical_identity_json_bytes

from .contracts import (
    HistoricalDatasetManifest, IdentityTemporalSemantics, ReplayCampaignManifest, ReplayClock,
    ReplayFailureRecord, ReplayPoint, ReplayResult, ReplaySummary, ReplayUniverse,
    ReplayMode,
)
from .identity import (
    HistoricalIdentityArtifactRepository, HistoricalIdentityUnavailableError,
)
from .policy import ReplayEvidence, evaluate_historical_evidence
from .storage import (
    HistoricalDatasetRepository, ReplayCampaignRepository,
)


@dataclass(frozen=True, slots=True)
class ReplaySupplementalOutcome:
    evidence_available: bool
    strict_evidence_available: bool
    attribution_available: bool
    knowledge_available: bool
    report_available: bool
    references: tuple[str, ...]
    reason_codes: tuple[str, ...]
    pit_rejection_count: int

    def __post_init__(self) -> None:
        if tuple(sorted(set(self.references))) != self.references:
            raise ValueError("supplemental references must be sorted and unique")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("supplemental reason codes must be sorted and unique")
        if self.pit_rejection_count < 0:
            raise ValueError("invalid PIT rejection count")
        if self.attribution_available and not self.strict_evidence_available:
            raise ValueError("historical attribution requires strict evidence")


class ReplaySupplementalProvider(Protocol):
    def evaluate(self, *, symbol: str, cutoff: datetime) -> ReplaySupplementalOutcome: ...


class LocalReplaySupplementalProvider:
    """Read existing local evidence/report/knowledge products without network access."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.local = LocalAskCapabilities(
            settings=settings,
            knowledge_settings=KnowledgeIntegrationSettings.from_env(),
        )

    def evaluate(self, *, symbol: str, cutoff: datetime) -> ReplaySupplementalOutcome:
        evidence = self._evidence(symbol, cutoff)
        reasons = set(evidence.reason_codes)
        references = set(evidence.visible_refs)
        report = self.local.report_lookup({
            "query": "historical replay", "symbol": symbol, "as_of_time": cutoff,
        }) if self.local.availability("report.lookup").value == "READY" else None
        report_available = bool(report and report.refs)
        if report_available:
            references.update(report.refs)
        else:
            reasons.add("REPORT_NOT_VISIBLE")
        attribution = self.local.attribution_lookup({
            "symbol": symbol, "as_of_time": cutoff,
        }) if report_available else None
        attribution_available = bool(
            attribution and attribution.causal_allowed and evidence.strict_refs
        )
        if attribution_available:
            references.update(attribution.report_refs)
        else:
            reasons.add("ATTRIBUTION_INSUFFICIENT")
        knowledge_available = False
        if self.local.availability("knowledge.query").value == "READY":
            knowledge = self.local.knowledge_query({
                "query": "historical replay", "symbol": symbol, "as_of_time": cutoff,
            })
            knowledge_available = bool(knowledge.refs)
            references.update(knowledge.refs)
        if not knowledge_available:
            reasons.add("KNOWLEDGE_TEMPORAL_UNVERIFIED")
        if not evidence.visible_refs:
            reasons.add("EVIDENCE_UNAVAILABLE")
        return ReplaySupplementalOutcome(
            bool(evidence.visible_refs), bool(evidence.strict_refs),
            attribution_available, knowledge_available, report_available,
            tuple(sorted(references)), tuple(sorted(reasons)),
            evidence.pit_rejection_count,
        )

    def configuration_state(self) -> dict[str, object]:
        return {
            "evidence": "READY" if any((
                self.settings.web_search_dir.is_dir()
                and any(self.settings.web_search_dir.rglob("*.parquet")),
                self.settings.announcement_dir.is_dir()
                and any(self.settings.announcement_dir.rglob("*.parquet")),
            )) else "UNAVAILABLE",
            "report": self.local.availability("report.lookup").value,
            "knowledge": self.local.availability("knowledge.query").value,
            "knowledge_index_version": self.local.knowledge_settings.lexical_index_version,
            "policy": "strict_historical:v1",
        }

    def _evidence(self, symbol: str, cutoff: datetime):
        from quantos.storage import NewsEvidenceRepository
        records: list[ReplayEvidence] = []
        web_root = self.settings.web_search_dir
        if web_root.is_dir() and any(web_root.rglob("*.parquet")):
            repository = NewsEvidenceRepository(self.settings)
            for item in repository.query_web_search_results(as_of_time=cutoff):
                if item.query_symbol == symbol:
                    records.append(ReplayEvidence(
                        f"W:{item.result_id}", item.published_at, item.available_at,
                        item.strict_pit, item.provider,
                    ))
        announcement_root = self.settings.announcement_dir
        if announcement_root.is_dir() and any(announcement_root.rglob("*.parquet")):
            repository = NewsEvidenceRepository(self.settings)
            for item in repository.query_announcements(
                start_time=cutoff - timedelta(days=7), end_time=cutoff,
                as_of_time=cutoff, symbol=symbol,
            ):
                records.append(ReplayEvidence(
                    f"A:{item.announcement_id}", item.published_at,
                    item.available_at, item.strict_pit, item.provider,
                ))
        return evaluate_historical_evidence(tuple(records), cutoff=cutoff)


class ReplayEngine:
    def __init__(
        self, *, settings: Settings = DEFAULT_SETTINGS,
        supplemental: ReplaySupplementalProvider | None = None,
        security_repository: SecurityMasterRepository | None = None,
        market_repository: MarketDataRepository | None = None,
        identity_repository: HistoricalIdentityArtifactRepository | None = None,
    ) -> None:
        self.settings = settings
        self.supplemental = supplemental or LocalReplaySupplementalProvider(settings)
        self.security = security_repository or SecurityMasterRepository(settings)
        self.market = market_repository or MarketDataRepository(settings)
        self.identities = identity_repository or HistoricalIdentityArtifactRepository(settings)

    def capability_state(self) -> dict[str, object]:
        inspect = getattr(self.supplemental, "configuration_state", None)
        supplemental = inspect() if callable(inspect) else {
            "adapter": type(self.supplemental).__name__,
        }
        return {
            "market": "MANIFEST_GATED", "identity": "SNAPSHOT_PINNED",
            "supplemental": supplemental,
        }

    def execute(self, point: ReplayPoint, dataset: HistoricalDatasetManifest,
                *, persist_trace: bool = True) -> ReplayResult:
        started = runtime_time.monotonic()
        if point.replay_mode is ReplayMode.STRICT_OPERATIONAL_PIT:
            if point.security_snapshot_ref is None:
                return self._failed(
                    point, "IDENTITY_HISTORY_UNAVAILABLE", started,
                    operational_identity_pit_rejection_count=1,
                )
            try:
                securities = self.security.list_from_snapshot(
                    point.security_snapshot_ref, as_of_time=point.as_of_time,
                )
            except SecurityMasterUnavailableError:
                return self._failed(
                    point, "IDENTITY_HISTORY_UNAVAILABLE", started,
                    operational_identity_pit_rejection_count=1,
                )
            matches = tuple(item for item in securities if item.symbol == point.symbol)
            if len(matches) != 1:
                return self._failed(
                    point, "IDENTITY_HISTORY_UNAVAILABLE", started,
                    operational_identity_pit_rejection_count=1,
                )
        else:
            try:
                artifact = self.identities.load(point.identity_authority_ref or "")
                if artifact.source_security_snapshot_id != point.security_snapshot_ref:
                    raise HistoricalIdentityUnavailableError("identity provenance mismatch")
                identity = artifact.resolve_symbol(
                    point.symbol, as_of_date=point.trading_date,
                )
                matches = (identity.to_security_master(),)
            except HistoricalIdentityUnavailableError:
                return self._failed(point, "IDENTITY_HISTORY_UNAVAILABLE", started)
        try:
            bars = tuple(
                item for item in self.market.read_by_symbol(
                    point.symbol, as_of_time=point.as_of_time,
                    start_date=point.trading_date, end_date=point.trading_date,
                )
                if item.source_record_id in dataset.record_refs
            )
        except Exception:
            return self._failed(
                point, "PIPELINE_FAILED", started, identity_available=True,
            )
        if not bars:
            return self._failed(
                point, "MARKET_DATA_UNAVAILABLE", started, identity_available=True,
            )
        if any(item.timestamp > point.as_of_time or item.available_at > point.as_of_time
               for item in bars):
            return self._failed(
                point, "PIT_REJECTED", started, identity_available=True,
                pit_rejection_count=1, market_pit_violation_count=1,
            )
        latest = max(bars, key=lambda item: (item.timestamp, item.source_record_id))
        previous = latest.prev_close
        change = ((latest.close / previous - Decimal(1)) * 100) if previous else None
        fact = QAMarketFacts(
            "M1", latest.timestamp.date().isoformat(), str(latest.close),
            str(previous) if previous is not None else None,
            str(change.quantize(Decimal("0.01"))) if change is not None else None,
            latest.volume, str(latest.amount), latest.source,
            latest.source_record_id, latest.available_at,
        )
        registry = ToolRegistry((Capability(
            "market.snapshot", (("symbol", str), ("as_of_time", datetime)),
            (AskIntent.MARKET_OVERVIEW,), lambda _args: fact, QAMarketFacts,
        ),))
        clock = ReplayClock(point.as_of_time)
        trace_repository = (
            AskTraceRepository(settings=self.settings) if persist_trace else None
        )
        try:
            response = AskService(
                securities=lambda _as_of: matches, registry=registry, llm=None,
                trace_repository=trace_repository, clock=clock.now,
                identity_visibility=(
                    _retrospective_identity_visible
                    if point.replay_mode is ReplayMode.RETROSPECTIVE_RECONSTRUCTED
                    else None
                ),
            ).ask(AskRequest(
                "最近一个交易日表现怎么样？", point.as_of_time,
                entity_hints=(point.symbol,), no_research=True,
            ))
        except Exception:
            return self._failed(
                point, "PIPELINE_FAILED", started, identity_available=True,
            )
        try:
            supplemental = self.supplemental.evaluate(
                symbol=point.symbol, cutoff=point.as_of_time,
            )
        except Exception:
            supplemental = ReplaySupplementalOutcome(
                False, False, False, False, False, (), ("PIPELINE_FAILED",), 0,
            )
        reasons = tuple(sorted(set(supplemental.reason_codes)))
        status = "PASS" if not reasons else "PARTIAL"
        references = tuple(sorted({"M1", *supplemental.references}))
        return ReplayResult(
            point.replay_id, point.as_of_time, point.symbol, status, True, True,
            supplemental.evidence_available, supplemental.strict_evidence_available,
            supplemental.attribution_available, supplemental.knowledge_available,
            response.facts, references, reasons or ("OK",), response.trace_id,
            response.trace.run_id, (runtime_time.monotonic() - started) * 1000,
            supplemental.pit_rejection_count, 0, point.replay_mode,
            _identity_semantics(point.replay_mode), 0, 0,
            supplemental.pit_rejection_count, 0, 0,
        )

    @staticmethod
    def _failed(point: ReplayPoint, code: str, started: float, *,
                identity_available: bool = False,
                pit_rejection_count: int = 0,
                operational_identity_pit_rejection_count: int = 0,
                market_pit_violation_count: int = 0) -> ReplayResult:
        return ReplayResult(
            point.replay_id, point.as_of_time, point.symbol, "FAILED", False,
            identity_available, False, False, False, False, (), (), (code,),
            None, None, (runtime_time.monotonic() - started) * 1000,
            pit_rejection_count, 0, point.replay_mode,
            _identity_semantics(point.replay_mode),
            operational_identity_pit_rejection_count,
            market_pit_violation_count, 0, 0, 0,
        )


class ReplayCampaignService:
    def __init__(self, *, settings: Settings = DEFAULT_SETTINGS,
                 engine: ReplayEngine | None = None,
                 clock: Callable[[], datetime] | None = None,
                 code_version: Callable[[], tuple[str, bool]] | None = None,
                 identity_repository: HistoricalIdentityArtifactRepository | None = None) -> None:
        self.settings = settings
        self.engine = engine or ReplayEngine(settings=settings)
        self.clock = clock or (lambda: datetime.now(MARKET_TIMEZONE))
        self.code_version = code_version or _code_version
        self.datasets = HistoricalDatasetRepository(settings)
        self.campaigns = ReplayCampaignRepository(settings)
        self.identities = identity_repository or HistoricalIdentityArtifactRepository(settings)

    def run(self, *, dataset_id: str, symbols: tuple[str, ...],
            start_date: date, end_date: date,
            verify_determinism: bool = True,
            replay_mode: ReplayMode = ReplayMode.STRICT_OPERATIONAL_PIT,
            identity_authority_id: str | None = None) -> ReplayCampaignManifest:
        dataset = self.datasets.load_visible(dataset_id)
        if not set(symbols) <= set(dataset.symbols):
            raise ValueError("requested symbols are outside the frozen dataset")
        dates = tuple(day for day in dataset.trading_dates
                      if start_date <= day <= end_date)
        universe = ReplayUniverse(tuple(sorted(symbols)), dataset.market,
                                  start_date, end_date, dates)
        if not isinstance(replay_mode, ReplayMode):
            raise ValueError("invalid replay mode")
        authority = None
        if replay_mode is ReplayMode.RETROSPECTIVE_RECONSTRUCTED:
            if identity_authority_id is None:
                raise ValueError("retrospective replay requires identity authority")
            authority = self.identities.load(identity_authority_id)
            source_snapshot = self.engine.security.snapshot_by_id(
                authority.source_security_snapshot_id
            )
            if (source_snapshot.provider_id != authority.source_provider
                    or source_snapshot.observed_at != authority.records[0].source_observed_at):
                raise ValueError("historical identity artifact provenance mismatch")
        elif identity_authority_id is not None:
            raise ValueError("strict replay cannot use retrospective identity authority")
        frozen_snapshots = self.engine.security.list_snapshots()
        boundaries: list[tuple[date, datetime, str | None]] = []
        snapshot_refs: set[str] = set()
        for day in dates:
            cutoff = datetime.combine(day, time(18, 0), tzinfo=MARKET_TIMEZONE)
            visible = tuple(item for item in frozen_snapshots
                            if item.observed_at <= cutoff)
            preferred = tuple(item for item in visible
                              if item.provider_id == "tushare")
            snapshot = (
                self.engine.security.snapshot_by_id(authority.source_security_snapshot_id)
                if authority is not None else
                max(preferred or visible, key=lambda item: (
                    item.observed_at, item.snapshot_id,
                )) if visible else None
            )
            snapshot_ref = snapshot.snapshot_id if snapshot else None
            if snapshot_ref:
                snapshot_refs.add(snapshot_ref)
            boundaries.append((day, cutoff, snapshot_ref))
        configuration_hash = hashlib.sha256(canonical_identity_json_bytes({
            "universe": universe.to_dict(), "dataset_id": dataset.dataset_id,
            "cutoff_time": "18:00:00+08:00", "policy": "replay:v1",
            "offline": True,
            "replay_mode": replay_mode.value,
            "identity_authority_id": identity_authority_id,
            "identity_snapshot_by_date": [
                [day.isoformat(), snapshot_ref]
                for day, _cutoff, snapshot_ref in boundaries
            ],
            "capability_availability": self.engine.capability_state(),
        })).hexdigest()
        points: list[ReplayPoint] = []
        for day, cutoff, snapshot_ref in boundaries:
            for symbol in universe.symbols:
                points.append(ReplayPoint.build(
                    symbol=symbol, trading_date=day, as_of_time=cutoff,
                    dataset_ref=dataset.dataset_id,
                    security_snapshot_ref=snapshot_ref,
                    replay_mode=replay_mode,
                    identity_authority_ref=identity_authority_id,
                ))
        results: list[ReplayResult] = []
        mismatches = 0
        for point in points:
            result = self.engine.execute(point, dataset, persist_trace=True)
            if verify_determinism:
                check = self.engine.execute(point, dataset, persist_trace=False)
                if check.semantic_hash != result.semantic_hash:
                    mismatches += 1
                    result = replace(
                        result, status="FAILED",
                        reason_codes=tuple(sorted({*result.reason_codes,
                                                   "DETERMINISTIC_MISMATCH"})),
                    )
            results.append(result)
        materialized = tuple(results)
        summary = ReplaySummary.from_results(
            materialized, deterministic_mismatch_count=mismatches,
        )
        version, dirty = self.code_version()
        manifest = ReplayCampaignManifest.build(
            created_at=self.clock(), universe=universe,
            dataset_refs=(dataset.dataset_id,),
            security_snapshot_refs=tuple(sorted(snapshot_refs)),
            configuration_hash=configuration_hash, code_version=version,
            dirty=dirty, results=materialized, summary=summary,
            replay_mode=replay_mode,
            identity_authority_refs=((identity_authority_id,)
                                     if identity_authority_id else ()),
        )
        failures = tuple(
            ReplayFailureRecord.from_result(
                item, stage=("identity" if "IDENTITY_HISTORY_UNAVAILABLE" in item.reason_codes
                             else "market" if item.status == "FAILED" else "supplemental"),
            )
            for item in materialized if item.status != "PASS"
        )
        self.campaigns.save(manifest, failures=failures)
        return manifest


def _identity_semantics(mode: ReplayMode) -> IdentityTemporalSemantics:
    return (
        IdentityTemporalSemantics.OBSERVED_KNOWLEDGE
        if mode is ReplayMode.STRICT_OPERATIONAL_PIT
        else IdentityTemporalSemantics.RETROSPECTIVE_EFFECTIVE_TRUTH
    )


def _retrospective_identity_visible(item, as_of: datetime) -> bool:
    return (
        item.effective_from <= as_of.date()
        and (item.effective_to is None or item.effective_to >= as_of.date())
        and item.is_active
        and item.source.startswith("retrospective:")
    )


def _code_version() -> tuple[str, bool]:
    try:
        version = subprocess.run(
            ("git", "rev-parse", "HEAD"), check=True, capture_output=True,
            text=True, timeout=2,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ("git", "status", "--porcelain"), check=True, capture_output=True,
            text=True, timeout=2,
        ).stdout.strip())
        return version, dirty
    except Exception:
        return "UNKNOWN", True
