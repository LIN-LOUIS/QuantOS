"""Provider health, bootstrap, refresh, and fresh-state data foundation."""

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from quantos.collectors import (
    ProviderAuthenticationError, ProviderPayloadError, ProviderRateLimitError,
    ProviderTransportError, RawMarketRecord,
)
from quantos.config import MARKET_TIMEZONE, Settings
from quantos.data import (
    DataBootstrapService, ProviderAvailability, ProviderFailureCode,
    ProviderRegistry,
)
from quantos.schemas import SecurityMaster
from quantos.storage import (
    BootstrapManifestRepository, MarketDataRepository, ProviderHealthRepository,
    SecurityMasterRepository, StorageError,
)


NOW = datetime(2026, 9, 19, 20, tzinfo=MARKET_TIMEZONE)
TRADE_DAY = date(2026, 9, 18)


def security(name="贵州茅台", *, available_at=NOW):
    return SecurityMaster(
        "600519.SH", name, "SHSE", date(2001, 8, 27), available_at,
        source="tushare",
    )


def raw_bar(**field_overrides):
    fields = {
        "timestamp": TRADE_DAY.isoformat(), "frequency": "1d",
        "open": "1250", "high": "1280", "low": "1240", "close": "1266.98",
        "prev_close": "1258", "volume": "1000", "amount": "1266980",
        "available_at": datetime(2026, 9, 18, 18, tzinfo=MARKET_TIMEZONE).isoformat(),
    }
    fields.update(field_overrides)
    return RawMarketRecord(
        "tushare", "600519.SH", "tushare:600519.SH:1d:2026-09-18", NOW, fields,
    )


class Provider:
    def __init__(self, *, securities=None, bars=None, error=None):
        self.securities = [security()] if securities is None else securities
        self.bars = [raw_bar()] if bars is None else bars
        self.error = error
        self.security_calls = 0
        self.calendar_calls = 0
        self.market_calls = 0

    def fetch_listed_securities(self, *, as_of_time):
        self.security_calls += 1
        if self.error:
            raise self.error
        return self.securities

    def fetch_trade_dates(self, *, start_date, end_date, as_of_time):
        self.calendar_calls += 1
        if self.error:
            raise self.error
        return [TRADE_DAY]

    def fetch_bars(self, **kwargs):
        self.market_calls += 1
        if self.error:
            raise self.error
        return self.bars


def service(tmp_path, provider, **overrides):
    settings = Settings.from_project_root(tmp_path)
    values = {"settings": settings, "providers": {"tushare": provider}, "clock": lambda: NOW}
    values.update(overrides)
    return DataBootstrapService(**values), settings


def test_security_bootstrap_success_manifest_and_idempotent_repeat(tmp_path):
    provider = Provider()
    bootstrap, settings = service(tmp_path, provider)

    first = bootstrap.bootstrap_security_master("tushare")
    repeated = bootstrap.bootstrap_security_master("tushare")

    assert first.status == repeated.status == "PASS"
    assert first.records_received == first.records_valid == 1
    assert first.records_rejected == 0
    assert first.datasets == ("security_master",)
    assert first.artifacts[0].startswith("security_master:")
    assert repeated.reason_codes == ("UNCHANGED",)
    assert provider.security_calls == 2
    assert len(SecurityMasterRepository(settings).list_snapshots()) == 1
    assert len(BootstrapManifestRepository(settings).list_manifests()) == 2


def test_security_bootstrap_uses_post_fetch_observation_boundary(tmp_path):
    collected_at = NOW + timedelta(seconds=1)
    moments = iter((
        NOW,
        NOW + timedelta(seconds=2),
        NOW + timedelta(seconds=3),
        NOW + timedelta(seconds=4),
    ))
    bootstrap, settings = service(
        tmp_path,
        Provider(securities=[security(available_at=collected_at)]),
        clock=lambda: next(moments),
    )

    result = bootstrap.bootstrap_security_master("tushare")

    assert result.status == "PASS"
    snapshot = SecurityMasterRepository(settings).list_snapshots()[0]
    assert snapshot.observed_at == NOW + timedelta(seconds=2)
    assert snapshot.records[0].security.available_at == collected_at


def test_refresh_persists_changed_observation_without_overwrite(tmp_path):
    provider = Provider()
    bootstrap, settings = service(tmp_path, provider)
    first = bootstrap.bootstrap_security_master("tushare")
    changed_at = NOW + timedelta(days=1)
    provider.securities = [security("贵州茅台新名", available_at=changed_at)]
    bootstrap._clock = lambda: changed_at

    refreshed = bootstrap.refresh_security_master("tushare")

    assert refreshed.status == "PASS"
    assert refreshed.operation == "refresh"
    assert refreshed.artifacts != first.artifacts
    repo = SecurityMasterRepository(settings)
    assert len(repo.list_snapshots()) == 2
    assert repo.resolve("贵州茅台", as_of_time=NOW)[0].company_name == "贵州茅台"
    assert repo.resolve("贵州茅台新名", as_of_time=changed_at)[0].company_name == "贵州茅台新名"


@pytest.mark.parametrize(("error", "code"), (
    (ProviderAuthenticationError("secret auth body"), ProviderFailureCode.AUTH_FAILED),
    (ProviderRateLimitError("secret rate body"), ProviderFailureCode.RATE_LIMITED),
    (ProviderTransportError("secret network body"), ProviderFailureCode.NETWORK_FAILED),
    (ProviderPayloadError("secret payload"), ProviderFailureCode.INVALID_SCHEMA),
))
def test_provider_failures_are_classified_without_secret_persistence(tmp_path, error, code):
    bootstrap, settings = service(tmp_path, Provider(error=error))

    result = bootstrap.bootstrap_security_master("tushare")

    assert result.status == "FAIL"
    assert result.reason_codes == (code.value,)
    persisted = str(BootstrapManifestRepository(settings).list_manifests()[0].to_dict())
    assert "secret" not in persisted
    health = ProviderHealthRepository(settings).load("tushare")
    assert health.last_failure_code == code
    assert "secret" not in str(health.to_dict())


def test_empty_security_result_is_controlled_and_does_not_initialize_dataset(tmp_path):
    bootstrap, settings = service(tmp_path, Provider(securities=[]))

    result = bootstrap.bootstrap_security_master("tushare")

    assert result.status == "FAIL"
    assert result.reason_codes == ("EMPTY_RESULT",)
    assert SecurityMasterRepository(settings).is_initialized() is False


def test_security_storage_failure_does_not_publish_ready_health(tmp_path):
    class BrokenRepository:
        def write_snapshot(self, *_args, **_kwargs):
            raise StorageError("disk secret")

    bootstrap, settings = service(
        tmp_path, Provider(), security_repository=BrokenRepository(),
    )

    result = bootstrap.bootstrap_security_master("tushare")

    assert result.status == "FAIL"
    assert result.reason_codes == ("STORAGE_FAILED",)
    assert ProviderHealthRepository(settings).load("tushare").availability is ProviderAvailability.UNAVAILABLE


def test_recent_market_bootstrap_persists_raw_normalized_calendar_and_is_idempotent(tmp_path):
    provider = Provider()
    bootstrap, settings = service(tmp_path, provider)

    first = bootstrap.bootstrap_market(
        "tushare", symbols=("600519.SH",), trading_days=1, as_of_time=NOW,
    )
    repeated = bootstrap.bootstrap_market(
        "tushare", symbols=("600519.SH",), trading_days=1, as_of_time=NOW,
    )

    assert first.status == repeated.status == "PASS"
    assert first.records_received == first.records_valid == 1
    assert first.artifacts == repeated.artifacts
    assert repeated.reason_codes == ("UNCHANGED",)
    repository = MarketDataRepository(settings)
    bars = repository.read_by_symbol("600519.SH", as_of_time=NOW)
    assert bars[0].close.__str__() == "1266.9800000000"
    assert list(settings.raw_market_dir.rglob("*.parquet"))
    assert list(settings.normalized_market_dir.rglob("*.parquet"))
    assert (settings.data_root / "reference" / "trading_calendar.json").is_file()


def test_market_normalization_failure_writes_failed_manifest_without_ready_dataset(tmp_path):
    bootstrap, settings = service(tmp_path, Provider(bars=[raw_bar(close="not-a-number")]))

    result = bootstrap.bootstrap_market(
        "tushare", symbols=("600519.SH",), trading_days=1, as_of_time=NOW,
    )

    assert result.status == "FAIL"
    assert result.reason_codes == ("NORMALIZATION_FAILED",)
    assert not list(settings.normalized_market_dir.rglob("*.parquet"))


def test_future_market_fact_is_rejected_before_persistence(tmp_path):
    future = (NOW + timedelta(hours=1)).isoformat()
    bootstrap, settings = service(tmp_path, Provider(bars=[raw_bar(available_at=future)]))

    result = bootstrap.bootstrap_market(
        "tushare", symbols=("600519.SH",), trading_days=1, as_of_time=NOW,
    )

    assert result.status == "FAIL"
    assert result.reason_codes == ("NORMALIZATION_FAILED",)
    assert not list(settings.raw_market_dir.rglob("*.parquet"))
    assert not list(settings.normalized_market_dir.rglob("*.parquet"))


def test_provider_outage_preserves_last_ready_security_dataset(tmp_path):
    provider = Provider()
    bootstrap, settings = service(tmp_path, provider)
    assert bootstrap.bootstrap_security_master("tushare").status == "PASS"
    provider.error = ProviderTransportError("temporary secret outage")

    failed = bootstrap.refresh_security_master("tushare")

    assert failed.status == "FAIL"
    assert len(SecurityMasterRepository(settings).list_snapshots()) == 1
    latest_ready = BootstrapManifestRepository(settings).latest_success("security_master")
    assert latest_ready is not None
    assert ProviderHealthRepository(settings).load("tushare").availability is ProviderAvailability.UNAVAILABLE


def test_provider_registry_uses_configuration_and_persisted_health_without_values(tmp_path):
    settings = Settings.from_project_root(tmp_path)
    registry = ProviderRegistry(settings=settings, environ={"TUSHARE_TOKEN": "configured-secret"})
    before = {item.provider_id: item for item in registry.inspect()}
    assert before["tushare"].availability is ProviderAvailability.DEGRADED
    assert before["tavily"].availability is ProviderAvailability.UNCONFIGURED
    ProviderHealthRepository(settings).record_success("tushare", observed_at=NOW)

    after = {item.provider_id: item for item in registry.inspect()}

    assert after["tushare"].availability is ProviderAvailability.READY
    assert after["tushare"].credential_configured is True
    assert "configured-secret" not in str(after["tushare"].to_dict())
    assert "akshare" not in after
