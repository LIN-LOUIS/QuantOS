"""Provider boundary types for raw market collection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from quantos.schemas._validation import require_aware, require_non_empty


class MarketDataError(RuntimeError):
    """Base class for explicit market provider failures."""


class ProviderTransportError(MarketDataError):
    """The provider could not be reached or returned a non-success HTTP status."""


class ProviderResponseError(MarketDataError):
    """The provider response was not valid JSON or reported an error."""


class ProviderPayloadError(MarketDataError):
    """The provider payload did not match the adapter's expected schema."""


class ProviderConfigurationError(MarketDataError):
    """Required provider configuration is missing or unusable."""


@dataclass(frozen=True, slots=True)
class RawMarketRecord:
    """Immutable provider payload retained before normalization."""

    provider: str
    symbol: str
    provider_record_id: str
    received_at: datetime
    fields: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in ("provider", "symbol", "provider_record_id"):
            require_non_empty(getattr(self, name), name)
        require_aware(self.received_at, "received_at")


@runtime_checkable
class MarketDataProvider(Protocol):
    """Minimal synchronous market-data contract used by the pipeline."""

    def fetch_bars(
        self,
        *,
        symbols: Sequence[str],
        frequency: str,
        start_time: datetime,
        end_time: datetime,
        as_of_time: datetime,
    ) -> list[RawMarketRecord]: ...


# Backwards-compatible name retained for existing Phase 1A imports.
MarketDataCollector = MarketDataProvider
