"""Deterministic Active A-share universe and sector membership builders."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from quantos.schemas import SecurityMaster, SectorMembership
from quantos.schemas._validation import require_aware

CSRC_CLASSIFICATION = "证监会行业分类"
BSE_SUPPORT = "partial"
_INDUSTRY_PATTERN = re.compile(r"^([A-Z]\d{2})(.+)$")
_SUPPORTED_EXCHANGE_SUFFIXES = {
    "SHSE": ".SH",
    "SZSE": ".SZ",
    "BSE": ".BJ",
}
_MEMBERSHIP_EXCHANGES = {"SHSE", "SZSE"}


@dataclass(frozen=True, slots=True)
class MissingIndustrySecurity:
    symbol: str
    company_name: str
    exchange: str


@dataclass(frozen=True, slots=True)
class ActiveAShareUniverse:
    securities: tuple[SecurityMaster, ...]
    total_securities: int
    active_securities: int
    active_a_shares_with_industry: int
    active_a_shares_without_industry: int
    shse_active_a_shares: int
    szse_active_a_shares: int
    bse_active_a_shares: int
    other_or_unsupported: tuple[SecurityMaster, ...]
    missing_industry_symbols: tuple[MissingIndustrySecurity, ...]
    bse_support: str = BSE_SUPPORT

    @property
    def active_a_shares(self) -> int:
        return len(self.securities)


@dataclass(frozen=True, slots=True)
class ParsedIndustry:
    sector_code: str | None
    sector_name: str
    raw_industry: str


@dataclass(frozen=True, slots=True)
class SectorSummary:
    sector_code: str
    sector_name: str
    classification: str
    member_count: int
    symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IndustryDataQualityAudit:
    universe: ActiveAShareUniverse
    memberships: tuple[SectorMembership, ...]
    sectors: tuple[SectorSummary, ...]
    malformed_industry_symbols: tuple[str, ...]

    @property
    def sector_membership_count(self) -> int:
        return len(self.memberships)

    @property
    def sector_count(self) -> int:
        return len(self.sectors)


def build_active_a_share_universe(
    securities: Sequence[SecurityMaster], *, as_of_time: datetime
) -> ActiveAShareUniverse:
    """Build a PIT-visible universe using explicit schema fields, never names."""

    require_aware(as_of_time, "as_of_time")
    visible = [security for security in securities if security.available_at <= as_of_time]
    current = _latest_by_symbol(visible)
    active = [security for security in current if _is_active_security(security, as_of_time)]

    active_a_shares: list[SecurityMaster] = []
    unsupported: list[SecurityMaster] = []
    for security in active:
        if security.asset_type != "stock":
            continue
        expected_suffix = _SUPPORTED_EXCHANGE_SUFFIXES.get(security.exchange)
        if expected_suffix is None or not security.symbol.endswith(expected_suffix):
            unsupported.append(security)
            continue
        active_a_shares.append(security)

    active_a_shares.sort(key=lambda security: security.symbol)
    unsupported.sort(key=lambda security: security.symbol)
    missing = tuple(
        MissingIndustrySecurity(
            symbol=security.symbol,
            company_name=security.company_name,
            exchange=security.exchange,
        )
        for security in active_a_shares
        if not security.industry
    )
    with_industry = sum(bool(security.industry) for security in active_a_shares)
    return ActiveAShareUniverse(
        securities=tuple(active_a_shares),
        total_securities=len(current),
        active_securities=len(active),
        active_a_shares_with_industry=with_industry,
        active_a_shares_without_industry=len(active_a_shares) - with_industry,
        shse_active_a_shares=sum(s.exchange == "SHSE" for s in active_a_shares),
        szse_active_a_shares=sum(s.exchange == "SZSE" for s in active_a_shares),
        bse_active_a_shares=sum(s.exchange == "BSE" for s in active_a_shares),
        other_or_unsupported=tuple(unsupported),
        missing_industry_symbols=missing,
    )


def parse_baostock_industry(raw_industry: str) -> ParsedIndustry:
    """Parse codes such as C15 without discarding malformed provider text."""

    match = _INDUSTRY_PATTERN.fullmatch(raw_industry)
    if match is None:
        return ParsedIndustry(None, raw_industry, raw_industry)
    sector_code, sector_name = match.groups()
    if not sector_name.strip():
        return ParsedIndustry(None, raw_industry, raw_industry)
    return ParsedIndustry(sector_code, sector_name, raw_industry)


def build_sector_memberships(
    securities: Sequence[SecurityMaster], *, as_of_time: datetime
) -> list[SectorMembership]:
    """Build memberships only for supported, parseable CSRC industry records."""

    universe = build_active_a_share_universe(securities, as_of_time=as_of_time)
    memberships: list[SectorMembership] = []
    for security in universe.securities:
        if security.exchange not in _MEMBERSHIP_EXCHANGES:
            continue
        if not security.industry or security.industry_classification != CSRC_CLASSIFICATION:
            continue
        parsed = parse_baostock_industry(security.industry)
        if parsed.sector_code is None:
            continue
        memberships.append(
            SectorMembership(
                symbol=security.symbol,
                company_name=security.company_name,
                exchange=security.exchange,
                sector_code=parsed.sector_code,
                sector_name=parsed.sector_name,
                classification=security.industry_classification,
                raw_industry=parsed.raw_industry,
                source=security.source,
                effective_from=security.effective_from,
                effective_to=security.effective_to,
                available_at=security.available_at,
            )
        )
    return sorted(memberships, key=lambda membership: membership.symbol)


def filter_memberships_as_of(
    memberships: Sequence[SectorMembership], *, as_of_time: datetime
) -> list[SectorMembership]:
    """Return only memberships observable and effective at the PIT cutoff."""

    require_aware(as_of_time, "as_of_time")
    as_of_date = as_of_time.date()
    return sorted(
        (
            membership
            for membership in memberships
            if membership.available_at <= as_of_time
            and membership.effective_from <= as_of_date
            and (
                membership.effective_to is None
                or membership.effective_to > as_of_date
            )
        ),
        key=lambda membership: membership.symbol,
    )


def audit_industry_data_quality(
    securities: Sequence[SecurityMaster], *, as_of_time: datetime
) -> IndustryDataQualityAudit:
    universe = build_active_a_share_universe(securities, as_of_time=as_of_time)
    memberships = tuple(build_sector_memberships(securities, as_of_time=as_of_time))
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for membership in memberships:
        groups[
            (
                membership.sector_code,
                membership.sector_name,
                membership.classification,
            )
        ].append(membership.symbol)
    sectors = tuple(
        SectorSummary(
            sector_code=key[0],
            sector_name=key[1],
            classification=key[2],
            member_count=len(symbols),
            symbols=tuple(sorted(symbols)),
        )
        for key, symbols in sorted(groups.items())
    )
    malformed = tuple(
        sorted(
            security.symbol
            for security in universe.securities
            if security.industry
            and security.industry_classification == CSRC_CLASSIFICATION
            and parse_baostock_industry(security.industry).sector_code is None
        )
    )
    return IndustryDataQualityAudit(
        universe=universe,
        memberships=memberships,
        sectors=sectors,
        malformed_industry_symbols=malformed,
    )


def _latest_by_symbol(securities: Sequence[SecurityMaster]) -> list[SecurityMaster]:
    latest: dict[str, SecurityMaster] = {}
    for security in securities:
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
    return sorted(latest.values(), key=lambda security: security.symbol)


def _is_active_security(security: SecurityMaster, as_of_time: datetime) -> bool:
    as_of_date = as_of_time.date()
    return (
        security.is_active
        and security.status == "listed"
        and security.effective_from <= as_of_date
        and (security.effective_to is None or security.effective_to > as_of_date)
    )
