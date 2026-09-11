from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from quantos.schemas import SecurityMaster, SectorMembership
from quantos.sectors import (
    BSE_SUPPORT,
    CSRC_CLASSIFICATION,
    audit_industry_data_quality,
    build_active_a_share_universe,
    build_sector_memberships,
    filter_memberships_as_of,
    parse_baostock_industry,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 8, 31, 12, 0, tzinfo=SHANGHAI)


def security(**overrides: object) -> SecurityMaster:
    values: dict[str, object] = {
        "symbol": "600519.SH",
        "company_name": "贵州茅台",
        "exchange": "SHSE",
        "effective_from": date(2001, 8, 27),
        "available_at": datetime(2026, 8, 31, 10, 0, tzinfo=SHANGHAI),
        "is_active": True,
        "source": "baostock",
        "asset_type": "stock",
        "status": "listed",
        "industry": "C15酒、饮料和精制茶制造业",
        "industry_classification": CSRC_CLASSIFICATION,
    }
    values.update(overrides)
    return SecurityMaster(**values)


def test_active_a_share_universe_uses_explicit_schema_fields() -> None:
    universe = build_active_a_share_universe(
        [
            security(),
            security(symbol="000001.SZ", company_name="平安银行", exchange="SZSE"),
        ],
        as_of_time=AS_OF,
    )

    assert [item.symbol for item in universe.securities] == ["000001.SZ", "600519.SH"]
    assert universe.active_a_shares == 2
    assert universe.shse_active_a_shares == 1
    assert universe.szse_active_a_shares == 1


@pytest.mark.parametrize(
    "excluded",
    [
        security(is_active=False),
        security(status="delisted", is_active=False, effective_to=date(2025, 1, 1)),
        security(asset_type="etf"),
    ],
    ids=["inactive", "delisted", "non_stock"],
)
def test_ineligible_security_is_not_an_active_a_share(excluded: SecurityMaster) -> None:
    universe = build_active_a_share_universe([excluded], as_of_time=AS_OF)
    assert universe.active_a_shares == 0


def test_active_security_is_not_assumed_to_be_active_a_share() -> None:
    universe = build_active_a_share_universe(
        [security(asset_type="index")], as_of_time=AS_OF
    )
    assert universe.active_securities == 1
    assert universe.active_a_shares == 0


def test_bse_is_counted_but_membership_support_is_partial() -> None:
    bse = security(symbol="430047.BJ", company_name="诺思兰德", exchange="BSE")
    universe = build_active_a_share_universe([bse], as_of_time=AS_OF)

    assert universe.active_a_shares == 1
    assert universe.bse_active_a_shares == 1
    assert universe.bse_support == BSE_SUPPORT == "partial"
    assert build_sector_memberships([bse], as_of_time=AS_OF) == []


def test_unsupported_exchange_mapping_is_reported_not_silently_dropped() -> None:
    unsupported = security(exchange="OTHER")
    universe = build_active_a_share_universe([unsupported], as_of_time=AS_OF)

    assert universe.active_a_shares == 0
    assert universe.other_or_unsupported == (unsupported,)


@pytest.mark.parametrize(
    ("raw", "code", "name"),
    [
        ("C15酒、饮料和精制茶制造业", "C15", "酒、饮料和精制茶制造业"),
        ("J66货币金融服务", "J66", "货币金融服务"),
        ("C38电气机械和器材制造业", "C38", "电气机械和器材制造业"),
    ],
)
def test_known_baostock_industry_format_is_parsed(raw: str, code: str, name: str) -> None:
    parsed = parse_baostock_industry(raw)
    assert parsed.sector_code == code
    assert parsed.sector_name == name
    assert parsed.raw_industry == raw


def test_malformed_industry_is_safe_and_preserves_raw_value() -> None:
    parsed = parse_baostock_industry("白酒")
    assert parsed.sector_code is None
    assert parsed.sector_name == "白酒"
    assert parsed.raw_industry == "白酒"


def test_missing_industry_is_warning_data_not_pipeline_failure() -> None:
    missing = security(industry=None, industry_classification=None)
    universe = build_active_a_share_universe([missing], as_of_time=AS_OF)

    assert universe.active_a_shares == 1
    assert universe.active_a_shares_with_industry == 0
    assert universe.active_a_shares_without_industry == 1
    assert universe.missing_industry_symbols[0].symbol == "600519.SH"
    assert build_sector_memberships([missing], as_of_time=AS_OF) == []


def test_membership_preserves_raw_classification_and_time_semantics() -> None:
    source = security()
    membership = build_sector_memberships([source], as_of_time=AS_OF)[0]

    assert membership.raw_industry == source.industry
    assert membership.classification == CSRC_CLASSIFICATION
    assert membership.effective_from == source.effective_from
    assert membership.effective_to == source.effective_to
    assert membership.available_at == source.available_at


def test_non_csrc_provider_industry_does_not_become_sector_membership() -> None:
    source = security(industry="银行", industry_classification="Tushare stock_basic")
    assert build_sector_memberships([source], as_of_time=AS_OF) == []


def test_audit_counts_memberships_and_unique_sectors() -> None:
    records = [
        security(),
        security(
            symbol="000001.SZ",
            company_name="平安银行",
            exchange="SZSE",
            industry="J66货币金融服务",
        ),
        security(
            symbol="300750.SZ",
            company_name="宁德时代",
            exchange="SZSE",
            industry="C38电气机械和器材制造业",
        ),
        security(
            symbol="000002.SZ",
            company_name="测试饮料",
            exchange="SZSE",
        ),
    ]
    audit = audit_industry_data_quality(records, as_of_time=AS_OF)

    assert audit.sector_membership_count == 4
    assert audit.sector_count == 3
    c15 = next(item for item in audit.sectors if item.sector_code == "C15")
    assert c15.member_count == 2
    assert c15.symbols == ("000002.SZ", "600519.SH")


def test_malformed_industry_is_audited_without_membership() -> None:
    audit = audit_industry_data_quality(
        [security(industry="malformed")], as_of_time=AS_OF
    )
    assert audit.universe.active_a_shares_with_industry == 1
    assert audit.sector_membership_count == 0
    assert audit.malformed_industry_symbols == ("600519.SH",)


def test_future_security_is_excluded_by_point_in_time_cutoff() -> None:
    future = security(available_at=datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI))
    universe = build_active_a_share_universe([future], as_of_time=AS_OF)
    assert universe.total_securities == 0
    assert universe.active_a_shares == 0


def test_latest_visible_security_revision_wins_without_future_leakage() -> None:
    old = security(industry=None, industry_classification=None)
    future = replace(
        old,
        industry="C15酒、饮料和精制茶制造业",
        industry_classification=CSRC_CLASSIFICATION,
        available_at=datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
    )
    universe = build_active_a_share_universe([old, future], as_of_time=AS_OF)
    assert universe.active_a_shares_without_industry == 1


def test_membership_pit_filter_rejects_future_and_expired_records() -> None:
    visible = build_sector_memberships([security()], as_of_time=AS_OF)[0]
    future = replace(
        visible,
        symbol="000001.SZ",
        company_name="平安银行",
        exchange="SZSE",
        available_at=datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI),
    )
    expired = replace(
        visible,
        symbol="300750.SZ",
        company_name="宁德时代",
        exchange="SZSE",
        effective_to=date(2026, 8, 30),
    )

    assert filter_memberships_as_of(
        [visible, future, expired], as_of_time=AS_OF
    ) == [visible]


def test_sector_membership_requires_timezone_aware_availability() -> None:
    with pytest.raises(ValueError, match="available_at"):
        SectorMembership(
            symbol="600519.SH",
            company_name="贵州茅台",
            exchange="SHSE",
            sector_code="C15",
            sector_name="酒、饮料和精制茶制造业",
            classification=CSRC_CLASSIFICATION,
            raw_industry="C15酒、饮料和精制茶制造业",
            source="baostock",
            effective_from=date(2001, 8, 27),
            available_at=datetime(2026, 8, 31, 10, 0),
        )
