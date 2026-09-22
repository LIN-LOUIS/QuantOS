"""User command adapter for bounded data bootstrap and refresh."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from quantos.collectors import (
    BaoStockSecurityMasterCollector, MarketDataError, TushareMarketCollector,
)
from quantos.config import MARKET_TIMEZONE, Settings
from quantos.data import DataBootstrapService


class _FailedProvider:
    def __init__(self, error: MarketDataError) -> None:
        self._error = error

    def fetch_listed_securities(self, **_kwargs):
        raise self._error

    def fetch_security_master(self, **_kwargs):
        raise self._error

    def fetch_trade_dates(self, **_kwargs):
        raise self._error


def execute(args) -> dict[str, object]:
    settings = Settings.from_project_root(Path(args.project_root))
    provider = _provider(args.provider)
    service = DataBootstrapService(settings=settings, providers={args.provider: provider})
    if args.data_action == "refresh":
        return service.refresh_security_master(args.provider).to_dict()
    if args.dataset == "security-master":
        return service.bootstrap_security_master(args.provider).to_dict()
    as_of = (
        datetime.fromisoformat(args.as_of_time)
        if args.as_of_time else datetime.now(MARKET_TIMEZONE)
    )
    if as_of.utcoffset() is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return service.bootstrap_market(
        args.provider, symbols=tuple(args.symbol), trading_days=args.trading_days,
        as_of_time=as_of,
    ).to_dict()


def _provider(provider_id: str):
    try:
        if provider_id == "tushare":
            return TushareMarketCollector()
        if provider_id == "baostock":
            return BaoStockSecurityMasterCollector()
        raise ValueError("UNSUPPORTED_PROVIDER")
    except MarketDataError as error:
        return _FailedProvider(error)


def render(value: dict[str, object]) -> str:
    lines = [
        f"QuantOS Data {value['operation']}  {value['status']}",
        f"Provider          {', '.join(value['providers'])}",
        f"Dataset           {', '.join(value['datasets'])}",
        f"Records           {value['records_valid']}/{value['records_received']}",
        f"Bootstrap ID      {value['bootstrap_id']}",
    ]
    if value["reason_codes"]:
        lines.append(f"Reason             {', '.join(value['reason_codes'])}")
    return "\n".join(lines)
