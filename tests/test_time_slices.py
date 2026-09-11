"""Offline acceptance for time-sliced product views; no provider execution."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import socket

import pytest

from quantos.config import MARKET_TIMEZONE as TZ, Settings
from quantos.orchestration import ModuleOutcome, collect_readiness, execute_run
from quantos.reporting import canonical_json_bytes, render_daily_report, report_to_dict
from quantos.schemas.run import (
    ArtifactReadiness, ArtifactReference, ModuleAvailabilityStatus as A,
    ModuleExecutionStatus as E, ModuleName as M, RunContext, RunType,
)
from quantos.schemas.time_slice import (
    ProductAvailability as P, ReportType, TemporalBucket as B,
)
from quantos.storage.report import DailyReportRepository
from quantos.storage.run import RunRepository
from quantos.storage.time_slice import TimeSliceRepository
from quantos.time_slices import (
    INTRADAY_DISCLAIMER, assemble_time_slice, availability_from_manifest,
    classify_temporal_bucket, evidence_window, load_daily_reference,
    render_time_slice, time_slice_from_record, time_slice_to_record,
)
from tests.test_reporting import build_report
from tests.test_synthesis import evidence

DAY = date(2026, 8, 31)
TARGET = date(2026, 9, 1)
CLOSE = datetime(2026, 8, 31, 15, tzinfo=TZ)
OPEN = datetime(2026, 9, 1, 9, 30, tzinfo=TZ)
PRE = OPEN.replace(hour=9, minute=0)
INTRA = OPEN.replace(hour=10, minute=30)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network is forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def record(identity, at, *, strict=True, available=None, provider="fixture"):
    value = evidence(identity, strict=strict)
    return replace(value, published_at=at, collected_at=available or at,
                   available_at=available or at, provider=provider)


def bucket(at, *, available=None, strict=True, mode="strict_live", previous=False, **kw):
    values = dict(published_at=at, available_at=available or at,
        previous_market_event_time=CLOSE - timedelta(hours=1), previous_close_time=CLOSE,
        current_open_time=OPEN, as_of_time=INTRA, mode=mode, strict_pit=strict,
        previous_attribution=previous)
    values.update(kw)
    return classify_temporal_bucket(**values)


@pytest.mark.parametrize("at,previous,expected", [
    (CLOSE - timedelta(hours=2), True, B.PREVIOUS_ATTRIBUTION),
    (CLOSE - timedelta(minutes=30), False, B.POST_EVENT_CONTEXT),
    (CLOSE + timedelta(hours=4), False, B.OVERNIGHT_CONTEXT),
    (PRE, False, B.PRE_OPEN_CONTEXT),
    (OPEN, False, B.SINCE_OPEN_CONTEXT),
])
def test_all_temporal_buckets(at, previous, expected):
    assert bucket(at, previous=previous) == expected


def test_previous_attribution_requires_existing_eligibility_not_just_time():
    assert bucket(CLOSE - timedelta(hours=2)) is None


def test_postclose_never_backflows_into_previous_attribution():
    assert bucket(CLOSE + timedelta(seconds=1), previous=True) == B.OVERNIGHT_CONTEXT


def test_strict_uses_late_available_not_earlier_publication():
    assert bucket(CLOSE - timedelta(hours=2), available=PRE, previous=True) == B.PRE_OPEN_CONTEXT


def test_research_proxy_retains_publication_window_and_never_upgrades_strict():
    assert bucket(CLOSE + timedelta(hours=3), available=PRE, strict=False, mode="research") == B.OVERNIGHT_CONTEXT
    assert bucket(CLOSE + timedelta(hours=3), available=PRE, strict=False) is None


@pytest.mark.parametrize("mode", ["research", "strict_live"])
def test_future_knowledge_excluded_in_both_modes(mode):
    assert bucket(PRE, available=INTRA + timedelta(hours=1), mode=mode, strict=mode == "strict_live") is None


def test_unknown_proxy_publication_unavailable():
    assert bucket(None, available=PRE, strict=False, mode="research") is None


@pytest.mark.parametrize("at,expected", [
    (CLOSE, B.POST_EVENT_CONTEXT),
    (datetime(2026, 9, 1, tzinfo=TZ), B.PRE_OPEN_CONTEXT),
    (OPEN - timedelta(microseconds=1), B.PRE_OPEN_CONTEXT),
    (OPEN, B.SINCE_OPEN_CONTEXT),
])
def test_exact_window_boundaries(at, expected):
    assert bucket(at) == expected


def test_naive_or_reversed_window_rejected():
    with pytest.raises(ValueError):
        bucket(PRE.replace(tzinfo=None))
    with pytest.raises(ValueError):
        bucket(PRE, previous_close_time=OPEN)


def test_same_day_close_cannot_define_previous_overnight_window():
    with pytest.raises(ValueError, match="window"):
        bucket(PRE, previous_close_time=PRE - timedelta(minutes=1))


def test_postclose_manifest_identity_references_must_agree(tmp_path):
    report, _, _, _, _ = product(tmp_path, run_type=RunType.POST_CLOSE, cutoff=CLOSE.replace(hour=20))
    ref = replace(report.payload.run_manifest_ref, artifact_id="0" * 64)
    with pytest.raises(ValueError, match="reference"):
        replace(report, payload=replace(report.payload, run_manifest_ref=ref))


def test_intraday_sector_return_alias_is_not_a_permitted_product_field(tmp_path):
    report, _, _, _, _ = product(tmp_path, run_type=RunType.INTRADAY, cutoff=INTRA)
    with pytest.raises(ValueError, match="unsupported"):
        replace(report, payload=replace(report.payload,
            previous_market_context={"intraday_sector_return": 0.1}))


@pytest.mark.parametrize("mode,strict", [("strict_live", True), ("research", False)])
@pytest.mark.parametrize("point,expected", [
    (CLOSE - timedelta(hours=1), B.PREVIOUS_ATTRIBUTION),
    (CLOSE, B.POST_EVENT_CONTEXT),
    (OPEN.replace(hour=0, minute=0), B.PRE_OPEN_CONTEXT),
    (OPEN, B.SINCE_OPEN_CONTEXT),
    (INTRA, B.SINCE_OPEN_CONTEXT),
])
def test_audit_exact_equalities_and_asof_inclusive(mode, strict, point, expected):
    assert bucket(point, mode=mode, strict=strict, previous=True) == expected
    assert bucket(INTRA + timedelta(microseconds=1), mode=mode, strict=strict) is None


def test_research_corpus_gate_matches_repository_without_becoming_strict(tmp_path):
    from quantos.storage.news import NewsEvidenceRepository
    from tests.test_calibration import attribution, result, EVENT, COLLECTED
    from quantos.synthesis import build_synthesis_input
    repository = NewsEvidenceRepository(Settings.from_project_root(tmp_path))
    item = result("audit-proxy", published=EVENT - timedelta(hours=1))
    repository.write_web_search_results([item])
    assert repository.query_web_search_results(as_of_time=COLLECTED - timedelta(microseconds=1)) == []
    visible = repository.query_web_search_results(as_of_time=COLLECTED)
    selection = attribution(visible, ["title_company_name"], as_of=COLLECTED)
    fact = selection.attribution_facts[0]
    assert fact.research_attribution_eligible and not fact.strict_attribution_eligible
    assert fact.knowledge_temporal_relation == "known_after_event"
    research = build_synthesis_input(selection.bundles[0], selection.attribution_facts,
        mode="research", company_name="绿色动力", market_event_time=EVENT)
    strict = build_synthesis_input(selection.bundles[0], selection.attribution_facts,
        mode="strict_live", company_name="绿色动力", market_event_time=EVENT)
    assert len(research.attribution_evidence) == 1 and strict.attribution_evidence == ()


def test_research_smoke_manifest_does_not_invent_fast_path_or_cache(tmp_path):
    _, daily, _, manifest, _, _ = fixture_run(tmp_path, run_type=RunType.POST_CLOSE, cutoff=CLOSE.replace(hour=20))
    assert sum(x.evidence_stats.eligible_attribution_count for x in daily.candidate_briefs) > 0
    plan = next(x for x in manifest.plan if x.module == M.SYNTHESIS)
    assert plan.availability == A.SKIP_POLICY_DISABLED
    assert plan.reason_code == "LLM_DISABLED"


def fixture_run(root, *, run_type=RunType.PRE_OPEN, mode="research", cutoff=PRE, plan_only=False):
    """Explicit synthetic canonical artifacts, never backdated production data."""
    settings = Settings.from_project_root(root)
    daily, factory, _ = build_report(root / "source", strict=mode == "strict_live", no_llm=True)
    assert factory.calls == 0
    post = run_type == RunType.POST_CLOSE
    source_time = cutoff if post else CLOSE.replace(hour=20)
    if post and cutoff.hour < 19:
        daily = replace(daily, candidate_briefs=tuple(replace(x, fund_flow={"available": False}) for x in daily.candidate_briefs),
            data_quality={**daily.data_quality, "fund_flow": {"covered_candidates": 0, "target_candidates": 2, "coverage": 0}})
    daily = replace(daily, as_of_time=source_time, generated_at=source_time,
                    provenance={**daily.provenance, "quantos_git_commit": None})
    daily = replace(daily, report_id=hashlib.sha256(canonical_json_bytes(report_to_dict(daily))).hexdigest())
    source_path, _ = DailyReportRepository(settings).write(daily)
    source_ref = ArtifactReference(hashlib.sha256(source_path.read_bytes()).hexdigest(), str(source_path), daily.schema_version)
    eligible_count = sum(x.evidence_stats.eligible_attribution_count for x in daily.candidate_briefs)
    observations = tuple(ArtifactReadiness(module, DAY,
        CLOSE.replace(hour=19) if module == M.FUND_FLOW else source_time, source_ref,
        mode=mode, strict_pit=mode == "strict_live", eligible_evidence_count=eligible_count if module == M.ATTRIBUTION else None)
        for module in M if module not in {M.SECTOR_CONTEXT, M.SYNTHESIS})
    target = DAY if post else TARGET
    readiness = collect_readiness(target_trade_date=target, as_of_time=cutoff, run_type=run_type,
                                  mode=mode, artifacts=observations)
    context = RunContext(target, readiness.market_basis_trade_date, cutoff, run_type, mode,
                         daily.universe_name, 2, False, cutoff)
    def handler(context, preceding):
        return ModuleOutcome((source_ref,), report_id=daily.report_id, report_reused=True)
    manifest = execute_run(context, readiness, handlers={m: handler for m in M}, plan_only=plan_only,
                           clock=lambda: cutoff)
    manifest_path = RunRepository(settings).write(manifest)
    manifest_ref = ArtifactReference(context.run_id, str(manifest_path), manifest.schema_version)
    records = (record("overnight", CLOSE + timedelta(hours=3)), record("pre-open", PRE - timedelta(minutes=10)),
               record("since-open", OPEN + timedelta(minutes=30)),
               record("proxy", CLOSE + timedelta(hours=4), strict=False, available=PRE - timedelta(minutes=20)))
    return settings, daily, source_ref, manifest, manifest_ref, records


def product(root, **kw):
    settings, daily, ref, manifest, manifest_ref, records = fixture_run(root, **kw)
    report = assemble_time_slice(manifest, manifest_ref=manifest_ref, daily_report=daily,
        daily_report_ref=ref, records=records, generated_at=manifest.run_context.as_of_time)
    return report, daily, manifest, records, settings


def test_preopen_payload_preserves_source_facts_and_ranking(tmp_path):
    report, daily, _, records, _ = product(tmp_path)
    assert report.report_type == ReportType.PRE_OPEN_BRIEF
    assert report.payload.previous_market_overview == daily.market_overview
    assert report.payload.previous_anomaly_overview == daily.anomaly_overview
    assert [x.rank for x in report.payload.watchlist] == [x.rank for x in daily.candidate_briefs]
    assert report.payload.watchlist[0].previous_market_facts == daily.candidate_briefs[0].market_facts
    assert report.payload.watchlist[0].overnight_evidence_count == 2
    assert report.payload.watchlist[0].pre_open_evidence_count == 1
    assert "OVERNIGHT_EVENT_PRESENT" in report.payload.watchlist[0].watch_reason_codes
    assert daily.candidate_briefs[0].synthesis.used_evidence_ids == ()
    assert records[0].result_id == "overnight"


def test_context_views_preserve_metadata_and_provider_independence(tmp_path):
    report, _, _, records, _ = product(tmp_path)
    window = report.payload.overnight_evidence_overview
    original = {x.result_id: x for x in records}
    for view in window.views:
        assert (view.published_at, view.available_at, view.strict_pit) == (
            original[view.evidence_id].published_at, original[view.evidence_id].available_at,
            original[view.evidence_id].strict_pit)
    assert window.providers == ({"provider": "fixture", "record_count": 3, "candidate_coverage": 1},)


def test_strict_excludes_proxy_and_research_labels_actual_proxy(tmp_path):
    research, _, _, _, _ = product(tmp_path / "r")
    strict, _, _, _, _ = product(tmp_path / "s", mode="strict_live")
    assert research.data_quality["historical_proxy_used"] is True
    assert strict.data_quality["historical_proxy_used"] is False
    assert {x.evidence_id for x in strict.payload.overnight_evidence_overview.views} == {"overnight", "pre-open"}


def test_research_without_proxy_not_mechanically_true(tmp_path):
    _, daily, ref, manifest, manifest_ref, records = fixture_run(tmp_path)
    result = assemble_time_slice(manifest, manifest_ref=manifest_ref, daily_report=daily,
        daily_report_ref=ref, records=records[:2], generated_at=PRE)
    assert result.data_quality["historical_proxy_used"] is False


@pytest.mark.parametrize("run_type,cutoff", [(RunType.PRE_OPEN, PRE), (RunType.INTRADAY, INTRA)])
def test_no_current_numeric_fields_prediction_or_invalid_model_prose(tmp_path, run_type, cutoff):
    report, _, _, _, _ = product(tmp_path, run_type=run_type, cutoff=cutoff)
    text = canonical_json_bytes(time_slice_to_record(report)).decode()
    markdown = render_time_slice(report)
    for key in ("today_market_overview", "current_price", "current_return", "intraday_anomaly", "target_price",
                "expected_return", "prediction", "buy_sell_hold", "Authorization", "possible_explanations"):
        assert key not in text
    for phrase in ("预计高开", "预计上涨", "看多", "看空", "推荐买入", "主力进场"):
        assert phrase not in markdown


def test_intraday_previous_background_and_since_open_only(tmp_path):
    report, daily, _, _, _ = product(tmp_path, run_type=RunType.INTRADAY, cutoff=INTRA)
    assert report.payload.market_data_status == {"canonical_intraday_supported": False, "status": "UNSUPPORTED_IN_V1"}
    assert report.payload.previous_market_context["market_overview"] == daily.market_overview
    assert [v.evidence_id for v in report.payload.evidence_since_open.views] == ["since-open"]
    assert report.payload.candidate_event_updates[0]["new_event_count_since_open"] == 1
    assert report.payload.candidate_event_updates[0]["latest_evidence_available_at"] == OPEN + timedelta(minutes=30)
    assert INTRADAY_DISCLAIMER in render_time_slice(report).split("## 报告信息")[0]
    assert "## 最近完成交易日背景" in render_time_slice(report)


def test_intraday_watchlist_does_not_rerank_from_new_events(tmp_path):
    pre, _, _, _, _ = product(tmp_path / "pre")
    intra, _, _, _, _ = product(tmp_path / "intra", run_type=RunType.INTRADAY, cutoff=INTRA)
    assert pre.payload.watchlist == intra.payload.watchlist


@pytest.mark.parametrize("hour,expected", [(16, P.UNAVAILABLE), (20, P.READY)])
def test_postclose_reuses_daily_core_and_manifest_fund_availability(tmp_path, hour, expected):
    report, daily, _, _, _ = product(tmp_path, run_type=RunType.POST_CLOSE, cutoff=CLOSE.replace(hour=hour))
    assert report.payload.availability_summary["fund_flow"] == expected
    assert not hasattr(report.payload, "anomaly_overview")
    md = render_time_slice(report, daily_report_loader=load_daily_reference)
    assert md.startswith(render_daily_report(load_daily_reference(report.payload.daily_intelligence_report_ref)))
    assert report.provenance["source_daily_report_id"] == daily.report_id


def test_cutoff_changes_product_identity(tmp_path):
    early, _, _, _, _ = product(tmp_path / "early", run_type=RunType.POST_CLOSE, cutoff=CLOSE.replace(hour=16))
    late, _, _, _, _ = product(tmp_path / "late", run_type=RunType.POST_CLOSE, cutoff=CLOSE.replace(hour=20))
    assert early.report_id != late.report_id


def test_generated_at_and_manifest_generation_path_do_not_change_identity(tmp_path):
    report, _, _, _, _ = product(tmp_path)
    newer = replace(report, generated_at=INTRA, run_context_ref=replace(report.run_context_ref, path="another-generation.json"))
    assert report.report_id == newer.report_id


def test_availability_reads_only_manifest_results(tmp_path):
    _, _, manifest, _, _ = product(tmp_path)
    altered = replace(manifest, execution_results=tuple(replace(x, status=E.FAIL) if x.module == M.FUND_FLOW else x
                                                      for x in manifest.execution_results))
    assert availability_from_manifest(altered)["fund_flow"] == P.FAILED
    assert availability_from_manifest(manifest)["evidence"] == P.UNSUPPORTED


def test_plan_only_cannot_generate_product(tmp_path):
    _, _, _, manifest, ref, _ = fixture_run(tmp_path, plan_only=True)
    with pytest.raises(ValueError, match="plan-only"):
        assemble_time_slice(manifest, manifest_ref=ref, generated_at=PRE)


@pytest.mark.parametrize("mutation", ["future", "wrong_date", "wrong_mode"])
def test_source_pit_and_mode_safety(tmp_path, mutation):
    _, daily, ref, manifest, manifest_ref, _ = fixture_run(tmp_path)
    if mutation == "future":
        daily = replace(daily, generated_at=INTRA)
    elif mutation == "wrong_date":
        daily = replace(daily, trade_date=TARGET)
    else:
        daily = replace(daily, mode="strict_live", data_quality={**daily.data_quality,
            "evidence": {**daily.data_quality["evidence"], "historical_proxy_used": False}})
    with pytest.raises(ValueError):
        assemble_time_slice(manifest, manifest_ref=manifest_ref, daily_report=daily,
                            daily_report_ref=ref, generated_at=PRE)


def test_background_unavailable_is_explicit_not_recalculated(tmp_path):
    _, _, _, manifest, ref, _ = fixture_run(tmp_path)
    report = assemble_time_slice(manifest, manifest_ref=ref, generated_at=PRE)
    assert report.payload.previous_market_overview is None
    assert report.payload.watchlist == ()
    assert report.data_quality["background_available"] is False


def test_postclose_missing_report_is_lightweight_unavailable_wrapper(tmp_path):
    _, _, _, manifest, ref, _ = fixture_run(tmp_path, run_type=RunType.POST_CLOSE, cutoff=CLOSE.replace(hour=16))
    report = assemble_time_slice(manifest, manifest_ref=ref, generated_at=PRE)
    assert report.payload.daily_intelligence_report_ref is None
    assert "DailyIntelligenceReport unavailable" in render_time_slice(report)


def test_failed_synthesis_and_sensitive_evidence_text_never_copied(tmp_path):
    _, daily, ref, manifest, manifest_ref, records = fixture_run(tmp_path)
    records = tuple(replace(x, title="Authorization should-not-persist", snippet="raw invalid model response") for x in records)
    before = canonical_json_bytes(report_to_dict(daily))
    report = assemble_time_slice(manifest, manifest_ref=manifest_ref, daily_report=daily,
        daily_report_ref=ref, records=records, generated_at=PRE)
    persisted = canonical_json_bytes(time_slice_to_record(report))
    assert b"Authorization" not in persisted and b"raw invalid" not in persisted
    assert canonical_json_bytes(report_to_dict(daily)) == before


@pytest.mark.parametrize("run_type,cutoff", [(RunType.PRE_OPEN, PRE), (RunType.INTRADAY, INTRA),
                                           (RunType.POST_CLOSE, CLOSE.replace(hour=20))])
def test_json_roundtrip_and_markdown_deterministic(tmp_path, run_type, cutoff):
    report, _, _, _, _ = product(tmp_path, run_type=run_type, cutoff=cutoff)
    encoded = canonical_json_bytes(time_slice_to_record(report))
    reread = time_slice_from_record(json.loads(encoded, parse_float=Decimal))
    assert canonical_json_bytes(time_slice_to_record(reread)) == encoded
    assert render_time_slice(reread, daily_report_loader=load_daily_reference) == render_time_slice(
        reread, daily_report_loader=load_daily_reference)


def test_cli_plan_only_has_no_products_and_zero_network(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("time_slice_cli", Path("scripts/generate_time_slice_report.py"))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "DEFAULT_SETTINGS", Settings.from_project_root(tmp_path))
    assert cli.main(["--trade-date", "2026-09-01", "--as-of-time", PRE.isoformat(),
        "--run-type", "PRE_OPEN", "--mode", "research", "--no-llm", "--plan-only"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["REAL_DEEPSEEK_REQUEST_COUNT"] == result["PROVIDER_NETWORK_REQUEST_COUNT"] == 0
    assert "json_path" not in result
    assert not (tmp_path / "data/derived/time_slice_reports").exists()


def test_cli_local_late_known_announcement_is_since_open(tmp_path, monkeypatch, capsys):
    from quantos.schemas.news import AnnouncementRecord
    from quantos.storage.news import NewsEvidenceRepository
    settings, _, _, _, _, _ = fixture_run(tmp_path, run_type=RunType.INTRADAY, cutoff=INTRA)
    known = OPEN + timedelta(minutes=1)
    announcement = AnnouncementRecord("late-announcement", "601330.SH", "fixture", "untrusted text", None,
        CLOSE - timedelta(hours=2), known, known, "fixture", "fixture", "original-id", "strict_live")
    NewsEvidenceRepository(settings).write_announcements([announcement])
    spec = importlib.util.spec_from_file_location("time_slice_cli_late", Path("scripts/generate_time_slice_report.py"))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "DEFAULT_SETTINGS", settings)
    assert cli.main(["--trade-date", "2026-09-01", "--as-of-time", INTRA.isoformat(),
        "--run-type", "INTRADAY", "--mode", "research", "--no-llm", "--top-n", "2"]) == 0
    output = json.loads(capsys.readouterr().out)
    report = TimeSliceRepository(settings).read(output["json_path"])
    assert [x.evidence_id for x in report.payload.evidence_since_open.views] == ["late-announcement"]
    assert output["REAL_DEEPSEEK_REQUEST_COUNT"] == output["PROVIDER_NETWORK_REQUEST_COUNT"] == 0


@pytest.mark.parametrize("field", ["prediction", "target_price", "current_price", "current_return", "intraday_anomaly"])
def test_schema_rejects_predicted_and_pseudo_realtime_fields(tmp_path, field):
    report, _, _, _, _ = product(tmp_path)
    bad = replace(report.payload, previous_market_overview={field: 1})
    with pytest.raises(ValueError):
        replace(report, payload=bad)


def test_schema_rejects_new_ranking_and_directional_watch_reason(tmp_path):
    report, _, _, _, _ = product(tmp_path)
    with pytest.raises(ValueError):
        replace(report, payload=replace(report.payload, watchlist=report.payload.watchlist[::-1]))
    with pytest.raises(ValueError):
        replace(report.payload.watchlist[0], watch_reason_codes=("LIKELY_TO_RISE",))


def test_provider_views_independent_and_exact_duplicates_only():
    one = record("one", PRE, provider="provider-a")
    two = record("two", PRE, provider="provider-b")
    result = evidence_window([one, one, two], symbols={one.query_symbol},
        previous_market_event_time=CLOSE, previous_close_time=CLOSE, current_open_time=OPEN,
        as_of_time=PRE, mode="strict_live")
    assert len(result.views) == 2
    assert [x["provider"] for x in result.providers] == ["provider-a", "provider-b"]
    assert all(x["record_count"] == 1 for x in result.providers)


def test_unknown_proxy_timestamp_not_used():
    item = replace(record("unknown", PRE, strict=False), timestamp_basis="unknown")
    result = evidence_window([item], symbols={item.query_symbol}, previous_market_event_time=CLOSE,
        previous_close_time=CLOSE, current_open_time=OPEN, as_of_time=PRE, mode="research")
    assert result.views == ()


def test_same_id_conflicting_metadata_fails_without_retry():
    item = record("conflict", PRE)
    with pytest.raises(ValueError, match="conflicting"):
        evidence_window([item, replace(item, provider_record_id="different", available_at=PRE + timedelta(minutes=1))],
            symbols={item.query_symbol}, previous_market_event_time=CLOSE, previous_close_time=CLOSE,
            current_open_time=OPEN, as_of_time=INTRA, mode="strict_live")


def test_source_reference_must_come_from_manifest(tmp_path):
    _, daily, ref, manifest, manifest_ref, _ = fixture_run(tmp_path)
    with pytest.raises(ValueError, match="referenced"):
        assemble_time_slice(manifest, manifest_ref=manifest_ref, daily_report=daily,
            daily_report_ref=replace(ref, path="not-in-manifest.json"), generated_at=PRE)


def test_postclose_future_fund_flow_cannot_leak_via_daily_core(tmp_path):
    _, daily, ref, manifest, manifest_ref, _ = fixture_run(tmp_path, run_type=RunType.POST_CLOSE, cutoff=CLOSE.replace(hour=16))
    unsafe = replace(daily, data_quality={**daily.data_quality,
        "fund_flow": {"covered_candidates": 1, "target_candidates": 2, "coverage": 0.5}})
    with pytest.raises(ValueError, match="fund-flow"):
        assemble_time_slice(manifest, manifest_ref=manifest_ref, daily_report=unsafe,
            daily_report_ref=ref, generated_at=PRE)


def test_intraday_late_knowledge_is_update_not_preopen_news(tmp_path):
    _, daily, ref, manifest, manifest_ref, _ = fixture_run(tmp_path, run_type=RunType.INTRADAY, cutoff=INTRA, mode="strict_live")
    late = record("late", PRE, available=OPEN + timedelta(minutes=1))
    report = assemble_time_slice(manifest, manifest_ref=manifest_ref, daily_report=daily,
        daily_report_ref=ref, records=[late], generated_at=INTRA)
    assert report.payload.watchlist[0].pre_open_evidence_count == 0
    assert report.payload.evidence_since_open.views[0].temporal_bucket == B.SINCE_OPEN_CONTEXT


def test_forbidden_extra_record_fields_do_not_enter_view(tmp_path):
    report, _, _, _, _ = product(tmp_path)
    view = report.payload.overnight_evidence_overview.views[0]
    assert not hasattr(view, "content") and not hasattr(view, "title") and not hasattr(view, "snippet")


@pytest.mark.parametrize("link_ready", [True, False])
def test_newsrecord_view_uses_only_existing_pit_visible_mention(link_ready):
    from quantos.schemas.news import NewsMention, NewsRecord
    at = CLOSE + timedelta(hours=3)
    news = NewsRecord("news-original-id", "fixture", "news", at, at, at, "do not copy title",
                      "do not copy body", None, (), "fixture", "original-provider-id", "strict_live")
    mention = NewsMention(news.news_id, "601330.SH", "title_symbol", "601330", True, False, False, 1,
                          at, at if link_ready else INTRA + timedelta(hours=1))
    result = evidence_window([news], symbols={mention.symbol}, mentions=[mention],
        previous_market_event_time=CLOSE, previous_close_time=CLOSE, current_open_time=OPEN,
        as_of_time=PRE, mode="strict_live")
    assert len(result.views) == int(link_ready)
    if link_ready:
        assert result.views[0].evidence_id == news.news_id
        assert result.views[0].published_at == news.published_at
        assert result.views[0].temporal_bucket == B.OVERNIGHT_CONTEXT


def test_postclose_ref_integrity_checked(tmp_path):
    _, daily, ref, _, _, _ = fixture_run(tmp_path)
    with pytest.raises(ValueError, match="changed"):
        load_daily_reference(replace(ref, artifact_id="0" * 64))


def test_numeric_values_remain_json_numbers(tmp_path):
    report, daily, _, _, _ = product(tmp_path)
    data = json.loads(canonical_json_bytes(time_slice_to_record(report)))
    assert isinstance(data["payload"]["previous_market_overview"]["total_amount"], (int, float))
    assert isinstance(data["payload"]["watchlist"][0]["previous_market_facts"]["return_zscore"], (int, float))


def test_five_fixture_smokes(tmp_path):
    results = offline_smokes(tmp_path)
    assert len(results) == 5 and all(x["status"] == "PASS" for x in results)


def offline_smokes(root):
    """Five explicitly fixture-based historical smokes; return safe paths/status only."""
    results = []
    for name, run_type, mode, cutoff in (
        ("pre_research", RunType.PRE_OPEN, "research", PRE),
        ("pre_strict", RunType.PRE_OPEN, "strict_live", PRE),
        ("intraday", RunType.INTRADAY, "research", INTRA),
        ("post_16", RunType.POST_CLOSE, "research", CLOSE.replace(hour=16)),
        ("post_20", RunType.POST_CLOSE, "research", CLOSE.replace(hour=20)),
    ):
        report, _, _, _, settings = product(root / name, run_type=run_type, mode=mode, cutoff=cutoff)
        path, md = TimeSliceRepository(settings).write(report)
        assert TimeSliceRepository(settings).read(path).report_id == report.report_id
        assert md.read_text() == render_time_slice(TimeSliceRepository(settings).read(path), daily_report_loader=load_daily_reference)
        results.append({"case": name, "fixture_based": True, "status": "PASS", "json_path": str(path),
            "markdown_path": str(md), "historical_proxy_used": report.data_quality["historical_proxy_used"],
            "fund_flow": report.payload.availability_summary["fund_flow"],
            "REAL_DEEPSEEK_REQUEST_COUNT": 0, "PROVIDER_NETWORK_REQUEST_COUNT": 0})
    return results
