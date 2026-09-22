"""Static registry for provider capabilities; credentials never leave the environment."""

from __future__ import annotations

from collections.abc import Mapping
import os

from quantos.config import DEFAULT_SETTINGS, Settings
from quantos.schemas import ProviderAvailability, ProviderContract
from quantos.storage import ProviderHealthRepository, StorageError


_PROVIDERS = (
    ("baostock", "reference", ("security_master",), ()),
    ("cninfo", "announcement", ("announcements",), ()),
    ("eastmoney", "market", ("market_bars",), ()),
    ("exa", "evidence", ("web_search",), ("EXA_API_KEY",)),
    ("gdelt", "news", ("news",), ()),
    ("tavily", "evidence", ("web_search",), ("TAVILY_API_KEY",)),
    (
        "tushare", "market_reference",
        ("market_bars", "moneyflow", "news", "security_master", "trading_calendar"),
        ("TUSHARE_TOKEN",),
    ),
)


class ProviderRegistry:
    """Inspect a closed provider catalog without exposing configuration values."""

    def __init__(
        self, *, settings: Settings = DEFAULT_SETTINGS,
        environ: Mapping[str, str] | None = None,
        health_repository: ProviderHealthRepository | None = None,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._health = health_repository or ProviderHealthRepository(settings)

    def inspect(self) -> tuple[ProviderContract, ...]:
        output = []
        for provider_id, provider_type, capabilities, requirements in _PROVIDERS:
            configured = all(bool(self._environ.get(name, "").strip()) for name in requirements)
            health_invalid = False
            try:
                health = self._health.maybe_load(provider_id)
            except StorageError:
                health = None
                health_invalid = True
            if requirements and not configured:
                availability = ProviderAvailability.UNCONFIGURED
                reason = "CREDENTIAL_NOT_CONFIGURED"
            elif health_invalid:
                availability = ProviderAvailability.UNAVAILABLE
                reason = "HEALTH_RECORD_INVALID"
            elif health is None:
                availability = ProviderAvailability.DEGRADED
                reason = "NOT_VALIDATED"
            else:
                availability = health.availability
                reason = health.last_failure_code.value if health.last_failure_code else None
            output.append(ProviderContract(
                provider_id=provider_id,
                provider_type=provider_type,
                capabilities=tuple(sorted(capabilities)),
                configuration_requirements=tuple(sorted(requirements)),
                credential_configured=configured,
                availability=availability,
                last_success=health.last_success_at if health else None,
                last_failure=health.last_failure_at if health else None,
                health_reason=reason,
            ))
        return tuple(output)

    def get(self, provider_id: str) -> ProviderContract:
        for item in self.inspect():
            if item.provider_id == provider_id:
                return item
        raise KeyError(f"unknown provider: {provider_id}")
