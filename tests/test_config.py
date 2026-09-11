from pathlib import Path

from quantos.config import (
    KNOWLEDGE_QUERY_STRATEGY_VERSION,
    KnowledgeIntegrationSettings,
    LLMSettings,
    Settings,
    SynthesisBatchSettings,
)


def test_settings_build_separate_raw_and_normalized_paths(tmp_path: Path) -> None:
    settings = Settings.from_project_root(tmp_path)

    assert settings.raw_market_dir == tmp_path / "data/raw/market"
    assert settings.normalized_market_dir == tmp_path / "data/normalized/market"
    assert settings.sector_snapshot_dir == tmp_path / "data/normalized/sector_snapshots"
    assert settings.sector_membership_snapshot_dir == (
        tmp_path / "data/normalized/sector_memberships"
    )
    assert settings.moneyflow_dir == tmp_path / "data/normalized/moneyflow"
    assert settings.news_dir == tmp_path / "data/normalized/news"
    assert settings.announcement_dir == tmp_path / "data/normalized/announcements"
    assert settings.synthesis_dir == tmp_path / "data/derived/synthesis"
    assert settings.report_dir == tmp_path / "data/derived/reports"
    assert settings.raw_market_dir != settings.normalized_market_dir
    assert str(settings.market_timezone) == "Asia/Shanghai"


def test_settings_create_runtime_directories(tmp_path: Path) -> None:
    settings = Settings.from_project_root(tmp_path)

    settings.ensure_directories()

    assert settings.raw_market_dir.is_dir()
    assert settings.normalized_market_dir.is_dir()
    assert settings.sector_snapshot_dir.is_dir()
    assert settings.sector_membership_snapshot_dir.is_dir()
    assert settings.moneyflow_dir.is_dir()
    assert settings.news_dir.is_dir()
    assert settings.announcement_dir.is_dir()
    assert settings.synthesis_dir.is_dir()
    assert settings.report_dir.is_dir()
    assert settings.duckdb_path.parent.is_dir()


def test_llm_generation_timeouts_have_bounded_independent_defaults(monkeypatch) -> None:
    monkeypatch.delenv("QUANTOS_LLM_CONNECT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("QUANTOS_LLM_READ_TIMEOUT_SECONDS", raising=False)
    settings = LLMSettings.from_env()
    assert settings.connect_timeout_seconds == 10.0
    assert settings.read_timeout_seconds == 120.0


def test_llm_generation_timeouts_are_environment_configurable(monkeypatch) -> None:
    monkeypatch.setenv("QUANTOS_LLM_CONNECT_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("QUANTOS_LLM_READ_TIMEOUT_SECONDS", "150")
    settings = LLMSettings.from_env()
    assert settings.connect_timeout_seconds == 7.5
    assert settings.read_timeout_seconds == 150.0


def test_llm_reasoning_effort_is_optional_and_none_is_supported(monkeypatch) -> None:
    monkeypatch.delenv("QUANTOS_LLM_REASONING_EFFORT", raising=False)
    assert LLMSettings.from_env().reasoning_effort is None
    monkeypatch.setenv("QUANTOS_LLM_REASONING_EFFORT", "none")
    assert LLMSettings.from_env().reasoning_effort == "none"


def test_llm_reasoning_effort_rejects_unapproved_values(monkeypatch) -> None:
    monkeypatch.setenv("QUANTOS_LLM_REASONING_EFFORT", "low")
    with __import__("pytest").raises(ValueError, match="must be none"):
        LLMSettings.from_env()


def test_synthesis_batch_settings_defaults_and_env(monkeypatch) -> None:
    monkeypatch.delenv("QUANTOS_SYNTHESIS_TOP_N", raising=False)
    monkeypatch.delenv("QUANTOS_LLM_MAX_CONCURRENCY", raising=False)
    assert SynthesisBatchSettings.from_env() == SynthesisBatchSettings(20, 2)
    monkeypatch.setenv("QUANTOS_SYNTHESIS_TOP_N", "7")
    monkeypatch.setenv("QUANTOS_LLM_MAX_CONCURRENCY", "3")
    assert SynthesisBatchSettings.from_env() == SynthesisBatchSettings(7, 3)


def test_synthesis_batch_settings_require_positive_values(monkeypatch) -> None:
    monkeypatch.setenv("QUANTOS_LLM_MAX_CONCURRENCY", "0")
    with __import__("pytest").raises(ValueError, match="must be positive"):
        SynthesisBatchSettings.from_env()


def test_knowledge_integration_is_disabled_by_default(monkeypatch) -> None:
    for name in (
        "QUANTOS_KNOWLEDGE_ENABLED",
        "QUANTOS_KNOWLEDGE_LEXICAL_INDEX_VERSION",
        "QUANTOS_KNOWLEDGE_RETRIEVAL_LIMIT",
        "QUANTOS_KNOWLEDGE_CONTEXT_MAX_ITEMS",
        "QUANTOS_KNOWLEDGE_CONTEXT_MAX_CHARS",
    ):
        monkeypatch.delenv(name, raising=False)
    value = KnowledgeIntegrationSettings.from_env()
    assert value == KnowledgeIntegrationSettings()
    assert value.enabled is False and value.lexical_index_version is None
    assert value.query_strategy_version == KNOWLEDGE_QUERY_STRATEGY_VERSION


def test_enabled_knowledge_requires_explicit_index_version(monkeypatch) -> None:
    monkeypatch.setenv("QUANTOS_KNOWLEDGE_ENABLED", "true")
    monkeypatch.delenv("QUANTOS_KNOWLEDGE_LEXICAL_INDEX_VERSION", raising=False)
    with __import__("pytest").raises(ValueError, match="explicit lexical index"):
        KnowledgeIntegrationSettings.from_env()


def test_knowledge_integration_settings_parse_bounded_env(monkeypatch) -> None:
    monkeypatch.setenv("QUANTOS_KNOWLEDGE_ENABLED", "1")
    monkeypatch.setenv("QUANTOS_KNOWLEDGE_LEXICAL_INDEX_VERSION", "a" * 64)
    monkeypatch.setenv("QUANTOS_KNOWLEDGE_RETRIEVAL_LIMIT", "7")
    monkeypatch.setenv("QUANTOS_KNOWLEDGE_CONTEXT_MAX_ITEMS", "5")
    monkeypatch.setenv("QUANTOS_KNOWLEDGE_CONTEXT_MAX_CHARS", "4096")
    value = KnowledgeIntegrationSettings.from_env()
    assert value == KnowledgeIntegrationSettings(True, "a" * 64, 7, 5, 4096)


def test_knowledge_integration_settings_reject_invalid_values(monkeypatch) -> None:
    monkeypatch.setenv("QUANTOS_KNOWLEDGE_ENABLED", "maybe")
    with __import__("pytest").raises(ValueError, match="true/false"):
        KnowledgeIntegrationSettings.from_env()
    with __import__("pytest").raises(ValueError, match="query strategy"):
        KnowledgeIntegrationSettings(query_strategy_version="candidate_subject:v2")
    for values in (
        {"enabled": 1},
        {"lexical_index_version": "A" * 64},
        {"retrieval_limit": 101},
        {"context_max_items": 0},
        {"context_max_chars": 0},
    ):
        with __import__("pytest").raises(ValueError):
            KnowledgeIntegrationSettings(**values)
