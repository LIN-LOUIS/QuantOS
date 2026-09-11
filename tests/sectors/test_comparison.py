from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from quantos.schemas import SecurityMaster
from quantos.sectors import compare_security_universes

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 8, 31, 12, 0, tzinfo=SHANGHAI)


def security(symbol: str, *, source: str, **overrides: object) -> SecurityMaster:
    exchange = "SHSE" if symbol.endswith(".SH") else "SZSE"
    values: dict[str, object] = {
        "symbol": symbol,
        "company_name": symbol,
        "exchange": exchange,
        "effective_from": date(2000, 1, 1),
        "available_at": datetime(2026, 8, 31, 10, 0, tzinfo=SHANGHAI),
        "is_active": True,
        "source": source,
        "asset_type": "stock",
        "status": "listed",
    }
    values.update(overrides)
    return SecurityMaster(**values)


def test_universe_cross_check_reports_common_and_missing_symbols() -> None:
    report = compare_security_universes(
        [
            security("600519.SH", source="baostock"),
            security("000001.SZ", source="baostock"),
        ],
        [
            security("600519.SH", source="tushare"),
            security("300750.SZ", source="tushare"),
        ],
        as_of_time=AS_OF,
    )

    assert report.baostock_active_a_shares == 2
    assert report.tushare_listed_a_shares == 2
    assert report.common_symbols == ("600519.SH",)
    assert report.only_baostock == ("000001.SZ",)
    assert report.only_tushare == ("300750.SZ",)


def test_provider_mismatches_are_report_only_and_do_not_raise() -> None:
    baostock = security("600519.SH", source="baostock", company_name="贵州茅台")
    tushare = security(
        "600519.SH",
        source="tushare",
        company_name="贵州茅台A",
        exchange="SZSE",
        effective_from=date(2001, 8, 27),
        status="delisted",
        is_active=False,
    )

    report = compare_security_universes([baostock], [tushare], as_of_time=AS_OF)

    assert report.name_mismatch == ("600519.SH",)
    assert report.exchange_mismatch == ("600519.SH",)
    assert report.status_mismatch == ("600519.SH",)
    assert report.list_date_mismatch == ("600519.SH",)


def test_cross_check_excludes_future_provider_observations() -> None:
    future = security(
        "600519.SH",
        source="tushare",
        available_at=datetime(2026, 9, 1, 13, 0, tzinfo=SHANGHAI),
    )
    report = compare_security_universes([], [future], as_of_time=AS_OF)
    assert report.tushare_listed_a_shares == 0
    assert report.only_tushare == ()
