"""CLI adapter for explicit import and offline historical replay."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from quantos.collectors import MarketDataError, TushareMarketCollector
from quantos.config import MARKET_TIMEZONE, Settings
from quantos.replay import HistoricalIdentityAuthority, ReplayCampaignService, ReplayMode
from quantos.replay.identity import HistoricalIdentityArtifactRepository
from quantos.replay.importer import HistoricalMarketImporter
from quantos.replay.storage import ReplayCampaignRepository
from quantos.storage import SecurityMasterRepository


_REPLAY_MODES = {
    "strict-operational": ReplayMode.STRICT_OPERATIONAL_PIT,
    "retrospective-reconstructed": ReplayMode.RETROSPECTIVE_RECONSTRUCTED,
}


class _FailedProvider:
    def __init__(self, error: MarketDataError) -> None:
        self.error = error

    def fetch_trade_dates(self, **_kwargs):
        raise self.error


def execute(args) -> dict[str, object]:
    settings = Settings.from_project_root(Path(args.project_root))
    if args.replay_command == "derive-identity":
        source = SecurityMasterRepository(settings).snapshot_by_id(args.snapshot_id)
        value = HistoricalIdentityAuthority().derive(
            source, generated_at=datetime.now(MARKET_TIMEZONE),
        )
        HistoricalIdentityArtifactRepository(settings).save(value)
        return value.to_dict()
    if args.replay_command == "import-market":
        provider = _provider(args.provider)
        as_of = (datetime.fromisoformat(args.as_of_time) if args.as_of_time
                 else datetime.now(MARKET_TIMEZONE))
        value = HistoricalMarketImporter(
            settings=settings, providers={args.provider: provider},
        ).import_range(
            provider_id=args.provider, symbols=tuple(sorted(args.symbol)),
            start_date=date.fromisoformat(args.start),
            end_date=date.fromisoformat(args.end), as_of_time=as_of,
        )
        return value.to_dict()
    if args.replay_command == "run":
        value = ReplayCampaignService(settings=settings).run(
            dataset_id=args.dataset_id, symbols=tuple(sorted(args.symbol)),
            start_date=date.fromisoformat(args.start),
            end_date=date.fromisoformat(args.end),
            verify_determinism=not args.no_determinism_check,
            replay_mode=_REPLAY_MODES[args.mode],
            identity_authority_id=args.identity_authority_id,
        )
        return value.to_dict()
    repository = ReplayCampaignRepository(settings)
    if args.replay_command == "show":
        return repository.load(args.campaign_id).to_dict()
    failures = repository.load_failures(args.campaign_id)
    return {"campaign_id": args.campaign_id,
            "failures": [item.to_dict() for item in failures]}


def _provider(provider_id: str):
    try:
        if provider_id == "tushare":
            return TushareMarketCollector()
        raise ValueError("UNSUPPORTED_PROVIDER")
    except MarketDataError as error:
        return _FailedProvider(error)


def render(value: dict[str, object]) -> str:
    if "summary" not in value:
        identity = (value.get("campaign_id") or value.get("dataset_id")
                    or value.get("artifact_id"))
        return f"QuantOS Replay Artifact\nID                 {identity}"
    summary = value["summary"]
    return "\n".join((
        "QuantOS Replay Campaign",
        f"Campaign ID        {value['campaign_id']}",
        f"Replay mode        {value['replay_mode']}",
        f"Trading days       {len(value['universe']['trading_days'])}",
        f"PASS               {summary['pass_points']}",
        f"PARTIAL            {summary['partial_points']}",
        f"FAILED             {summary['failed_points']}",
        f"Identity coverage  {summary['identity_coverage']:.1%}",
        f"Market coverage    {summary['market_coverage']:.1%}",
        f"Evidence coverage  {summary['evidence_coverage']:.1%}",
        f"Strict evidence    {summary['strict_evidence_coverage']:.1%}",
        f"Attribution        {summary['attribution_coverage']:.1%}",
        f"PIT rejections     {summary['pit_rejection_count']}",
        f"Determinism mismatch {summary['deterministic_mismatch_count']}",
    ))
