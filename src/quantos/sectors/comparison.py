"""Deterministic SecurityMaster universe comparison across providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from quantos.schemas import SecurityMaster
from quantos.schemas._validation import require_aware

from .membership import build_active_a_share_universe


@dataclass(frozen=True, slots=True)
class UniverseCrossCheck:
    baostock_active_a_shares: int
    tushare_listed_a_shares: int
    common_symbols: tuple[str, ...]
    only_baostock: tuple[str, ...]
    only_tushare: tuple[str, ...]
    name_mismatch: tuple[str, ...]
    exchange_mismatch: tuple[str, ...]
    status_mismatch: tuple[str, ...]
    list_date_mismatch: tuple[str, ...]


def compare_security_universes(
    baostock_securities: Sequence[SecurityMaster],
    tushare_securities: Sequence[SecurityMaster],
    *,
    as_of_time: datetime,
) -> UniverseCrossCheck:
    """Report provider differences without treating them as system failure."""

    require_aware(as_of_time, "as_of_time")
    baostock_universe = build_active_a_share_universe(
        baostock_securities, as_of_time=as_of_time
    )
    tushare_universe = build_active_a_share_universe(
        tushare_securities, as_of_time=as_of_time
    )
    baostock_active = {record.symbol: record for record in baostock_universe.securities}
    tushare_active = {record.symbol: record for record in tushare_universe.securities}
    common_active = set(baostock_active).intersection(tushare_active)

    baostock_visible = _latest_visible_by_symbol(baostock_securities, as_of_time)
    tushare_visible = _latest_visible_by_symbol(tushare_securities, as_of_time)
    comparable = set(baostock_visible).intersection(tushare_visible)
    return UniverseCrossCheck(
        baostock_active_a_shares=len(baostock_active),
        tushare_listed_a_shares=len(tushare_active),
        common_symbols=tuple(sorted(common_active)),
        only_baostock=tuple(sorted(set(baostock_active).difference(tushare_active))),
        only_tushare=tuple(sorted(set(tushare_active).difference(baostock_active))),
        name_mismatch=tuple(
            sorted(
                symbol
                for symbol in comparable
                if baostock_visible[symbol].company_name
                != tushare_visible[symbol].company_name
            )
        ),
        exchange_mismatch=tuple(
            sorted(
                symbol
                for symbol in comparable
                if baostock_visible[symbol].exchange
                != tushare_visible[symbol].exchange
            )
        ),
        status_mismatch=tuple(
            sorted(
                symbol
                for symbol in comparable
                if (
                    baostock_visible[symbol].status,
                    baostock_visible[symbol].is_active,
                )
                != (
                    tushare_visible[symbol].status,
                    tushare_visible[symbol].is_active,
                )
            )
        ),
        list_date_mismatch=tuple(
            sorted(
                symbol
                for symbol in comparable
                if baostock_visible[symbol].effective_from
                != tushare_visible[symbol].effective_from
            )
        ),
    )


def _latest_visible_by_symbol(
    securities: Sequence[SecurityMaster], as_of_time: datetime
) -> dict[str, SecurityMaster]:
    latest: dict[str, SecurityMaster] = {}
    for security in securities:
        if security.available_at > as_of_time:
            continue
        incumbent = latest.get(security.symbol)
        if incumbent is None or (
            security.available_at,
            security.effective_from,
            security.source,
        ) > (
            incumbent.available_at,
            incumbent.effective_from,
            incumbent.source,
        ):
            latest[security.symbol] = security
    return latest
