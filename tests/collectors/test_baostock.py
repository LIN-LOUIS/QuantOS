from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from quantos.collectors import (
    BaoStockSecurityMasterCollector,
    ProviderResponseError,
    ProviderTransportError,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
BASIC_FIELDS = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
INDUSTRY_FIELDS = [
    "updateDate",
    "code",
    "code_name",
    "industry",
    "industryClassification",
]


class Status:
    def __init__(self, error_code: str = "0") -> None:
        self.error_code = error_code


class Result:
    def __init__(self, fields, rows, *, error_code: str = "0", next_error=None):
        self.fields = fields
        self.rows = rows
        self.error_code = error_code
        self.index = -1
        self.next_error = next_error

    def next(self):
        if self.next_error:
            raise self.next_error
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self):
        return self.rows[self.index]


class FakeBaoStock:
    def __init__(
        self,
        *,
        login_code="0",
        basic=None,
        industry=None,
        basic_error: Exception | None = None,
    ) -> None:
        self.login_code = login_code
        self.basic = basic or Result(
            BASIC_FIELDS,
            [["sh.600519", "贵州茅台", "2001-08-27", "", "1", "1"]],
        )
        self.industry = industry or Result(
            INDUSTRY_FIELDS,
            [["2024-01-03", "sh.600519", "贵州茅台", "白酒", "证监会行业分类"]],
        )
        self.basic_error = basic_error
        self.login_calls = 0
        self.logout_calls = 0

    def login(self):
        self.login_calls += 1
        return Status(self.login_code)

    def logout(self):
        self.logout_calls += 1
        return Status()

    def query_stock_basic(self):
        if self.basic_error:
            raise self.basic_error
        return self.basic

    def query_stock_industry(self):
        return self.industry


def collect(client: FakeBaoStock):
    return BaoStockSecurityMasterCollector(
        client=client,
        clock=lambda: datetime(2026, 8, 27, 10, 0, tzinfo=SHANGHAI),
    ).fetch_security_master(
        as_of_time=datetime(2026, 8, 27, 10, 0, tzinfo=SHANGHAI)
    )


def test_login_query_merge_mapping_and_logout_success() -> None:
    client = FakeBaoStock()
    record = collect(client)[0]

    assert client.login_calls == 1
    assert client.logout_calls == 1
    assert record.symbol == "600519.SH"
    assert record.name == "贵州茅台"
    assert record.exchange == "SHSE"
    assert record.asset_type == "stock"
    assert record.list_date == date(2001, 8, 27)
    assert record.delist_date is None
    assert record.status == "listed"
    assert record.is_active
    assert record.industry == "白酒"
    assert record.industry_classification == "证监会行业分类"
    assert record.industry_l1 is None
    assert record.source_updated_at == datetime(
        2024, 1, 3, 0, 0, tzinfo=SHANGHAI
    )


def test_login_failure_is_explicit_and_does_not_logout() -> None:
    client = FakeBaoStock(login_code="1001")
    with pytest.raises(ProviderResponseError, match="login"):
        collect(client)
    assert client.logout_calls == 0


def test_delisted_status_and_sz_symbol_mapping() -> None:
    basic = Result(
        BASIC_FIELDS,
        [["sz.000001", "平安银行", "1991-04-03", "2025-01-02", "1", "0"]],
    )
    industry = Result(INDUSTRY_FIELDS, [])
    record = collect(FakeBaoStock(basic=basic, industry=industry))[0]

    assert record.symbol == "000001.SZ"
    assert record.status == "delisted"
    assert not record.is_active
    assert record.delist_date == date(2025, 1, 2)


def test_missing_industry_is_safely_none() -> None:
    record = collect(FakeBaoStock(industry=Result(INDUSTRY_FIELDS, [])))[0]
    assert record.industry is None
    assert record.industry_classification is None
    assert record.source_updated_at is None


def test_raw_industry_classification_is_preserved_without_relabeling() -> None:
    industry = Result(
        INDUSTRY_FIELDS,
        [["2024-01-03", "sh.600519", "贵州茅台", "饮料", "Provider Raw Label"]],
    )
    record = collect(FakeBaoStock(industry=industry))[0]
    assert record.industry == "饮料"
    assert record.industry_classification == "Provider Raw Label"
    assert record.industry_l1 is None
    assert record.industry_l2 is None


def test_query_provider_error_is_explicit_and_logs_out() -> None:
    client = FakeBaoStock(basic=Result(BASIC_FIELDS, [], error_code="1002"))
    with pytest.raises(ProviderResponseError, match="query_stock_basic"):
        collect(client)
    assert client.logout_calls == 1


def test_logout_occurs_when_query_raises_exception() -> None:
    client = FakeBaoStock(basic_error=RuntimeError("network failed"))
    with pytest.raises(ProviderTransportError, match="query_stock_basic"):
        collect(client)
    assert client.logout_calls == 1


def test_logout_occurs_when_result_iteration_raises() -> None:
    result = Result(BASIC_FIELDS, [], next_error=RuntimeError("iteration failed"))
    client = FakeBaoStock(basic=result)
    with pytest.raises(ProviderResponseError, match="query_stock_basic"):
        collect(client)
    assert client.logout_calls == 1
