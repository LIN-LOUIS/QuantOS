"""Closed replay contracts and temporal policy tests."""

from datetime import date, datetime, timedelta

import pytest

from quantos.config import MARKET_TIMEZONE
from quantos.replay import (
    HistoricalDatasetManifest, ReplayClock, ReplayEvidence, ReplayPoint,
    ReplayResult, ReplayUniverse, evaluate_historical_evidence,
    knowledge_is_visible, report_is_visible,
)


T1 = datetime(2026, 8, 3, 18, tzinfo=MARKET_TIMEZONE)
T2 = T1 + timedelta(days=1)


def test_universe_and_clock_are_bounded_and_canonical():
    universe = ReplayUniverse(
        ("600519.SH",), "A_SHARE", date(2026, 8, 3), date(2026, 8, 4),
        (date(2026, 8, 3), date(2026, 8, 4)),
    )
    clock = ReplayClock(T1)

    assert universe.to_dict()["symbols"] == ["600519.SH"]
    assert clock.now() == clock.now() == T1
    with pytest.raises(ValueError):
        ReplayUniverse((), "A_SHARE", date(2026, 8, 3), date(2026, 8, 4), ())
    with pytest.raises(ValueError):
        ReplayClock(datetime(2026, 8, 3, 18))


def test_dataset_identity_ignores_import_wall_clock_but_changes_with_records():
    first = HistoricalDatasetManifest.build(
        provider="tushare", market="A_SHARE", symbols=("600519.SH",),
        start_date=date(2026, 8, 3), end_date=date(2026, 8, 4),
        trading_dates=(date(2026, 8, 3), date(2026, 8, 4)),
        record_refs=("daily:1", "daily:2"), imported_at=T2,
    )
    repeated = HistoricalDatasetManifest.build(
        provider="tushare", market="A_SHARE", symbols=("600519.SH",),
        start_date=date(2026, 8, 3), end_date=date(2026, 8, 4),
        trading_dates=(date(2026, 8, 3), date(2026, 8, 4)),
        record_refs=("daily:1", "daily:2"), imported_at=T2 + timedelta(hours=1),
    )
    changed = HistoricalDatasetManifest.build(
        provider="tushare", market="A_SHARE", symbols=("600519.SH",),
        start_date=date(2026, 8, 3), end_date=date(2026, 8, 4),
        trading_dates=(date(2026, 8, 3), date(2026, 8, 4)),
        record_refs=("daily:1",), imported_at=T2,
    )

    assert first.dataset_id == repeated.dataset_id
    assert first.dataset_id != changed.dataset_id
    assert HistoricalDatasetManifest.from_dict(first.to_dict()) == first


def test_result_semantic_hash_excludes_trace_timing_and_runtime():
    point = ReplayPoint.build(
        symbol="600519.SH", trading_date=date(2026, 8, 3), as_of_time=T1,
        dataset_ref="a" * 64, security_snapshot_ref="b" * 64,
    )
    first = ReplayResult(
        point.replay_id, T1, "600519.SH", "PASS", True, True, True, True,
        True, True, ({"ref": "M1", "close": "100"},), ("M1",), ("OK",),
        "trace-a", None, 1.2, 0, 0,
    )
    second = ReplayResult(
        point.replay_id, T1, "600519.SH", "PASS", True, True, True, True,
        True, True, ({"ref": "M1", "close": "100"},), ("M1",), ("OK",),
        "trace-b", None, 999.0, 0, 0,
    )

    assert first.semantic_hash == second.semantic_hash


def test_historical_evidence_rejects_future_unknown_and_non_strict_causality():
    values = (
        ReplayEvidence("strict", T1 - timedelta(hours=2), T1 - timedelta(hours=1), True, "cninfo"),
        ReplayEvidence("future", T1 + timedelta(hours=1), T1 - timedelta(hours=1), True, "cninfo"),
        ReplayEvidence("unknown", None, T1 - timedelta(hours=1), False, "tavily"),
        ReplayEvidence("proxy", T1 - timedelta(hours=2), T1 - timedelta(hours=1), False, "tavily"),
        ReplayEvidence("learned-late", T1 - timedelta(days=1), T1 + timedelta(hours=1), True, "cninfo"),
    )

    result = evaluate_historical_evidence(values, cutoff=T1)

    assert result.visible_refs == ("proxy", "strict")
    assert result.strict_refs == ("strict",)
    assert result.causal_eligible_refs == ("strict",)
    assert result.pit_rejection_count == 3
    assert "NON_STRICT_PIT_EVIDENCE" in result.reason_codes


def test_knowledge_and_report_temporal_boundaries_fail_closed():
    assert knowledge_is_visible(
        available_at=T1, published_at=T1 - timedelta(days=1),
        effective_from=None, effective_to=None, cutoff=T1,
    ) is True
    assert knowledge_is_visible(
        available_at=T2, published_at=T1, effective_from=None,
        effective_to=None, cutoff=T1,
    ) is False
    assert report_is_visible(as_of_time=T1, generated_at=T1, cutoff=T1) is True
    assert report_is_visible(as_of_time=T1, generated_at=T2, cutoff=T1) is False


def test_campaign_summary_aggregates_pass_partial_failed_without_accuracy_claims():
    common = dict(
        as_of_time=T1, symbol="600519.SH", market_available=True,
        identity_available=True, evidence_available=False,
        strict_evidence_available=False, attribution_available=False,
        knowledge_available=False, facts=(), references=(), trace_id=None,
        run_id=None, duration_ms=1.0, pit_rejection_count=0,
        provider_failure_count=0,
    )
    from quantos.replay import ReplaySummary
    values = (
        ReplayResult("1" * 64, status="PASS", reason_codes=("OK",), **common),
        ReplayResult("2" * 64, status="PARTIAL", reason_codes=("NO_EVIDENCE",), **common),
        ReplayResult("3" * 64, status="FAILED", reason_codes=("MARKET_DATA_UNAVAILABLE",),
                     **{**common, "market_available": False}),
    )

    summary = ReplaySummary.from_results(values, deterministic_mismatch_count=0)

    assert (summary.pass_points, summary.partial_points, summary.failed_points) == (1, 1, 1)
    assert "accuracy" not in summary.to_dict()
