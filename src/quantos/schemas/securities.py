"""Provider-neutral A-share security master records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from ._validation import require_aware, require_non_empty

_SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


@dataclass(frozen=True, slots=True)
class SecurityMaster:
    symbol: str
    company_name: str
    exchange: str
    effective_from: date
    available_at: datetime
    aliases: tuple[str, ...] = ()
    industry_l1: str | None = None
    industry_l2: str | None = None
    effective_to: date | None = None
    is_active: bool = True
    source: str = "manual"
    asset_type: str = "stock"
    status: str = "listed"
    industry: str | None = None
    industry_classification: str | None = None
    source_updated_at: datetime | None = None
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        if not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol must use the canonical 000001.SZ format")
        require_non_empty(self.company_name, "company_name")
        require_non_empty(self.exchange, "exchange")
        require_non_empty(self.source, "source")
        require_non_empty(self.asset_type, "asset_type")
        require_non_empty(self.status, "status")
        require_aware(self.available_at, "available_at")
        if self.source_updated_at is not None:
            require_aware(self.source_updated_at, "source_updated_at")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to cannot precede effective_from")

    @property
    def name(self) -> str:
        return self.company_name

    @property
    def list_date(self) -> date:
        return self.effective_from

    @property
    def delist_date(self) -> date | None:
        return self.effective_to
