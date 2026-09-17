"""Shared Daily and TimeSlice command execution over existing core libraries."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import json
from pathlib import Path
import subprocess
from typing import Callable

from quantos.anomalies import detect_stock_anomaly
from quantos.calibration import (
    calibrate_attribution_evidence, calibrate_event_evidence,
    link_stored_query_a_results,
)
from quantos.commands import CommandFailure
from quantos.config import (
    DEFAULT_SETTINGS, KnowledgeIntegrationSettings, LLMSettings, MARKET_TIMEZONE,
    Settings, SynthesisBatchSettings,
)
from quantos.fundflow import build_fund_flow_evidence
from quantos.knowledge_integration import (
    KnowledgePreparationError, prepare_candidate_synthesis_input,
)
from quantos.orchestration import (
    collect_readiness, execute_run, load_local_artifacts,
    load_local_operational_artifacts, local_artifact_handlers,
)
from quantos.reporting import generate_daily_report
from quantos.schemas.run import (
    ArtifactReference, ModuleExecutionStatus as E, ModuleName as M,
    RunContext, RunType,
)
from quantos.snapshots import build_market_snapshot
from quantos.storage import (
    DailyReportRepository, KnowledgeContextRepository,
    KnowledgeLexicalIndexRepository, KnowledgeRepository, MarketDataRepository,
    MoneyFlowRepository, NewsEvidenceRepository, RunRepository,
    SynthesisRepository, TimeSliceRepository,
)
from quantos.storage.market import StorageError
from quantos.synthesis import DeepSeekLLMClient
from quantos.time_slices import assemble_time_slice, load_daily_reference
from quantos.triage import rank_anomaly_candidates


@dataclass(frozen=True, slots=True)
class DailyOptions:
    trade_date: date
    as_of_time: datetime
    mode: str
    top_n: int
    no_llm: bool


@dataclass(frozen=True, slots=True)
class TimeSliceOptions:
    trade_date: date
    as_of_time: datetime
    run_type: RunType
    mode: str
    top_n: int
    no_llm: bool
    plan_only: bool


def add_daily_arguments(parser: argparse.ArgumentParser, *, include_output=True) -> None:
    _add_common_report_arguments(parser)
    if include_output:
        parser.add_argument("--json", action="store_true", dest="json_output")
        parser.add_argument("--project-root", type=Path, default=Path.cwd())


def add_time_slice_arguments(parser: argparse.ArgumentParser, *, include_output=True) -> None:
    _add_common_report_arguments(parser)
    parser.add_argument("--plan-only", action="store_true")
    if include_output:
        parser.add_argument("--json", action="store_true", dest="json_output")
        parser.add_argument("--project-root", type=Path, default=Path.cwd())


def _add_common_report_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--trade-date", required=True, type=date.fromisoformat)
    parser.add_argument("--as-of-time", required=True, type=datetime.fromisoformat)
    parser.add_argument("--mode", required=True, choices=("research", "strict_live"))
    parser.add_argument("--top-n", type=int)
    parser.add_argument("--no-llm", action="store_true")


def validate_common(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.as_of_time.tzinfo is None or args.as_of_time.utcoffset() is None:
        _validation_error(
            args, parser, "TIMEZONE_REQUIRED",
            "--as-of-time must include a timezone offset",
            "Provide an ISO 8601 datetime with an explicit timezone offset.",
        )
    try:
        top_n = (
            args.top_n
            if args.top_n is not None
            else SynthesisBatchSettings.from_env().synthesis_top_n
        )
    except (TypeError, ValueError):
        _validation_error(
            args, parser, "SYNTHESIS_CONFIG_INVALID",
            "synthesis configuration is invalid",
            "Fix the local synthesis configuration or pass --top-n.",
        )
    if top_n < 1:
        _validation_error(
            args, parser, "TOP_N_INVALID", "--top-n must be positive",
            "Pass a positive integer for --top-n.",
        )
    return top_n


def validate_time_slice(
    args: argparse.Namespace, parser: argparse.ArgumentParser, run_type: RunType,
) -> int:
    top_n = validate_common(args, parser)
    opened = datetime.combine(args.trade_date, time(9, 30), MARKET_TIMEZONE)
    if (
        run_type is RunType.PRE_OPEN and args.as_of_time > opened
        or run_type is RunType.INTRADAY and args.as_of_time < opened
    ):
        _validation_error(
            args, parser, "PRODUCT_WINDOW_INVALID",
            "cutoff does not match product window",
            "Use a timezone-aware cutoff inside the requested product window.",
        )
    return top_n


def _validation_error(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    error_code: str,
    message: str,
    remediation: str,
) -> None:
    if getattr(args, "json_output", False):
        raise CommandFailure(error_code, remediation, 2)
    parser.error(message)


def execute_daily(
    options: DailyOptions,
    *,
    settings: Settings = DEFAULT_SETTINGS,
    clock: Callable[[], datetime] | None = None,
    prepare_inputs=None,
) -> dict[str, object]:
    """Execute the existing Daily chain once and publish its existing product."""
    target_market_dir = (
        settings.normalized_market_dir / f"trade_date={options.trade_date.isoformat()}"
    )
    if not tuple(target_market_dir.rglob("*.parquet")):
        raise CommandFailure(
            "MARKET_ARTIFACTS_MISSING",
            "Prepare canonical local market artifacts for the requested trade date.",
            3,
        )
    clock = clock or (lambda: datetime.now(MARKET_TIMEZONE))
    prepare_inputs = prepare_inputs or prepare_synthesis_inputs
    try:
        market, anomalies, candidates, flows, _current_bars = market_artifacts(
            settings, options.trade_date, options.as_of_time,
        )
        names = stored_company_names(settings, options.trade_date)
        records = NewsEvidenceRepository(settings).query_web_search_results(
            as_of_time=options.as_of_time,
            pit_mode=(
                "source_timestamp_proxy"
                if options.mode == "research" else "strict_live"
            ),
        )
        market_event_time = datetime.combine(
            options.trade_date,
            datetime.min.time().replace(hour=15),
            tzinfo=MARKET_TIMEZONE,
        )
        event = calibrate_event_evidence(
            records,
            link_stored_query_a_results(records),
            market_event_time=market_event_time,
        )
        attribution = calibrate_attribution_evidence(
            event, candidates, flows,
            market_event_time=market_event_time,
            as_of_time=options.as_of_time,
        )
        selected_bundles = attribution.bundles[:options.top_n]
        generated_at = clock()
        knowledge_settings = KnowledgeIntegrationSettings.from_env()
        synthesis_inputs = prepare_inputs(
            selected_bundles,
            attribution.attribution_facts,
            company_names=names,
            mode=options.mode,
            market_event_time=market_event_time,
            research_corpus_cutoff=(
                options.as_of_time if options.mode == "research" else None
            ),
            generated_at=generated_at,
            knowledge_settings=knowledge_settings,
            settings=settings,
        )
        llm_settings = LLMSettings.from_env()
        if not options.no_llm and llm_settings.reasoning_effort is not None:
            raise CommandFailure(
                "LLM_REASONING_CONFIG_INVALID",
                "Use the supported default/high reasoning configuration.",
                2,
            )
        report = generate_daily_report(
            market_snapshot=market, sector_snapshots=(), anomalies=anomalies,
            candidates=candidates, company_names=names, attribution=attribution,
            synthesis_inputs=synthesis_inputs,
            client_factory=lambda _value: DeepSeekLLMClient(settings=llm_settings),
            synthesis_repository=SynthesisRepository(settings),
            mode=options.mode, top_n=options.top_n,
            generated_at=generated_at, no_llm=options.no_llm,
            provenance={
                "market_snapshot_version": market.schema_version,
                "sector_snapshot_version": None,
                "anomaly_version": anomalies[0].schema_version if anomalies else None,
                "anomaly_config_version": None,
                "triage_version": None,
                "fund_flow_version": flows[0].schema_version if flows else None,
                "discovery_version": records[0].schema_version if records else None,
                "evidence_calibration_version": None,
                "attribution_version": None,
                "synthesis_prompt_version": (
                    synthesis_inputs[0].prompt_version if synthesis_inputs else None
                ),
                "synthesis_schema_version": (
                    synthesis_inputs[0].schema_version if synthesis_inputs else None
                ),
                "quantos_git_commit": git_commit(settings.project_root),
            },
        )
        json_path, markdown_path = DailyReportRepository(settings).write(report)
    except CommandFailure:
        raise
    except KnowledgePreparationError as error:
        raise CommandFailure(
            error.safe_reason_code,
            "Repair or rebuild the configured local Knowledge artifacts.",
            3,
        ) from None
    except (FileNotFoundError, StorageError, TypeError, ValueError):
        raise CommandFailure(
            "REPORT_ARTIFACTS_NOT_READY",
            "Verify local canonical artifacts, PIT cutoffs, and report configuration.",
            3,
        ) from None
    knowledge_status = (
        "READY"
        if report.candidate_briefs
        and all(item.synthesis.supplied_context_id for item in report.candidate_briefs)
        else "NOT_CONFIGURED"
    )
    return {
        "status": "PASS", "command": "report daily",
        "trade_date": report.trade_date.isoformat(), "mode": report.mode,
        "report_id": report.report_id,
        "json_path": str(json_path), "markdown_path": str(markdown_path),
        "total_candidates": report.candidate_summary["total_candidates"],
        "selected_candidates": report.candidate_summary["selected_for_report"],
        "llm_requests": report.synthesis_runtime["actual_llm_requests"],
        "knowledge_status": knowledge_status,
        "provider_network_requests": 0,
        "synthetic_fallback_count": 0,
    }


def prepare_synthesis_inputs(
    bundles, attribution_facts, *, company_names, mode, market_event_time,
    research_corpus_cutoff, generated_at, knowledge_settings,
    settings=DEFAULT_SETTINGS, knowledge_repository=None, index_repository=None,
    context_repository=None, preparer=prepare_candidate_synthesis_input,
):
    """Prepare inputs through the existing Knowledge integration entry point."""
    if knowledge_settings.enabled:
        knowledge_repository = knowledge_repository or KnowledgeRepository(settings=settings)
        index_repository = index_repository or KnowledgeLexicalIndexRepository(settings=settings)
        context_repository = context_repository or KnowledgeContextRepository(settings=settings)
    return [
        preparer(
            bundle, attribution_facts, mode=mode,
            company_name=company_names.get(bundle.candidate.symbol, bundle.candidate.symbol),
            market_event_time=market_event_time,
            research_corpus_cutoff=research_corpus_cutoff,
            generated_at=generated_at, settings=knowledge_settings,
            knowledge_repository=knowledge_repository,
            index_repository=index_repository,
            context_repository=context_repository,
        ).synthesis_input
        for bundle in bundles
    ]


def market_artifacts(settings: Settings, trade_date: date, as_of_time: datetime):
    market_repository = MarketDataRepository(settings)
    history = defaultdict(list)
    for day_dir in sorted(settings.normalized_market_dir.glob("trade_date=*")):
        day = date.fromisoformat(day_dir.name.split("=", 1)[1])
        if day <= trade_date:
            for bar in market_repository.read_by_date(day, as_of_time=as_of_time):
                history[bar.symbol].append(bar)
    anomalies, current_bars = [], {}
    for symbol, bars in history.items():
        bars.sort(key=lambda item: item.timestamp)
        current = next((item for item in bars if item.timestamp.date() == trade_date), None)
        if current is not None:
            current_bars[symbol] = current
            anomalies.append(detect_stock_anomaly(
                current, [item for item in bars if item.timestamp < current.timestamp],
                as_of_time=as_of_time,
            ))
    candidates = rank_anomaly_candidates(
        anomalies, (), (), as_of_time=as_of_time, top_n=None,
    )
    money = MoneyFlowRepository(settings).read_by_date(trade_date, as_of_time=as_of_time)
    money_by_symbol = {item.symbol: item for item in money}
    flows = [
        build_fund_flow_evidence(
            money_by_symbol[item.symbol], current_bars[item.symbol],
            as_of_time=as_of_time,
        )
        for item in candidates if item.symbol in money_by_symbol
    ]
    market = build_market_snapshot(
        tuple(current_bars.values()), (),
        security_member_symbols=tuple(current_bars),
        trade_date=trade_date, as_of_time=as_of_time,
    )
    return market, anomalies, candidates, flows, current_bars


def stored_company_names(settings: Settings, trade_date: date) -> dict[str, str]:
    path = settings.project_root / "reports" / f"evidence_quality_calibration_{trade_date}.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return {item["symbol"]: item["company_name"] for item in value.get("top20", ())}


def execute_time_slice(
    options: TimeSliceOptions,
    *, settings: Settings = DEFAULT_SETTINGS,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Execute the existing run/TimeSlice chain without reimplementing it."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    universe = "shse_szse_a_share"
    try:
        if options.plan_only or options.run_type is RunType.INTRADAY:
            observations = load_local_artifacts(
                settings, target_trade_date=options.trade_date,
                as_of_time=options.as_of_time, mode=options.mode,
                top_n=options.top_n, universe_name=universe,
            )
            knowledge_state = None
        else:
            observations, knowledge_state = load_local_operational_artifacts(
                settings, target_trade_date=options.trade_date,
                as_of_time=options.as_of_time, run_type=options.run_type,
                mode=options.mode, top_n=options.top_n, universe_name=universe,
            )
        readiness = collect_readiness(
            target_trade_date=options.trade_date,
            as_of_time=options.as_of_time,
            run_type=options.run_type, mode=options.mode,
            artifacts=observations, knowledge_state=knowledge_state,
        )
        context = RunContext(
            options.trade_date, readiness.market_basis_trade_date,
            options.as_of_time, options.run_type, options.mode, universe,
            options.top_n, not options.no_llm, clock(),
        )
        manifest = execute_run(
            context, readiness,
            handlers=local_artifact_handlers(readiness),
            plan_only=options.plan_only,
        )
        manifest_path = RunRepository(settings).write(manifest)
        result: dict[str, object] = {
            "status": "PASS" if options.plan_only or manifest.is_success else "FAIL",
            "command": f"report {options.run_type.value.lower().replace('_', '-')}",
            "run_id": context.run_id,
            "manifest_path": str(manifest_path),
            "REAL_DEEPSEEK_REQUEST_COUNT": 0,
            "PROVIDER_NETWORK_REQUEST_COUNT": 0,
            "plan_only": options.plan_only,
            "knowledge_status": knowledge_state.status.value if knowledge_state else None,
            "knowledge_reason_code": knowledge_state.reason_code if knowledge_state else None,
            "market_basis_trade_date": (
                str(context.market_basis_trade_date)
                if context.market_basis_trade_date else None
            ),
            "synthetic_fallback_count": 0,
        }
        if not options.plan_only and manifest.is_success:
            desired = (
                M.DAILY_REPORT if options.run_type is RunType.POST_CLOSE
                else M.ANOMALY_TRIAGE
            )
            refs = [
                ref for item in manifest.execution_results
                if item.module == desired and item.status == E.PASS
                for ref in item.artifact_refs if ref.path.endswith(".json")
            ]
            ref = refs[-1] if refs else None
            daily = load_daily_reference(ref) if ref else None
            records = []
            if daily is not None and options.run_type is not RunType.POST_CLOSE:
                repository = NewsEvidenceRepository(settings)
                pit_mode = "strict_live" if options.mode == "strict_live" else None
                records.extend(repository.query_web_search_results(
                    as_of_time=options.as_of_time, pit_mode=pit_mode,
                ))
                records.extend(repository.query_announcements(
                    start_time=datetime.min.replace(tzinfo=timezone.utc),
                    end_time=options.as_of_time,
                    as_of_time=options.as_of_time,
                    pit_mode=pit_mode,
                ))
            report = assemble_time_slice(
                manifest,
                manifest_ref=ArtifactReference(
                    context.run_id, str(manifest_path), manifest.schema_version,
                ),
                daily_report=daily, daily_report_ref=ref, records=records,
                generated_at=clock(),
            )
            json_path, md_path = TimeSliceRepository(settings).write(report)
            result.update({
                "report_id": report.report_id,
                "report_type": report.report_type.value,
                "json_path": str(json_path), "markdown_path": str(md_path),
                "daily_report_id": daily.report_id if daily is not None else None,
                "daily_report_ref": ref.path if ref is not None else None,
                "availability_summary": report.payload.availability_summary,
                "data_quality": report.data_quality,
            })
        if result["status"] == "FAIL":
            reason_code = next((
                item.safe_error_code or item.reason_code
                for item in manifest.execution_results
                if item.status is E.FAIL
                and (item.safe_error_code or item.reason_code)
            ), None)
            if reason_code is None and knowledge_state is not None:
                reason_code = knowledge_state.reason_code
            result["error_code"] = reason_code or "REPORT_NOT_READY"
            result["remediation"] = (
                "Prepare the required local artifacts for this product window."
            )
        return result
    except (FileNotFoundError, StorageError, TypeError, ValueError):
        raise CommandFailure(
            "REPORT_ARTIFACTS_NOT_READY",
            "Verify local artifacts, product window, and PIT cutoff.",
            3,
        ) from None


def render_daily(result: dict[str, object]) -> str:
    return "\n".join((
        "Daily Intelligence Complete",
        f"Trade Date        {result['trade_date']}",
        f"Mode              {result['mode']}",
        f"Candidates        {result['total_candidates']}",
        f"Selected          {result['selected_candidates']}",
        f"Report ID         {result['report_id']}",
        f"JSON              {result['json_path']}",
        f"Markdown          {result['markdown_path']}",
        f"LLM Requests      {result['llm_requests']}",
        f"Knowledge Status  {result['knowledge_status']}",
    ))


def render_time_slice(result: dict[str, object]) -> str:
    if result["status"] != "PASS":
        return "\n".join((
            f"TimeSlice Failed  {result['error_code']}",
            f"Remediation       {result['remediation']}",
        ))
    return "\n".join((
        "TimeSlice Complete",
        f"Type              {result.get('report_type', 'PLAN_ONLY')}",
        f"Run ID            {result['run_id']}",
        f"TimeSlice ID      {result.get('report_id', 'NOT_CREATED')}",
        f"Daily Report ID   {result.get('daily_report_id', 'NOT_APPLICABLE')}",
        f"Manifest          {result['manifest_path']}",
        f"JSON              {result.get('json_path', 'NOT_CREATED')}",
        f"Markdown          {result.get('markdown_path', 'NOT_CREATED')}",
    ))


def git_commit(project_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project_root,
        check=False, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None
