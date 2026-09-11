from datetime import datetime
from zoneinfo import ZoneInfo

from quantos.collectors import (
    EastmoneyMarketCollector,
    MarketDataProvider,
    TushareMarketCollector,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


class EmptyTushareClient:
    def daily(self, **kwargs):
        return []


def test_eastmoney_satisfies_market_data_provider_contract() -> None:
    assert isinstance(EastmoneyMarketCollector(), MarketDataProvider)


def test_tushare_satisfies_market_data_provider_contract() -> None:
    provider = TushareMarketCollector(
        client=EmptyTushareClient(),
        environ={"TUSHARE_TOKEN": "unit-test-placeholder"},
        clock=lambda: datetime(2026, 8, 27, tzinfo=SHANGHAI),
    )
    assert isinstance(provider, MarketDataProvider)
