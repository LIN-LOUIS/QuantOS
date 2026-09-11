"""Offline time-sliced products from an executed QuantOS run and local records."""

import argparse
from datetime import date, datetime, time, timezone
import json

from quantos.config import DEFAULT_SETTINGS, MARKET_TIMEZONE, SynthesisBatchSettings
from quantos.orchestration import (
    collect_readiness, execute_run, load_local_artifacts,
    load_local_operational_artifacts, local_artifact_handlers,
)
from quantos.schemas.run import ArtifactReference, ModuleExecutionStatus as E, ModuleName as M, RunContext, RunType
from quantos.storage.news import NewsEvidenceRepository
from quantos.storage.run import RunRepository
from quantos.storage.time_slice import TimeSliceRepository
from quantos.time_slices import assemble_time_slice, load_daily_reference


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", type=date.fromisoformat, required=True)
    parser.add_argument("--as-of-time", type=datetime.fromisoformat, required=True)
    parser.add_argument("--run-type", choices=[x.value for x in RunType], required=True)
    parser.add_argument("--mode", choices=["research", "strict_live"], required=True)
    parser.add_argument("--top-n", type=int, default=SynthesisBatchSettings.from_env().synthesis_top_n)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args(argv)
    if args.as_of_time.tzinfo is None or args.as_of_time.utcoffset() is None or args.top_n < 1:
        parser.error("timezone-aware cutoff and positive top_n required")
    run_type = RunType(args.run_type)
    opened = datetime.combine(args.trade_date, time(9, 30), MARKET_TIMEZONE)
    if (run_type == RunType.PRE_OPEN and args.as_of_time > opened
        or run_type == RunType.INTRADAY and args.as_of_time < opened):
        parser.error("cutoff does not match product window")
    universe = "shse_szse_a_share"
    # Preserve the existing non-Knowledge intraday product path unchanged.
    if args.plan_only or run_type is RunType.INTRADAY:
        observations = load_local_artifacts(
            DEFAULT_SETTINGS, target_trade_date=args.trade_date,
            as_of_time=args.as_of_time, mode=args.mode,
            top_n=args.top_n, universe_name=universe,
        )
        knowledge_state = None
    else:
        observations, knowledge_state = load_local_operational_artifacts(
            DEFAULT_SETTINGS, target_trade_date=args.trade_date,
            as_of_time=args.as_of_time, run_type=run_type, mode=args.mode,
            top_n=args.top_n, universe_name=universe,
        )
    readiness = collect_readiness(target_trade_date=args.trade_date, as_of_time=args.as_of_time,
        run_type=run_type, mode=args.mode, artifacts=observations,
        knowledge_state=knowledge_state)
    context = RunContext(args.trade_date, readiness.market_basis_trade_date, args.as_of_time, run_type,
        args.mode, universe, args.top_n, not args.no_llm, datetime.now(timezone.utc))
    # This Phase 3C.2 CLI has no generation-capable adapter, even without --no-llm.
    manifest = execute_run(context, readiness, handlers=local_artifact_handlers(readiness), plan_only=args.plan_only)
    manifest_path = RunRepository(DEFAULT_SETTINGS).write(manifest)
    result = {"manifest_path": str(manifest_path), "REAL_DEEPSEEK_REQUEST_COUNT": 0,
              "PROVIDER_NETWORK_REQUEST_COUNT": 0, "plan_only": args.plan_only,
              "knowledge_status": knowledge_state.status.value if knowledge_state else None,
              "knowledge_reason_code": knowledge_state.reason_code if knowledge_state else None}
    if not args.plan_only and manifest.is_success:
        desired = M.DAILY_REPORT if run_type == RunType.POST_CLOSE else M.ANOMALY_TRIAGE
        refs = [ref for item in manifest.execution_results if item.module == desired and item.status == E.PASS
                for ref in item.artifact_refs if ref.path.endswith(".json")]
        ref = refs[-1] if refs else None
        daily = load_daily_reference(ref) if ref else None
        records = []
        if daily is not None and run_type != RunType.POST_CLOSE:
            repository = NewsEvidenceRepository(DEFAULT_SETTINGS)
            pit_mode = "strict_live" if args.mode == "strict_live" else None
            records.extend(repository.query_web_search_results(as_of_time=args.as_of_time, pit_mode=pit_mode))
            records.extend(repository.query_announcements(
                # A previously published announcement can first become known now.
                # Query local publication history; the view buckets by knowledge time.
                start_time=datetime.min.replace(tzinfo=timezone.utc),
                end_time=args.as_of_time, as_of_time=args.as_of_time, pit_mode=pit_mode))
        report = assemble_time_slice(manifest,
            manifest_ref=ArtifactReference(context.run_id, str(manifest_path), manifest.schema_version),
            daily_report=daily, daily_report_ref=ref, records=records, generated_at=datetime.now(timezone.utc))
        json_path, md_path = TimeSliceRepository(DEFAULT_SETTINGS).write(report)
        result.update({"report_id": report.report_id, "report_type": report.report_type.value,
            "json_path": str(json_path), "markdown_path": str(md_path),
            "market_basis_trade_date": str(context.market_basis_trade_date) if context.market_basis_trade_date else None,
            "availability_summary": report.payload.availability_summary, "data_quality": report.data_quality})
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if args.plan_only or manifest.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
