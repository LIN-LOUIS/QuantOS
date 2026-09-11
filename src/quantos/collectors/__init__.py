"""External market-data adapters."""

from .base import (
    MarketDataCollector,
    MarketDataProvider,
    MarketDataError,
    ProviderConfigurationError,
    ProviderPayloadError,
    ProviderResponseError,
    ProviderTransportError,
    RawMarketRecord,
)
from .baostock import BaoStockSecurityMasterCollector
from .comparison import compare_market_bars
from .cninfo import CninfoAnnouncementProvider
from .eastmoney import EastmoneyMarketCollector
from .gdelt import GdeltNewsProvider
from .news import AnnouncementProvider, NewsProvider, TushareNewsProvider
from .tushare import TushareMarketCollector, TushareMoneyFlowCollector
from .web_search import ExaWebSearchProvider, TavilyWebSearchProvider, WebSearchProvider

__all__ = [
    "BaoStockSecurityMasterCollector",
    "CninfoAnnouncementProvider",
    "EastmoneyMarketCollector",
    "GdeltNewsProvider",
    "AnnouncementProvider",
    "NewsProvider",
    "MarketDataCollector",
    "MarketDataProvider",
    "MarketDataError",
    "ProviderConfigurationError",
    "ProviderPayloadError",
    "ProviderResponseError",
    "ProviderTransportError",
    "RawMarketRecord",
    "TushareMarketCollector",
    "TushareMoneyFlowCollector",
    "TushareNewsProvider",
    "WebSearchProvider",
    "TavilyWebSearchProvider",
    "ExaWebSearchProvider",
    "compare_market_bars",
]
