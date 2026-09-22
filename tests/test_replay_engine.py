"""Offline campaign replay over frozen, manifest-gated QuantOS data."""

from dataclasses import replace
from datetime import date, datetime, timedelta

from quantos.collectors import RawMarketRecord
from quantos.config import MARKET_TIMEZONE, Settings
from quantos.normalizers import MarketNormalizer
from quantos.replay import (
    HistoricalDatasetManifest, HistoricalIdentityAuthority,
    IdentityTemporalSemantics, ReplayCampaignService, ReplayEngine, ReplayMode,
    ReplaySupplementalOutcome,
)
from quantos.replay.identity import HistoricalIdentityArtifactRepository
from quantos.replay.storage import HistoricalDatasetRepository, ReplayCampaignRepository
from quantos.schemas import SecurityMaster
from quantos.storage import MarketDataRepository, SecurityMasterRepository


DAY1 = date(2026, 8, 3)
DAY2 = date(2026, 8, 4)
NOW = datetime(2026, 8, 5, 12, tzinfo=MARKET_TIMEZONE)


def raw(day, record_id, *, available_hour=18):
    available = datetime.combine(day, datetime.min.time(), MARKET_TIMEZONE).replace(
        hour=available_hour
    )
    return RawMarketRecord(
        "tushare", "600519.SH", record_id, NOW,
        {"timestamp": day.isoformat(), "frequency": "1d", "open": "100",
         "high": "103", "low": "99", "close": "102", "prev_close": "100",
         "volume": "10", "amount": "1020", "available_at": available.isoformat()},
    )


def foundation(tmp_path, *, identity_observed=None, record_refs=("daily:1", "daily:2")):
    settings = Settings.from_project_root(tmp_path)
    identity_at = identity_observed or datetime(2026, 8, 2, 18, tzinfo=MARKET_TIMEZONE)
    securities = SecurityMasterRepository(settings)
    securities.write_snapshot((SecurityMaster(
        "600519.SH", "贵州茅台", "SHSE", date(2001, 8, 27), identity_at,
        source="tushare",
    ),), provider_id="tushare", observed_at=identity_at)
    market = MarketDataRepository(settings)
    raws = (raw(DAY1, "daily:1"), raw(DAY2, "daily:2"))
    market.write_raw(raws)
    market.write_bars(tuple(MarketNormalizer().normalize(item) for item in raws))
    manifest = HistoricalDatasetManifest.build(
        provider="tushare", market="A_SHARE", symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY2, trading_dates=(DAY1, DAY2),
        record_refs=record_refs, imported_at=NOW,
    )
    HistoricalDatasetRepository(settings).save(manifest)
    return settings, manifest


class Supplemental:
    def __init__(self, by_date=None):
        self.by_date = by_date or {}

    def evaluate(self, *, symbol, cutoff):
        return self.by_date.get(cutoff.date(), ReplaySupplementalOutcome(
            False, False, False, False, False, (),
            ("ATTRIBUTION_INSUFFICIENT", "EVIDENCE_UNAVAILABLE",
             "KNOWLEDGE_TEMPORAL_UNVERIFIED", "REPORT_NOT_VISIBLE"), 0,
        ))


def complete_outcome():
    return ReplaySupplementalOutcome(
        True, True, True, True, True, ("E:strict", "K:1", "R:1"), (), 0,
    )


def service(settings, supplemental):
    engine = ReplayEngine(
        settings=settings, supplemental=supplemental,
        security_repository=SecurityMasterRepository(settings),
        market_repository=MarketDataRepository(settings),
    )
    return ReplayCampaignService(
        settings=settings, engine=engine, clock=lambda: NOW,
        code_version=lambda: ("deadbeef", False),
    )


def test_campaign_runs_existing_ask_core_with_one_canonical_cutoff(tmp_path):
    settings, manifest = foundation(tmp_path)
    replay = service(settings, Supplemental({DAY1: complete_outcome(), DAY2: complete_outcome()}))

    campaign = replay.run(
        dataset_id=manifest.dataset_id, symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY2,
    )

    assert campaign.summary.pass_points == 2
    assert campaign.summary.failed_points == 0
    assert campaign.summary.deterministic_mismatch_count == 0
    assert {item.as_of_time.hour for item in campaign.results} == {18}
    assert all(item.trace_id for item in campaign.results)
    assert all(item.facts[0]["source_record_id"] in manifest.record_refs
               for item in campaign.results)
    assert ReplayCampaignRepository(settings).load(campaign.campaign_id) == campaign


def test_missing_optional_inputs_is_partial_and_generates_failures(tmp_path):
    settings, manifest = foundation(tmp_path)

    campaign = service(settings, Supplemental()).run(
        dataset_id=manifest.dataset_id, symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY1,
    )

    assert campaign.summary.partial_points == 1
    assert campaign.results[0].reason_codes == (
        "ATTRIBUTION_INSUFFICIENT", "EVIDENCE_UNAVAILABLE",
        "KNOWLEDGE_TEMPORAL_UNVERIFIED", "REPORT_NOT_VISIBLE",
    )
    failures = ReplayCampaignRepository(settings).load_failures(campaign.campaign_id)
    assert failures[0].stage == "supplemental"


def test_future_identity_is_not_backfilled_into_replay(tmp_path):
    future = datetime(2026, 8, 5, 18, tzinfo=MARKET_TIMEZONE)
    settings, manifest = foundation(tmp_path, identity_observed=future)

    campaign = service(settings, Supplemental()).run(
        dataset_id=manifest.dataset_id, symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY1,
    )

    assert campaign.results[0].status == "FAILED"
    assert campaign.results[0].reason_codes == ("IDENTITY_HISTORY_UNAVAILABLE",)


def test_strict_and_retrospective_campaigns_keep_distinct_identity_semantics(tmp_path):
    future = datetime(2026, 8, 5, 18, tzinfo=MARKET_TIMEZONE)
    settings, manifest = foundation(tmp_path, identity_observed=future)
    source = SecurityMasterRepository(settings).list_snapshots()[0]
    artifact = HistoricalIdentityAuthority().derive(
        source, generated_at=future + timedelta(minutes=1),
    )
    HistoricalIdentityArtifactRepository(settings).save(artifact)
    replay = service(
        settings, Supplemental({DAY1: complete_outcome(), DAY2: complete_outcome()}),
    )

    strict = replay.run(
        dataset_id=manifest.dataset_id, symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY2,
    )
    retrospective = replay.run(
        dataset_id=manifest.dataset_id, symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY2,
        replay_mode=ReplayMode.RETROSPECTIVE_RECONSTRUCTED,
        identity_authority_id=artifact.artifact_id,
    )

    assert strict.summary.failed_points == 2
    assert strict.summary.operational_identity_pit_rejection_count == 2
    assert retrospective.summary.pass_points == 2
    assert retrospective.summary.identity_covered == 2
    assert retrospective.summary.market_covered == 2
    assert all(item.facts and item.trace_id for item in retrospective.results)
    assert retrospective.results[0].identity_temporal_semantics is (
        IdentityTemporalSemantics.RETROSPECTIVE_EFFECTIVE_TRUTH
    )
    assert strict.configuration_hash != retrospective.configuration_hash
    assert strict.campaign_id != retrospective.campaign_id
    assert retrospective.identity_authority_refs == (artifact.artifact_id,)


def test_orphan_or_unmanifested_market_record_is_not_visible(tmp_path):
    settings, manifest = foundation(tmp_path, record_refs=("daily:2",))

    campaign = service(settings, Supplemental()).run(
        dataset_id=manifest.dataset_id, symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY1,
    )

    assert campaign.results[0].status == "FAILED"
    assert campaign.results[0].reason_codes == ("MARKET_DATA_UNAVAILABLE",)


def test_campaign_hashes_ignore_trace_and_runtime_nondeterminism(tmp_path):
    settings, manifest = foundation(tmp_path)
    replay = service(settings, Supplemental({DAY1: complete_outcome()}))

    first = replay.run(
        dataset_id=manifest.dataset_id, symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY1,
    )
    second = replay.run(
        dataset_id=manifest.dataset_id, symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY1,
    )

    assert first.results[0].trace_id != second.results[0].trace_id
    assert first.results[0].semantic_hash == second.results[0].semantic_hash
    assert first.configuration_hash == second.configuration_hash


def test_campaign_freezes_security_snapshots_before_point_execution(tmp_path):
    settings, manifest = foundation(tmp_path)
    repository = SecurityMasterRepository(settings)

    class RefreshingSecurityRepository:
        def __init__(self):
            self.refreshed = False

        def _refresh(self):
            if self.refreshed:
                return
            self.refreshed = True
            observed_at = datetime(2026, 8, 4, 17, tzinfo=MARKET_TIMEZONE)
            repository.write_snapshot((SecurityMaster(
                "600519.SH", "贵州茅台股份", "SHSE", date(2001, 8, 27), observed_at,
                source="tushare",
            ),), provider_id="tushare", observed_at=observed_at)

        def list_snapshots(self):
            frozen = repository.list_snapshots()
            self._refresh()
            return frozen

        def snapshot_as_of(self, as_of_time):
            selected = repository.snapshot_as_of(as_of_time)
            self._refresh()
            return selected

        def list_from_snapshot(self, snapshot_id, *, as_of_time):
            return repository.list_from_snapshot(snapshot_id, as_of_time=as_of_time)

    security = RefreshingSecurityRepository()
    engine = ReplayEngine(
        settings=settings,
        supplemental=Supplemental({DAY1: complete_outcome(), DAY2: complete_outcome()}),
        security_repository=security,
        market_repository=MarketDataRepository(settings),
    )
    replay = ReplayCampaignService(
        settings=settings, engine=engine, clock=lambda: NOW,
        code_version=lambda: ("deadbeef", False),
    )

    campaign = replay.run(
        dataset_id=manifest.dataset_id, symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY2,
    )

    assert campaign.summary.pass_points == 2
    assert len(campaign.security_snapshot_refs) == 1


def test_future_market_fact_from_storage_boundary_is_rejected(tmp_path):
    settings, manifest = foundation(tmp_path)

    class FutureMarket:
        def read_by_symbol(self, *_args, **_kwargs):
            item = MarketNormalizer().normalize(raw(DAY1, "daily:1"))
            return [replace(
                item, available_at=datetime(2026, 8, 4, 18,
                                             tzinfo=MARKET_TIMEZONE),
            )]

    engine = ReplayEngine(
        settings=settings, supplemental=Supplemental(),
        security_repository=SecurityMasterRepository(settings),
        market_repository=FutureMarket(),
    )
    replay = ReplayCampaignService(
        settings=settings, engine=engine, clock=lambda: NOW,
        code_version=lambda: ("deadbeef", False),
    )

    campaign = replay.run(
        dataset_id=manifest.dataset_id, symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY1,
    )

    assert campaign.results[0].reason_codes == ("PIT_REJECTED",)
    assert campaign.summary.pit_rejection_count == 1


def test_retrospective_identity_does_not_relax_market_pit(tmp_path):
    settings, manifest = foundation(tmp_path)
    source = SecurityMasterRepository(settings).list_snapshots()[0]
    artifact = HistoricalIdentityAuthority().derive(
        source, generated_at=NOW + timedelta(minutes=1),
    )
    HistoricalIdentityArtifactRepository(settings).save(artifact)

    class FutureMarket:
        def read_by_symbol(self, *_args, **_kwargs):
            item = MarketNormalizer().normalize(raw(DAY1, "daily:1"))
            return [replace(
                item, available_at=datetime(2026, 8, 4, 18,
                                             tzinfo=MARKET_TIMEZONE),
            )]

    engine = ReplayEngine(
        settings=settings, supplemental=Supplemental(),
        security_repository=SecurityMasterRepository(settings),
        market_repository=FutureMarket(),
    )
    replay = ReplayCampaignService(
        settings=settings, engine=engine, clock=lambda: NOW,
        code_version=lambda: ("deadbeef", False),
    )

    campaign = replay.run(
        dataset_id=manifest.dataset_id, symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY1,
        replay_mode=ReplayMode.RETROSPECTIVE_RECONSTRUCTED,
        identity_authority_id=artifact.artifact_id,
    )

    assert campaign.results[0].reason_codes == ("PIT_REJECTED",)
    assert campaign.summary.market_pit_violation_count == 1


def test_supplemental_exception_degrades_with_structured_failure(tmp_path):
    settings, manifest = foundation(tmp_path)

    class BrokenSupplemental:
        def evaluate(self, **_kwargs):
            raise RuntimeError("Authorization: secret provider response")

    replay = service(settings, BrokenSupplemental())

    campaign = replay.run(
        dataset_id=manifest.dataset_id, symbols=("600519.SH",),
        start_date=DAY1, end_date=DAY1,
    )

    result = campaign.results[0]
    assert result.status == "PARTIAL"
    assert result.reason_codes == ("PIPELINE_FAILED",)
    assert result.facts
    assert "secret" not in str(campaign.to_dict())
