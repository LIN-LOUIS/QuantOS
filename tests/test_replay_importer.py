"""Explicit bounded historical import remains separate from offline replay."""

from datetime import date, datetime

import pytest

from quantos.collectors import ProviderTransportError, RawMarketRecord
from quantos.config import MARKET_TIMEZONE, Settings
from quantos.replay.importer import HistoricalImportError, HistoricalMarketImporter
from quantos.replay.storage import HistoricalDatasetRepository
from quantos.storage import MarketDataRepository


NOW = datetime(2026, 8, 5, 20, tzinfo=MARKET_TIMEZONE)
DAY = date(2026, 8, 3)


class Provider:
    def __init__(self, *, error=None, available_at=None):
        self.error = error
        self.available_at = available_at or datetime(2026, 8, 3, 18, tzinfo=MARKET_TIMEZONE)
        self.calls = 0

    def fetch_trade_dates(self, **_kwargs):
        if self.error:
            raise self.error
        return [DAY]

    def fetch_bars(self, **_kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return [RawMarketRecord(
            "tushare", "600519.SH", "historical:1", NOW,
            {"timestamp": DAY.isoformat(), "frequency": "1d", "open": "100",
             "high": "103", "low": "99", "close": "102", "prev_close": "100",
             "volume": "10", "amount": "1020",
             "available_at": self.available_at.isoformat()},
        )]


def importer(tmp_path, provider):
    settings = Settings.from_project_root(tmp_path)
    return HistoricalMarketImporter(
        settings=settings, providers={"tushare": provider},
    ), settings


def test_historical_import_is_bounded_manifest_driven_and_idempotent(tmp_path):
    provider = Provider()
    service, settings = importer(tmp_path, provider)

    first = service.import_range(
        provider_id="tushare", symbols=("600519.SH",), start_date=DAY,
        end_date=DAY, as_of_time=NOW,
    )
    repeated = service.import_range(
        provider_id="tushare", symbols=("600519.SH",), start_date=DAY,
        end_date=DAY, as_of_time=NOW,
    )

    assert first.dataset_id == repeated.dataset_id
    assert provider.calls == 2
    assert len(HistoricalDatasetRepository(settings).list_visible()) == 1
    assert len(MarketDataRepository(settings).read_by_symbol(
        "600519.SH", as_of_time=NOW,
    )) == 1


def test_provider_failure_does_not_publish_visible_dataset(tmp_path):
    service, settings = importer(
        tmp_path, Provider(error=ProviderTransportError("secret provider body")),
    )

    with pytest.raises(HistoricalImportError, match="PROVIDER_FAILURE"):
        service.import_range(
            provider_id="tushare", symbols=("600519.SH",), start_date=DAY,
            end_date=DAY, as_of_time=NOW,
        )

    assert HistoricalDatasetRepository(settings).list_visible() == ()


def test_historical_import_rejects_future_market_availability(tmp_path):
    service, settings = importer(
        tmp_path, Provider(available_at=datetime(2026, 8, 6, 18,
                                                 tzinfo=MARKET_TIMEZONE)),
    )

    with pytest.raises(HistoricalImportError, match="PIT_REJECTED"):
        service.import_range(
            provider_id="tushare", symbols=("600519.SH",), start_date=DAY,
            end_date=DAY, as_of_time=NOW,
        )

    assert HistoricalDatasetRepository(settings).list_visible() == ()
