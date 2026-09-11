"""Centralized filesystem and runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
KNOWLEDGE_QUERY_STRATEGY_VERSION = "candidate_subject:v1"

_SHA256_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class Settings:
    """Phase 1A settings with explicit, injectable storage paths."""

    project_root: Path
    data_root: Path
    raw_market_dir: Path
    normalized_market_dir: Path
    sector_snapshot_dir: Path
    sector_membership_snapshot_dir: Path
    moneyflow_dir: Path
    news_dir: Path
    announcement_dir: Path
    web_search_dir: Path
    synthesis_dir: Path
    report_dir: Path
    duckdb_path: Path
    market_timezone: ZoneInfo = MARKET_TIMEZONE

    @classmethod
    def from_project_root(cls, project_root: Path | str) -> "Settings":
        root = Path(project_root).expanduser().resolve()
        data_root = root / "data"
        return cls(
            project_root=root,
            data_root=data_root,
            raw_market_dir=data_root / "raw" / "market",
            normalized_market_dir=data_root / "normalized" / "market",
            sector_snapshot_dir=data_root / "normalized" / "sector_snapshots",
            sector_membership_snapshot_dir=(
                data_root / "normalized" / "sector_memberships"
            ),
            moneyflow_dir=data_root / "normalized" / "moneyflow",
            news_dir=data_root / "normalized" / "news",
            announcement_dir=data_root / "normalized" / "announcements",
            web_search_dir=data_root / "normalized" / "web_search",
            synthesis_dir=data_root / "derived" / "synthesis",
            report_dir=data_root / "derived" / "reports",
            duckdb_path=data_root / "quantos.duckdb",
        )

    def ensure_directories(self) -> None:
        """Create only directories required by Phase 1A runtime data."""

        self.raw_market_dir.mkdir(parents=True, exist_ok=True)
        self.normalized_market_dir.mkdir(parents=True, exist_ok=True)
        self.sector_snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.sector_membership_snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.moneyflow_dir.mkdir(parents=True, exist_ok=True)
        self.news_dir.mkdir(parents=True, exist_ok=True)
        self.announcement_dir.mkdir(parents=True, exist_ok=True)
        self.web_search_dir.mkdir(parents=True, exist_ok=True)
        self.synthesis_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.duckdb_path.parent.mkdir(parents=True, exist_ok=True)


DEFAULT_SETTINGS = Settings.from_project_root(Path(__file__).parents[2])


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """Environment-only LLM selection; credentials are intentionally excluded."""

    provider: str
    model: str
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 120.0
    reasoning_effort: str | None = None

    @classmethod
    def from_env(cls) -> "LLMSettings":
        return cls(
            provider=os.environ.get("QUANTOS_LLM_PROVIDER", ""),
            model=os.environ.get("QUANTOS_LLM_MODEL", ""),
            connect_timeout_seconds=_positive_float_env(
                "QUANTOS_LLM_CONNECT_TIMEOUT_SECONDS", 10.0,
            ),
            read_timeout_seconds=_positive_float_env(
                "QUANTOS_LLM_READ_TIMEOUT_SECONDS", 120.0,
            ),
            reasoning_effort=_optional_reasoning_effort_env(),
        )


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _optional_reasoning_effort_env() -> str | None:
    value = os.environ.get("QUANTOS_LLM_REASONING_EFFORT")
    if value in {None, ""}:
        return None
    if value != "none":
        raise ValueError("QUANTOS_LLM_REASONING_EFFORT must be none when set")
    return value


@dataclass(frozen=True, slots=True)
class SynthesisBatchSettings:
    """Batch-only controls; they never alter candidate ranking or LLM content."""

    synthesis_top_n: int = 20
    max_concurrency: int = 2

    @classmethod
    def from_env(cls) -> "SynthesisBatchSettings":
        return cls(
            synthesis_top_n=_positive_int_env("QUANTOS_SYNTHESIS_TOP_N", 20),
            max_concurrency=_positive_int_env("QUANTOS_LLM_MAX_CONCURRENCY", 2),
        )


@dataclass(frozen=True, slots=True)
class KnowledgeIntegrationSettings:
    """Explicit, backward-compatible controls for local Knowledge preparation."""

    enabled: bool = False
    lexical_index_version: str | None = None
    retrieval_limit: int = 10
    context_max_items: int = 10
    context_max_chars: int = 10_000
    query_strategy_version: str = KNOWLEDGE_QUERY_STRATEGY_VERSION

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("knowledge enabled must be boolean")
        if self.lexical_index_version is not None and not _SHA256_DIGEST.fullmatch(
            self.lexical_index_version
        ):
            raise ValueError("knowledge lexical index version must be a SHA-256 digest")
        if self.enabled and self.lexical_index_version is None:
            raise ValueError("enabled knowledge requires an explicit lexical index version")
        if type(self.retrieval_limit) is not int or not 1 <= self.retrieval_limit <= 100:
            raise ValueError("knowledge retrieval limit is outside the supported range")
        if type(self.context_max_items) is not int or not 1 <= self.context_max_items <= 100:
            raise ValueError("knowledge context max_items is outside the supported range")
        if type(self.context_max_chars) is not int or self.context_max_chars < 1:
            raise ValueError("knowledge context max_chars must be positive")
        if self.query_strategy_version != KNOWLEDGE_QUERY_STRATEGY_VERSION:
            raise ValueError("unsupported knowledge query strategy")

    @classmethod
    def from_env(cls) -> "KnowledgeIntegrationSettings":
        return cls(
            enabled=_boolean_env("QUANTOS_KNOWLEDGE_ENABLED", False),
            lexical_index_version=os.environ.get(
                "QUANTOS_KNOWLEDGE_LEXICAL_INDEX_VERSION"
            ) or None,
            retrieval_limit=_positive_int_env("QUANTOS_KNOWLEDGE_RETRIEVAL_LIMIT", 10),
            context_max_items=_positive_int_env(
                "QUANTOS_KNOWLEDGE_CONTEXT_MAX_ITEMS", 10,
            ),
            context_max_chars=_positive_int_env(
                "QUANTOS_KNOWLEDGE_CONTEXT_MAX_CHARS", 10_000,
            ),
        )


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _boolean_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true"}:
        return True
    if normalized in {"0", "false"}:
        return False
    raise ValueError(f"{name} must be true/false or 1/0")
