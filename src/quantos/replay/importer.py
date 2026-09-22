"""Explicit bounded historical market import; replay never calls this service."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time

from quantos.collectors import (
    MarketDataError, ProviderConfigurationError, ProviderPayloadError,
)
from quantos.config import DEFAULT_SETTINGS, MARKET_TIMEZONE, Settings
from quantos.normalizers import MarketNormalizationError, MarketNormalizer
from quantos.storage import MarketDataRepository, StorageError

from .contracts import HistoricalDatasetManifest
from .storage import HistoricalDatasetRepository, ReplayStorageError


class HistoricalImportError(RuntimeError):
    """Safe import failure code; provider messages are never retained."""


class HistoricalMarketImporter:
    def __init__(self, *, settings: Settings = DEFAULT_SETTINGS,
                 providers: Mapping[str, object],
                 market_repository: MarketDataRepository | None = None,
                 dataset_repository: HistoricalDatasetRepository | None = None) -> None:
        self.settings = settings
        self.providers = dict(providers)
        self.market = market_repository
        self.datasets = dataset_repository or HistoricalDatasetRepository(settings)

    def import_range(self, *, provider_id: str, symbols: tuple[str, ...],
                     start_date: date, end_date: date,
                     as_of_time: datetime) -> HistoricalDatasetManifest:
        if as_of_time.utcoffset() is None:
            raise ValueError("TIMEZONE_REQUIRED")
        if start_date > end_date or end_date > as_of_time.date():
            raise ValueError("invalid historical import range")
        if (not symbols or tuple(sorted(set(symbols))) != symbols
                or len(symbols) > 100):
            raise ValueError("historical import symbols must be bounded and sorted")
        provider = self.providers.get(provider_id)
        if provider is None:
            raise HistoricalImportError("UNCONFIGURED")
        try:
            trading_dates = tuple(provider.fetch_trade_dates(
                start_date=start_date, end_date=end_date, as_of_time=as_of_time,
            ))
            if (not trading_dates or tuple(sorted(set(trading_dates))) != trading_dates
                    or any(day < start_date or day > end_date for day in trading_dates)):
                raise ProviderPayloadError("invalid historical trading calendar")
            records = tuple(provider.fetch_bars(
                symbols=symbols, frequency="1d",
                start_time=datetime.combine(start_date, time.min, MARKET_TIMEZONE),
                end_time=datetime.combine(end_date, time.max, MARKET_TIMEZONE),
                as_of_time=as_of_time,
            ))
            if not records:
                raise HistoricalImportError("MARKET_DATA_UNAVAILABLE")
            bars = tuple(MarketNormalizer().normalize_many(list(records)))
            if any(
                item.symbol not in symbols
                or item.timestamp.date() not in trading_dates
                or item.timestamp > as_of_time
                or item.available_at > as_of_time
                for item in bars
            ):
                raise HistoricalImportError("PIT_REJECTED")
            repository = self.market or MarketDataRepository(self.settings)
            repository.write_raw(records)
            repository.write_bars(bars)
            manifest = HistoricalDatasetManifest.build(
                provider=provider_id, market="A_SHARE", symbols=symbols,
                start_date=start_date, end_date=end_date,
                trading_dates=trading_dates,
                record_refs=tuple(sorted(item.source_record_id for item in bars)),
                imported_at=as_of_time,
            )
            self.datasets.save(manifest)
            return manifest
        except HistoricalImportError:
            raise
        except (ProviderConfigurationError,):
            raise HistoricalImportError("UNCONFIGURED") from None
        except MarketDataError:
            raise HistoricalImportError("PROVIDER_FAILURE") from None
        except MarketNormalizationError:
            raise HistoricalImportError("INVALID_SCHEMA") from None
        except (StorageError, ReplayStorageError):
            raise HistoricalImportError("STORAGE_FAILED") from None
