"""Historical identity truth stays separate from operational observation time."""

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from quantos.config import MARKET_TIMEZONE, Settings
from quantos.replay import (
    HistoricalIdentityAuthority, IdentityTemporalSemantics, ReplayEvidence, ReplayMode,
    evaluate_historical_evidence, knowledge_is_visible, report_is_visible,
)
from quantos.replay.identity import (
    HistoricalIdentityArtifact, HistoricalIdentityArtifactRepository,
    HistoricalIdentityUnavailableError,
)
from quantos.schemas import SecurityMaster
from quantos.storage import SecurityMasterRepository


OBSERVED = datetime(2026, 9, 20, 12, 30, tzinfo=MARKET_TIMEZONE)


def snapshot(tmp_path, *, effective_to=None, active=True):
    settings = Settings.from_project_root(tmp_path)
    value = SecurityMasterRepository(settings).write_snapshot((SecurityMaster(
        "600519.SH", "贵州茅台", "SHSE", date(2001, 8, 27), OBSERVED,
        effective_to=effective_to, is_active=active, source="tushare",
        status="listed" if active else "delisted", industry="白酒",
        industry_classification="Tushare stock_basic",
    ),), provider_id="tushare", observed_at=OBSERVED).snapshot
    return settings, value


def test_replay_mode_is_finite_and_defaults_remain_operational():
    assert ReplayMode.STRICT_OPERATIONAL_PIT.value == "STRICT_OPERATIONAL_PIT"
    assert ReplayMode.RETROSPECTIVE_RECONSTRUCTED.value == "RETROSPECTIVE_RECONSTRUCTED"
    assert IdentityTemporalSemantics.OBSERVED_KNOWLEDGE.value == "OBSERVED_KNOWLEDGE"
    with pytest.raises(ValueError):
        ReplayMode("arbitrary")


def test_tushare_authority_derivation_preserves_observation_and_excludes_current_name(tmp_path):
    settings, source = snapshot(tmp_path)
    policy = HistoricalIdentityAuthority()

    artifact = policy.derive(source, generated_at=OBSERVED + timedelta(minutes=1))
    record = artifact.resolve_symbol("600519.SH", as_of_date=date(2026, 8, 20))

    assert artifact.derivation_type == "RETROSPECTIVE_DERIVATION"
    assert artifact.source_security_snapshot_id == source.snapshot_id
    assert record.source_observed_at == OBSERVED
    assert record.authoritative_fields == (
        "effective_from", "effective_to", "exchange", "symbol",
    )
    assert record.temporal_unverified_fields == (
        "aliases", "company_name", "industry",
    )
    assert "贵州茅台" not in str(artifact.to_dict())
    repository = HistoricalIdentityArtifactRepository(settings)
    repository.save(artifact)
    assert repository.load(artifact.artifact_id) == artifact


def test_listing_and_delisting_intervals_are_authoritative(tmp_path):
    _settings, source = snapshot(
        tmp_path, effective_to=date(2026, 8, 31), active=False,
    )
    artifact = HistoricalIdentityAuthority().derive(
        source, generated_at=OBSERVED + timedelta(minutes=1),
    )

    assert artifact.resolve_symbol(
        "600519.SH", as_of_date=date(2026, 8, 20),
    ).exchange == "SHSE"
    with pytest.raises(HistoricalIdentityUnavailableError):
        artifact.resolve_symbol("600519.SH", as_of_date=date(2026, 9, 1))


def test_ambiguous_authoritative_identity_fails_closed(tmp_path):
    _settings, source = snapshot(tmp_path)
    artifact = HistoricalIdentityAuthority().derive(
        source, generated_at=OBSERVED + timedelta(minutes=1),
    )
    duplicate = replace(
        artifact.records[0], canonical_security_id="f" * 64,
        source_record_ref="f" * 64,
    )
    ambiguous = HistoricalIdentityArtifact.build(
        source_security_snapshot_id=artifact.source_security_snapshot_id,
        source_provider=artifact.source_provider,
        generated_at=artifact.generated_at,
        authority_policy_version=artifact.authority_policy_version,
        records=(artifact.records[0], duplicate),
    )

    with pytest.raises(HistoricalIdentityUnavailableError, match="ambiguous"):
        ambiguous.resolve_symbol("600519.SH", as_of_date=date(2026, 8, 20))


def test_provider_field_audits_are_conservative():
    policy = HistoricalIdentityAuthority()

    tushare = policy.field_audit("tushare")
    baostock = policy.field_audit("baostock")

    assert tushare["list_date"] == "HISTORICALLY_EFFECTIVE"
    assert tushare["name"] == "CURRENT_SNAPSHOT_ONLY"
    assert tushare["area"] == "TEMPORAL_UNVERIFIED"
    assert tushare["industry"] == "CURRENT_SNAPSHOT_ONLY"
    assert baostock["ipoDate"] == "HISTORICALLY_EFFECTIVE"
    assert baostock["code_name"] == "CURRENT_SNAPSHOT_ONLY"


def test_retrospective_mode_does_not_relax_other_temporal_policies():
    cutoff = datetime(2026, 8, 20, 18, tzinfo=MARKET_TIMEZONE)
    future = cutoff + timedelta(minutes=1)

    evidence = evaluate_historical_evidence((ReplayEvidence(
        "future", future, future, True, "cninfo",
    ),), cutoff=cutoff)

    assert ReplayMode.RETROSPECTIVE_RECONSTRUCTED.value
    assert evidence.visible_refs == ()
    assert evidence.pit_rejection_count == 1
    assert knowledge_is_visible(
        available_at=future, published_at=cutoff, effective_from=None,
        effective_to=None, cutoff=cutoff,
    ) is False
    assert report_is_visible(
        as_of_time=cutoff, generated_at=future, cutoff=cutoff,
    ) is False
