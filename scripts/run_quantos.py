"""QuantOS V1 run control plane over local PIT-aware canonical artifacts."""

import argparse
from datetime import date, datetime, timezone
import json
import subprocess

from quantos.config import DEFAULT_SETTINGS, SynthesisBatchSettings
from quantos.orchestration import (
    collect_readiness, execute_run, load_local_artifacts,
    load_local_operational_artifacts, local_artifact_handlers,
)
from quantos.schemas.run import RunContext, RunType
from quantos.storage.run import RunRepository, manifest_record


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
    if args.as_of_time.tzinfo is None or args.as_of_time.utcoffset() is None:
        parser.error("--as-of-time must be timezone-aware")
    if args.top_n < 1:
        parser.error("--top-n must be positive")
    run_type = RunType(args.run_type)
    universe = "shse_szse_a_share"
    # V1 Knowledge operational observation is intentionally unsupported intraday.
    if args.plan_only or run_type is RunType.INTRADAY:
        artifacts = load_local_artifacts(
            DEFAULT_SETTINGS, target_trade_date=args.trade_date,
            as_of_time=args.as_of_time, mode=args.mode,
            top_n=args.top_n, universe_name=universe,
        )
        knowledge_state = None
    else:
        artifacts, knowledge_state = load_local_operational_artifacts(
            DEFAULT_SETTINGS,
            target_trade_date=args.trade_date,
            as_of_time=args.as_of_time,
            run_type=run_type,
            mode=args.mode,
            top_n=args.top_n,
            universe_name=universe,
        )
    readiness = collect_readiness(target_trade_date=args.trade_date, as_of_time=args.as_of_time,
                                  run_type=run_type, mode=args.mode, artifacts=artifacts,
                                  knowledge_state=knowledge_state)
    context = RunContext(args.trade_date, readiness.market_basis_trade_date, args.as_of_time,
                         run_type, args.mode, universe, args.top_n, not args.no_llm,
                         datetime.now(timezone.utc))
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=DEFAULT_SETTINGS.project_root,
                         capture_output=True, text=True, check=False)
    manifest = execute_run(context, readiness, handlers=local_artifact_handlers(readiness),
                           plan_only=args.plan_only, git_commit=git.stdout.strip() if git.returncode == 0 else None)
    path = RunRepository(DEFAULT_SETTINGS).write(manifest)
    record = manifest_record(manifest)
    print(json.dumps({"manifest_path": str(path), "run_id": context.run_id,
                      "market_basis_trade_date": str(context.market_basis_trade_date) if context.market_basis_trade_date else None,
                      "CURRENT_INTRADAY_MARKET_DATA": readiness.current_intraday_market_data,
                      "knowledge_status": (
                          knowledge_state.status.value if knowledge_state else None
                      ),
                      "knowledge_reason_code": (
                          knowledge_state.reason_code if knowledge_state else None
                      ),
                      "knowledge_product_ref": (
                          record["knowledge_state"]["product_ref"]
                          if knowledge_state is not None
                          and knowledge_state.product_ref is not None else None
                      ),
                      "plan": record["plan"], "summary": record["summary"],
                      "execution_results": record["execution_results"],
                      "provider_network_requests": 0}, ensure_ascii=False, sort_keys=True))
    return 1 if not manifest.is_success else 0


if __name__ == "__main__":
    raise SystemExit(main())
