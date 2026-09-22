"""Audited, idempotent bootstrap over existing QuantOS provider adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from quantos.collectors import (
    ProviderAuthenticationError, ProviderConfigurationError, ProviderPayloadError,
    ProviderRateLimitError, ProviderResponseError, ProviderTransportError,
)
from quantos.config import DEFAULT_SETTINGS, MARKET_TIMEZONE, Settings
from quantos.normalizers import MarketNormalizationError, MarketNormalizer
from quantos.schemas import (
    BootstrapManifest, LocalTradingCalendarArtifact, ProviderFailureCode,
)
from quantos.schemas.scheduler_ops import LOCAL_CALENDAR_SCHEMA_VERSION
from quantos.scheduler_ops import calendar_artifact_record, load_local_calendar_artifact
from quantos.serialization import canonical_json_bytes
from quantos.storage import (
    BootstrapManifestRepository, MarketDataRepository, ProviderHealthRepository,
    SecurityMasterRepository, StorageError,
)


class DataBootstrapService:
    """Run bounded fetch/normalize/persist operations and publish an audit manifest."""

    def __init__(
        self, *, settings: Settings = DEFAULT_SETTINGS,
        providers: Mapping[str, object],
        clock: Callable[[], datetime] | None = None,
        security_repository: SecurityMasterRepository | None = None,
        market_repository: MarketDataRepository | None = None,
        manifest_repository: BootstrapManifestRepository | None = None,
        health_repository: ProviderHealthRepository | None = None,
    ) -> None:
        self.settings = settings
        self.providers = dict(providers)
        self._clock = clock or (lambda: datetime.now(MARKET_TIMEZONE))
        self.security_repository = security_repository or SecurityMasterRepository(settings)
        self._market_repository = market_repository
        self.manifests = manifest_repository or BootstrapManifestRepository(settings)
        self.health = health_repository or ProviderHealthRepository(settings)

    def bootstrap_security_master(self, provider_id: str) -> BootstrapManifest:
        return self._security_master(provider_id, operation="bootstrap")

    def refresh_security_master(self, provider_id: str) -> BootstrapManifest:
        return self._security_master(provider_id, operation="refresh")

    def _security_master(self, provider_id: str, *, operation: str) -> BootstrapManifest:
        started = self._now()
        received = 0
        try:
            provider = self._provider(provider_id)
            fetch = getattr(provider, "fetch_listed_securities", None)
            if fetch is None:
                fetch = getattr(provider, "fetch_security_master", None)
            if fetch is None:
                raise ProviderConfigurationError("provider lacks security master capability")
            records = tuple(fetch(as_of_time=started))
            received = len(records)
            if not records:
                return self._failure(
                    provider_id, "security_master", operation, started,
                    ProviderFailureCode.EMPTY_RESULT, received=0,
                )
            observed_at = self._now()
            if any(record.available_at > observed_at for record in records):
                raise ProviderPayloadError("future Security Master observation")
            written = self.security_repository.write_snapshot(
                records, provider_id=provider_id, observed_at=observed_at,
            )
            self.health.record_success(provider_id, observed_at=self._now())
            return self._finish(
                provider_id, "security_master", operation, started,
                records_received=received, records_valid=received,
                artifacts=(f"security_master:{written.snapshot.snapshot_id}",),
                reason_codes=() if written.created else ("UNCHANGED",),
            )
        except Exception as error:
            return self._failure(
                provider_id, "security_master", operation, started,
                _failure_code(error), received=received,
            )

    def bootstrap_market(
        self, provider_id: str, *, symbols: Sequence[str], trading_days: int,
        as_of_time: datetime,
    ) -> BootstrapManifest:
        started = self._now()
        received = 0
        if trading_days < 1:
            raise ValueError("trading_days must be positive")
        if not symbols:
            raise ValueError("at least one symbol is required")
        try:
            provider = self._provider(provider_id)
            start_date = as_of_time.date() - timedelta(days=max(14, trading_days * 3))
            dates = tuple(provider.fetch_trade_dates(
                start_date=start_date, end_date=as_of_time.date(), as_of_time=as_of_time,
            ))[-trading_days:]
            if not dates:
                return self._failure(
                    provider_id, "market_recent", "bootstrap", started,
                    ProviderFailureCode.EMPTY_RESULT, received=0,
                )
            if tuple(sorted(set(dates))) != dates or any(day > as_of_time.date() for day in dates):
                raise ProviderPayloadError("invalid trading calendar boundary")
            start_time = datetime.combine(dates[0], time.min, tzinfo=MARKET_TIMEZONE)
            end_time = datetime.combine(dates[-1], time.max, tzinfo=MARKET_TIMEZONE)
            raw = tuple(provider.fetch_bars(
                symbols=tuple(symbols), frequency="1d", start_time=start_time,
                end_time=end_time, as_of_time=as_of_time,
            ))
            received = len(raw)
            if not raw:
                return self._failure(
                    provider_id, "market_recent", "bootstrap", started,
                    ProviderFailureCode.EMPTY_RESULT, received=0,
                )
            bars = tuple(MarketNormalizer().normalize_many(list(raw)))
            if any(bar.available_at > as_of_time for bar in bars):
                raise MarketNormalizationError("future market fact is not PIT-visible")
            repository = self._market_repository or MarketDataRepository(self.settings)
            raw_new = repository.write_raw(raw)
            bars_new = repository.write_bars(bars)
            calendar = self._write_calendar(dates, generated_at=self._now())
            self.health.record_success(provider_id, observed_at=self._now())
            return self._finish(
                provider_id, "market_recent", "bootstrap", started,
                records_received=received, records_valid=len(bars),
                artifacts=(
                    "market:raw", "market:normalized",
                    f"calendar:{calendar.coverage_start}:{calendar.coverage_end}",
                ),
                reason_codes=("UNCHANGED",) if raw_new == bars_new == 0 else (),
            )
        except Exception as error:
            return self._failure(
                provider_id, "market_recent", "bootstrap", started,
                _failure_code(error), received=received,
            )

    def _write_calendar(
        self, dates: Sequence[date], *, generated_at: datetime,
    ) -> LocalTradingCalendarArtifact:
        path = self.settings.data_root / "reference" / "trading_calendar.json"
        combined = set(dates)
        if path.is_file():
            combined.update(load_local_calendar_artifact(path).trading_dates)
        ordered = tuple(sorted(combined))
        artifact = LocalTradingCalendarArtifact(
            LOCAL_CALENDAR_SCHEMA_VERSION, "CN_A_SHARE", "Asia/Shanghai",
            ordered[0], ordered[-1], ordered, generated_at,
        )
        _atomic_replace(path, canonical_json_bytes(calendar_artifact_record(artifact)) + b"\n")
        return artifact

    def _provider(self, provider_id: str) -> object:
        try:
            return self.providers[provider_id]
        except KeyError:
            raise ProviderConfigurationError("provider is not configured") from None

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("bootstrap clock must be timezone-aware")
        return value

    def _failure(
        self, provider_id: str, dataset: str, operation: str, started: datetime,
        code: ProviderFailureCode, *, received: int,
    ) -> BootstrapManifest:
        completed = self._now()
        effective_code = code
        try:
            self.health.record_failure(provider_id, observed_at=completed, code=code)
        except StorageError:
            effective_code = ProviderFailureCode.STORAGE_FAILED
        return self._finish(
            provider_id, dataset, operation, started,
            records_received=received, records_valid=0,
            records_rejected=received, status="FAIL",
            reason_codes=(effective_code.value,),
        )

    def _finish(
        self, provider_id: str, dataset: str, operation: str, started: datetime,
        *, records_received: int, records_valid: int,
        records_rejected: int = 0, artifacts: tuple[str, ...] = (),
        status: str = "PASS", reason_codes: tuple[str, ...] = (),
    ) -> BootstrapManifest:
        manifest = BootstrapManifest(
            uuid4().hex, operation, started, self._now(), (provider_id,), (dataset,),
            records_received, records_valid, records_rejected, artifacts, status,
            reason_codes,
        )
        self.manifests.write(manifest)
        return manifest


def _failure_code(error: Exception) -> ProviderFailureCode:
    if isinstance(error, ProviderConfigurationError):
        return ProviderFailureCode.UNCONFIGURED
    if isinstance(error, ProviderAuthenticationError):
        return ProviderFailureCode.AUTH_FAILED
    if isinstance(error, ProviderRateLimitError):
        return ProviderFailureCode.RATE_LIMITED
    if isinstance(error, ProviderTransportError):
        return ProviderFailureCode.NETWORK_FAILED
    if isinstance(error, ProviderPayloadError):
        return ProviderFailureCode.INVALID_SCHEMA
    if isinstance(error, ProviderResponseError):
        return ProviderFailureCode.REMOTE_ERROR
    if isinstance(error, MarketNormalizationError):
        return ProviderFailureCode.NORMALIZATION_FAILED
    if isinstance(error, StorageError):
        return ProviderFailureCode.STORAGE_FAILED
    return ProviderFailureCode.REMOTE_ERROR


def _atomic_replace(path: Path, payload: bytes) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}-", suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        raise StorageError("failed to persist trading calendar") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
