"""Offline policy, PIT, dependency and local-artifact execution acceptance."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import socket

import pytest

from quantos.config import MARKET_TIMEZONE, Settings
from quantos.orchestration import (
    EXECUTION_ORDER, HARD_DEPENDENCIES, SOFT_DEPENDENCIES, ModuleOutcome,
    build_run_plan, collect_readiness, execute_run, load_local_artifacts,
    local_artifact_handlers,
)
from quantos.schemas.run import (
    ArtifactReadiness, ArtifactReference, ModuleAvailabilityStatus as A,
    ModuleExecutionStatus as E, ModuleName as M, RunContext, RunType,
)
from quantos.storage.run import manifest_record

DAY = date(2026, 8, 31)
AT = datetime(2026, 8, 31, 20, tzinfo=MARKET_TIMEZONE)
EARLY = AT.replace(hour=15)
REF = ArtifactReference("fixture", "fixture://canonical-artifact", "v1")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("public network forbidden in unit tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def observation(module, *, day=DAY, available=EARLY, mode=None, count=None, strict=False, **kwargs):
    return ArtifactReadiness(module, day, available, REF, mode=mode,
                             eligible_evidence_count=count, strict_pit=strict, **kwargs)


def facts(mode="research", *, count=0):
    return tuple(observation(module, mode=mode if module in {M.EVIDENCE, M.ATTRIBUTION, M.SYNTHESIS, M.DAILY_REPORT} else None,
                             count=count if module == M.ATTRIBUTION else None, strict=mode == "strict_live",
                             market_event_time=EARLY if module == M.ATTRIBUTION else None)
                 for module in M)


def setup_run(*, artifacts=None, run_type=RunType.POST_CLOSE, target=DAY, cutoff=AT,
              mode="research", llm=False, top_n=20):
    readiness = collect_readiness(target_trade_date=target, as_of_time=cutoff,
                                  run_type=run_type, mode=mode,
                                  artifacts=facts(mode) if artifacts is None else artifacts)
    context = RunContext(target, readiness.market_basis_trade_date, cutoff, run_type,
                         mode, "shse_szse_a_share", top_n, llm, AT)
    return context, readiness


def plans(context, readiness):
    return {x.module: x for x in build_run_plan(context, readiness).entries}


def handlers(calls, failures=()):
    def make(module):
        def run(context, preceding):
            calls.append(module)
            assert context.llm_allowed is False
            if module in failures:
                raise RuntimeError("secret-invalid-response Authorization fake-credential")
            return ModuleOutcome((REF,), report_id="fixture-report" if module == M.DAILY_REPORT else None)
        return run
    return {module: make(module) for module in M}


def test_context_deterministic_identity_excludes_generation():
    context, _ = setup_run()
    assert context.run_id == replace(context, generated_at=AT + timedelta(days=1)).run_id
    assert len(context.run_id) == 64


@pytest.mark.parametrize("change", [
    {"llm_allowed": True}, {"top_n": 1}, {"mode": "strict_live"},
    {"as_of_time": AT + timedelta(seconds=1)}, {"market_basis_trade_date": None},
    {"target_trade_date": DAY + timedelta(days=1)}, {"universe_name": "other"},
])
def test_context_identity_covers_policy_inputs(change):
    context, _ = setup_run()
    assert replace(context, **change).run_id != context.run_id


@pytest.mark.parametrize("change", [
    {"as_of_time": AT.replace(tzinfo=None)}, {"generated_at": AT.replace(tzinfo=None)},
    {"run_type": "POST_CLOSE"}, {"run_type": None}, {"mode": ""},
    {"top_n": 0}, {"top_n": True}, {"llm_allowed": 1},
    {"market_basis_trade_date": DAY + timedelta(days=1)},
])
def test_context_rejects_invalid_or_implicit_inputs(change):
    context, _ = setup_run()
    with pytest.raises(ValueError):
        replace(context, **change)


@pytest.mark.parametrize("run_type", [RunType.PRE_OPEN, RunType.INTRADAY])
def test_previous_completed_basis_is_observed_not_calendar_minus_one(run_type):
    friday = DAY - timedelta(days=3)
    data = [observation(M.MARKET_CONTEXT, day=friday, available=EARLY - timedelta(days=3)),
            observation(M.MARKET_CONTEXT, completed_daily=False),
            observation(M.MARKET_CONTEXT, available=AT + timedelta(days=1))]
    context, readiness = setup_run(run_type=run_type, artifacts=data)
    assert context.market_basis_trade_date == friday
    assert readiness.market_target_available is False
    assert plans(context, readiness)[M.MARKET_CONTEXT].reason_code == "COMPLETED_DAILY_BACKGROUND_ONLY"


@pytest.mark.parametrize("run_type", [RunType.PRE_OPEN, RunType.INTRADAY])
def test_background_never_uses_target_even_if_daily_artifact_claims_complete(run_type):
    context, readiness = setup_run(run_type=run_type)
    assert context.market_basis_trade_date is None
    assert plans(context, readiness)[M.MARKET_CONTEXT].availability == A.SKIP_DATA_MISSING


@pytest.mark.parametrize("run_type", list(RunType))
def test_market_readiness_cannot_come_from_another_mode_report(run_type):
    target = DAY if run_type == RunType.POST_CLOSE else DAY + timedelta(days=1)
    context, ready = setup_run(run_type=run_type, target=target, mode="strict_live",
        artifacts=[observation(M.MARKET_CONTEXT, mode="research")])
    assert ready.market_basis_trade_date is None
    assert plans(context, ready)[M.MARKET_CONTEXT].availability != A.READY


@pytest.mark.parametrize("run_type", [RunType.PRE_OPEN, RunType.INTRADAY])
def test_background_event_chain_and_report_are_unsupported(run_type):
    context, readiness = setup_run(run_type=run_type, target=DAY + timedelta(days=1))
    plan = plans(context, readiness)
    for module in (M.EVIDENCE, M.ATTRIBUTION, M.SYNTHESIS, M.DAILY_REPORT):
        assert plan[module].availability == A.SKIP_UNSUPPORTED_IN_V1
    assert readiness.current_intraday_market_data == "UNSUPPORTED_IN_V1"
    calls = []
    result = execute_run(context, readiness, handlers=handlers(calls))
    assert not result.summary.report_generated
    assert M.DAILY_REPORT not in calls
    text = json.dumps(manifest_record(result))
    assert "current_price" not in text and "return_pct" not in text


@pytest.mark.parametrize("hour,expected", [(16, A.SKIP_NOT_AVAILABLE_YET), (20, A.READY)])
@pytest.mark.parametrize("run_type", [RunType.POST_CLOSE, RunType.PRE_OPEN])
def test_fundflow_uses_actual_availability_not_expected_publish_time(hour, expected, run_type):
    data = tuple(replace(x, available_at=AT.replace(hour=19)) if x.module == M.FUND_FLOW else x for x in facts())
    context, readiness = setup_run(artifacts=data, cutoff=AT.replace(hour=hour), run_type=run_type,
                                   target=DAY if run_type == RunType.POST_CLOSE else DAY + timedelta(days=1))
    plan = plans(context, readiness)
    assert plan[M.FUND_FLOW].availability == expected
    if hour == 16:
        assert plan[M.FUND_FLOW].reason_code == "DATA_AVAILABLE_AFTER_AS_OF"
        assert readiness.fund_flow_next_available_at == AT.replace(hour=19)
    if run_type == RunType.POST_CLOSE:
        assert plan[M.DAILY_REPORT].will_execute


def test_postclose_target_ready_and_no_silent_previous_fallback():
    context, readiness = setup_run()
    assert context.market_basis_trade_date == DAY
    data = [observation(M.MARKET_CONTEXT, available=AT + timedelta(seconds=1)),
            observation(M.MARKET_CONTEXT, day=DAY - timedelta(days=3))]
    context, readiness = setup_run(artifacts=data)
    assert readiness.market_previous_available
    assert context.market_basis_trade_date is None
    assert plans(context, readiness)[M.MARKET_CONTEXT].availability == A.SKIP_NOT_AVAILABLE_YET
    assert not plans(context, readiness)[M.DAILY_REPORT].will_execute


def test_artifact_computed_after_cutoff_is_not_ready():
    context, readiness = setup_run(artifacts=[observation(M.MARKET_CONTEXT, artifact_as_of_time=AT + timedelta(seconds=1))])
    assert context.market_basis_trade_date is None


@pytest.mark.parametrize("failed,blocked", [
    (M.MARKET_CONTEXT, M.ANOMALY_TRIAGE), (M.ANOMALY_TRIAGE, M.EVIDENCE),
    (M.EVIDENCE, M.ATTRIBUTION), (M.ATTRIBUTION, M.SYNTHESIS),
])
def test_execution_hard_dependencies_propagate_without_retry(failed, blocked):
    context, readiness = setup_run()
    calls = []
    manifest = execute_run(context, readiness, handlers=handlers(calls, {failed}))
    plan = {x.module: x for x in manifest.plan}
    assert plan[blocked].availability == A.BLOCKED_DEPENDENCY
    assert blocked not in calls and calls.count(failed) == 1
    assert next(x for x in manifest.execution_results if x.module == failed).status == E.FAIL
    if failed in {M.EVIDENCE, M.ATTRIBUTION}:
        assert manifest.summary.report_generated


@pytest.mark.parametrize("soft", [M.SECTOR_CONTEXT, M.FUND_FLOW, M.SYNTHESIS])
def test_soft_failure_does_not_block_report_and_is_visible_to_handler(soft):
    context, readiness = setup_run()
    calls = []
    adapter = handlers(calls, {soft})
    original = adapter[M.DAILY_REPORT]
    def report(context, preceding):
        assert next(x for x in preceding if x.module == soft).status == E.FAIL
        return original(context, preceding)
    adapter[M.DAILY_REPORT] = report
    manifest = execute_run(context, readiness, handlers=adapter)
    assert manifest.summary.report_generated and manifest.summary.failed_modules == 1


@pytest.mark.parametrize("missing", [M.SECTOR_CONTEXT, M.FUND_FLOW])
def test_soft_missing_allows_report(missing):
    context, readiness = setup_run(artifacts=[x for x in facts() if x.module != missing])
    plan = plans(context, readiness)
    assert plan[missing].availability == A.SKIP_DATA_MISSING
    assert plan[M.DAILY_REPORT].will_execute


@pytest.mark.parametrize("mode", ["research", "strict_live"])
@pytest.mark.parametrize("cache,count,expected", [(True, 1, A.READY), (False, 0, A.READY), (False, 1, A.SKIP_POLICY_DISABLED)])
def test_no_llm_cache_and_fast_path_planning(mode, cache, count, expected):
    data = [x for x in facts(mode, count=count) if cache or x.module != M.SYNTHESIS]
    context, readiness = setup_run(mode=mode, artifacts=data)
    assert context.llm_allowed is False
    assert plans(context, readiness)[M.SYNTHESIS].availability == expected
    assert plans(context, readiness)[M.DAILY_REPORT].will_execute


def test_explicit_llm_allowed_can_plan_generation_without_invoking_it():
    context, readiness = setup_run(artifacts=[x for x in facts(count=1) if x.module != M.SYNTHESIS], llm=True)
    assert plans(context, readiness)[M.SYNTHESIS].reason_code == "ELIGIBLE_EVIDENCE_GENERATION_ALLOWED"
    manifest = execute_run(context, readiness, handlers={}, plan_only=True)
    assert manifest.summary.real_llm_requests == 0


def test_plan_only_no_callbacks_no_provider_no_report_and_stable_order():
    context, readiness = setup_run()
    def forbidden(*args):
        raise AssertionError("business modules must not execute")
    first = build_run_plan(context, readiness)
    assert first == build_run_plan(context, readiness)
    assert tuple(x.module for x in first.entries) == EXECUTION_ORDER
    manifest = execute_run(context, readiness, handlers={m: forbidden for m in M}, plan_only=True)
    assert manifest.summary.executed_modules == manifest.summary.real_llm_requests == 0
    assert manifest.summary.skipped_modules == 8
    assert not manifest.summary.report_generated
    assert all(x.started_at is None and x.status == E.SKIP for x in manifest.execution_results)


@pytest.mark.parametrize("mutation", ["proxy", "research", "late"])
def test_strict_attribution_cannot_fallback(mutation):
    data = list(facts("strict_live", count=1))
    index = [x.module for x in data].index(M.ATTRIBUTION)
    changes = {"strict_pit": False} if mutation == "proxy" else {"mode": "research"} if mutation == "research" else {"available_at": EARLY + timedelta(hours=1)}
    data[index] = replace(data[index], **changes)
    context, readiness = setup_run(artifacts=data, mode="strict_live")
    assert readiness.attribution_strict_available is False
    assert not plans(context, readiness)[M.SYNTHESIS].will_execute


def test_missing_sector_never_uses_other_date():
    data = [replace(x, trade_date=DAY + timedelta(days=1)) if x.module == M.SECTOR_CONTEXT else x for x in facts()]
    context, readiness = setup_run(artifacts=data)
    assert not readiness.sector_basis_available
    assert not plans(context, readiness)[M.SECTOR_CONTEXT].will_execute


def test_no_fallback_and_exception_contents_never_escape_manifest():
    context, readiness = setup_run()
    calls = []
    manifest = execute_run(context, readiness, handlers=handlers(calls, {M.EVIDENCE}))
    raw = json.dumps(manifest_record(manifest)).lower()
    for text in ("secret-invalid-response", "authorization", "fake-credential", "system_health", "fund_flow_score", "ranking_score"):
        assert text not in raw
    assert calls.count(M.EVIDENCE) == 1
    assert M.ATTRIBUTION not in calls


def test_readiness_must_match_context():
    context, readiness = setup_run()
    with pytest.raises(ValueError):
        build_run_plan(replace(context, mode="strict_live"), readiness)


def test_execution_is_sequential_topological():
    context, readiness = setup_run()
    calls = []
    execute_run(context, readiness, handlers=handlers(calls))
    assert tuple(calls) == EXECUTION_ORDER
    assert HARD_DEPENDENCIES[M.DAILY_REPORT] == (M.MARKET_CONTEXT, M.ANOMALY_TRIAGE)
    assert SOFT_DEPENDENCIES[M.DAILY_REPORT] == (M.SECTOR_CONTEXT, M.FUND_FLOW, M.ATTRIBUTION, M.SYNTHESIS)


def test_no_llm_execution_does_not_call_disabled_synthesis():
    context, readiness = setup_run(artifacts=[x for x in facts(count=2) if x.module != M.SYNTHESIS])
    calls = []
    manifest = execute_run(context, readiness, handlers=handlers(calls))
    assert M.SYNTHESIS not in calls
    assert manifest.summary.real_llm_requests == 0
    assert manifest.summary.report_generated


def test_missing_handler_fails_once_and_blocks_hard_dependents():
    context, readiness = setup_run()
    manifest = execute_run(context, readiness, handlers={})
    assert manifest.execution_results[0].safe_error_code == "MODULE_UNAVAILABLE"
    assert manifest.plan[2].availability == A.BLOCKED_DEPENDENCY


def test_local_report_reuse_respects_generation_cutoff_and_no_llm(tmp_path):
    from tests.test_reporting import build_report
    from quantos.storage.report import DailyReportRepository
    report, _, _ = build_report(tmp_path / "fixtures", strict=True, no_llm=True)
    settings = Settings.from_project_root(tmp_path / "local")
    path, _ = DailyReportRepository(settings).write(report)
    cutoff = max(report.generated_at, report.as_of_time)
    kwargs = dict(target_trade_date=report.trade_date, mode="strict_live", top_n=2,
                  universe_name=report.universe_name)
    obs = load_local_artifacts(settings, as_of_time=cutoff, **kwargs)
    context, readiness = setup_run(artifacts=obs, mode="strict_live", cutoff=cutoff, top_n=2)
    manifest = execute_run(context, readiness, handlers=local_artifact_handlers(readiness))
    assert manifest.summary.report_reused and not manifest.summary.report_generated
    assert manifest.summary.report_id == report.report_id
    assert manifest.summary.real_llm_requests == 0
    assert manifest.execution_results[-1].artifact_refs[0].path == str(path)
    earlier, state = setup_run(artifacts=obs, mode="strict_live", cutoff=cutoff - timedelta(seconds=1), top_n=2)
    assert earlier.market_basis_trade_date is None
    assert not plans(earlier, state)[M.DAILY_REPORT].will_execute


def test_local_probe_does_not_change_ranking_fundflow_or_health(tmp_path):
    from tests.test_reporting import build_report
    from quantos.storage.report import DailyReportRepository
    report, _, _ = build_report(tmp_path / "fixtures", no_llm=True)
    settings = Settings.from_project_root(tmp_path / "local")
    json_path, markdown = DailyReportRepository(settings).write(report)
    before = (json_path.read_bytes(), markdown.read_bytes())
    cutoff = max(report.generated_at, report.as_of_time)
    obs = load_local_artifacts(settings, target_trade_date=report.trade_date, as_of_time=cutoff,
                               mode="research", top_n=2, universe_name=report.universe_name)
    context, ready = setup_run(artifacts=obs, cutoff=cutoff, top_n=2)
    manifest = execute_run(context, ready, handlers=local_artifact_handlers(ready))
    assert manifest.summary.report_reused
    assert (json_path.read_bytes(), markdown.read_bytes()) == before


def test_local_reuse_matches_frozen_phase3b_decimal_reader(tmp_path):
    from decimal import Decimal
    from tests.test_reporting import build_report
    from quantos.storage.report import DailyReportRepository
    report, _, _ = build_report(tmp_path / "fixtures", no_llm=True)
    first = report.candidate_briefs[0]
    first = replace(first, market_facts={**first.market_facts, "return_zscore": Decimal("12.68138852092433245207272755")})
    report = replace(report, candidate_briefs=(first, *report.candidate_briefs[1:]))
    settings = Settings.from_project_root(tmp_path / "local")
    DailyReportRepository(settings).write(report)
    cutoff = max(report.generated_at, report.as_of_time)
    obs = load_local_artifacts(settings, target_trade_date=report.trade_date, as_of_time=cutoff,
                               mode="research", top_n=2, universe_name=report.universe_name)
    context, ready = setup_run(artifacts=obs, cutoff=cutoff, top_n=2)
    result = execute_run(context, ready, handlers=local_artifact_handlers(ready))
    assert result.summary.report_reused


def test_completed_local_bar_probe_excludes_unfinished_and_future(tmp_path):
    import duckdb
    settings = Settings.from_project_root(tmp_path)
    directory = settings.normalized_market_dir / "trade_date=2026-08-28" / "market=shse_szse"
    directory.mkdir(parents=True)
    path = directory / "fixture.parquet"
    with duckdb.connect(":memory:") as db:
        db.execute("CREATE TABLE fixture(timestamp TIMESTAMPTZ, available_at TIMESTAMPTZ, frequency VARCHAR, schema_version VARCHAR)")
        db.executemany("INSERT INTO fixture VALUES (?, ?, ?, ?)", [
            ("2026-08-28T15:00:00+08:00", "2026-08-28T18:00:00+08:00", "1d", "v1"),
            ("2026-08-31T10:00:00+08:00", "2026-08-31T10:00:00+08:00", "1d", "v1"),
            ("2026-08-31T15:00:00+08:00", "2026-09-02T19:00:00+08:00", "1d", "v1"),
            ("2026-09-01T15:00:00+08:00", "2026-09-01T09:00:00+08:00", "1d", "v1"),
        ])
        db.execute("COPY fixture TO ? (FORMAT PARQUET)", [str(path)])
    cutoff = datetime(2026, 9, 1, 10, 30, tzinfo=MARKET_TIMEZONE)
    obs = load_local_artifacts(settings, target_trade_date=date(2026, 9, 1), as_of_time=cutoff,
                               mode="research", top_n=20, universe_name="shse_szse_a_share")
    context, ready = setup_run(artifacts=obs, target=date(2026, 9, 1), cutoff=cutoff, run_type=RunType.INTRADAY)
    assert context.market_basis_trade_date == date(2026, 8, 28)


def test_cli_plan_only_persists_manifest_without_business_or_network(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("run_quantos_cli_test", Path("scripts/run_quantos.py"))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "DEFAULT_SETTINGS", Settings.from_project_root(tmp_path))
    monkeypatch.setattr(cli, "load_local_artifacts", lambda *args, **kwargs: facts())
    calls = []
    monkeypatch.setattr(cli, "local_artifact_handlers", lambda readiness: handlers(calls))
    assert cli.main(["--trade-date", "2026-08-31", "--as-of-time", AT.isoformat(),
                     "--run-type", "POST_CLOSE", "--mode", "research", "--no-llm", "--plan-only"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert calls == []
    assert output["summary"]["real_llm_requests"] == output["provider_network_requests"] == 0
    assert Path(output["manifest_path"]).exists()
    assert not list((tmp_path / "data" / "derived" / "reports").rglob("*.json"))


@pytest.mark.parametrize("scenario", ["cache", "empty", "miss"])
def test_no_llm_actual_phase3b_gate_with_fake_client(tmp_path, scenario):
    from quantos.reporting import _no_llm_batch
    from quantos.synthesis import FakeLLMClient, synthesize_evidence
    from quantos.storage.synthesis import SynthesisRepository
    from quantos.synthesis_runtime import SynthesisCacheKey
    from tests.test_synthesis import synthesis_input, valid_payload, NOW
    value = synthesis_input(no_evidence=scenario == "empty")
    repo = SynthesisRepository(Settings.from_project_root(tmp_path))
    client = FakeLLMClient(valid_payload())
    if scenario == "cache":
        result = synthesize_evidence(value, client, clock=lambda: NOW)
        key = SynthesisCacheKey.for_input(value, model_provider="fake", model_name="fake-structured-v1", reasoning_config="default/high")
        repo.write(result, cache_key=key.as_dict())
    before = len(client.calls)
    context, ready = setup_run(artifacts=[x for x in facts(count=0 if scenario == "empty" else 1)
                                         if scenario == "cache" or x.module != M.SYNTHESIS])
    # The report layer invokes the exact frozen --no-llm gate, regardless of
    # whether synthesis itself was policy-skipped by the planner.
    seen = []
    adapter = handlers([])
    def report(context, preceding):
        assert not context.llm_allowed
        batch = _no_llm_batch([value], repo, model_provider="fake", model_name="fake-structured-v1", reasoning_config="default/high")
        seen.append(batch)
        return ModuleOutcome((REF,), report_id="fixture-report")
    adapter[M.DAILY_REPORT] = report
    result = execute_run(context, ready, handlers=adapter)
    assert result.summary.real_llm_requests == 0 and len(client.calls) == before
    assert seen[0].actual_llm_request_count == 0
    assert seen[0].results[0].status == {"cache": "CACHE_HIT", "empty": "NO_EVIDENCE_FAST_PATH", "miss": "FAIL"}[scenario]


def test_local_report_mutation_after_plan_fails_closed(tmp_path):
    from tests.test_reporting import build_report
    from quantos.storage.report import DailyReportRepository
    report, _, _ = build_report(tmp_path / "fixtures", no_llm=True)
    settings = Settings.from_project_root(tmp_path / "local")
    path, _ = DailyReportRepository(settings).write(report)
    cutoff = max(report.generated_at, report.as_of_time)
    obs = load_local_artifacts(settings, target_trade_date=report.trade_date, as_of_time=cutoff,
                               mode="research", top_n=2, universe_name=report.universe_name)
    context, ready = setup_run(artifacts=obs, cutoff=cutoff, top_n=2)
    path.write_bytes(path.read_bytes() + b" ")
    result = execute_run(context, ready, handlers=local_artifact_handlers(ready))
    assert result.execution_results[0].status == E.FAIL
    assert not result.summary.report_reused


@pytest.mark.parametrize("argv", [[], ["--trade-date", "2026-08-31", "--as-of-time", "2026-08-31T20:00:00",
                                      "--mode", "research", "--run-type", "POST_CLOSE"]])
def test_cli_requires_explicit_inputs_and_timezone(argv):
    spec = importlib.util.spec_from_file_location("run_quantos_cli", Path("scripts/run_quantos.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SystemExit) as exc:
        module.main(argv)
    assert exc.value.code == 2
