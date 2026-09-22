"""Persistent bitemporal Security Master behavior for Phase 6B."""

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from quantos.config import MARKET_TIMEZONE, Settings
from quantos.schemas import SecurityMaster
from quantos.storage import (
    SecurityMasterRepository,
    SecurityMasterUnavailableError,
)


OBSERVED = datetime(2026, 9, 19, 9, tzinfo=MARKET_TIMEZONE)


def security(symbol="600519.SH", name="贵州茅台", **overrides):
    values = {
        "symbol": symbol,
        "company_name": name,
        "exchange": "SHSE" if symbol.endswith(".SH") else "SZSE",
        "effective_from": date(2001, 8, 27),
        "available_at": OBSERVED,
        "source": "tushare",
    }
    values.update(overrides)
    return SecurityMaster(**values)


def repository(tmp_path):
    return SecurityMasterRepository(Settings.from_project_root(tmp_path))


def test_missing_bootstrap_fails_closed_without_creating_storage(tmp_path):
    repo = repository(tmp_path)

    with pytest.raises(SecurityMasterUnavailableError, match="not initialized"):
        repo.list_as_of(OBSERVED)

    assert not repo.root.exists()


def test_snapshot_round_trip_and_repeat_is_content_idempotent(tmp_path):
    repo = repository(tmp_path)
    first = repo.write_snapshot((security(),), provider_id="tushare", observed_at=OBSERVED)
    repeated = repo.write_snapshot(
        (replace(security(), available_at=OBSERVED + timedelta(minutes=5)),),
        provider_id="tushare", observed_at=OBSERVED + timedelta(minutes=5),
    )

    assert first.created is True
    assert repeated.created is False
    assert repeated.snapshot.snapshot_id == first.snapshot.snapshot_id
    assert repo.read(first.path) == first.snapshot
    assert len(repo.list_snapshots()) == 1
    assert first.snapshot.records[0].canonical_security_id


def test_refresh_name_change_preserves_pit_history_and_alias(tmp_path):
    repo = repository(tmp_path)
    first = repo.write_snapshot(
        (security(name="贵州茅台旧名"),), provider_id="tushare", observed_at=OBSERVED,
    )
    changed_at = OBSERVED + timedelta(days=1)
    second = repo.write_snapshot(
        (replace(security(name="贵州茅台新名"), available_at=changed_at),),
        provider_id="tushare", observed_at=changed_at,
    )

    old = repo.resolve("贵州茅台旧名", as_of_time=OBSERVED + timedelta(hours=1))
    new = repo.resolve("贵州茅台新名", as_of_time=changed_at + timedelta(hours=1))

    assert first.snapshot.snapshot_id != second.snapshot.snapshot_id
    assert old[0].company_name == "贵州茅台旧名"
    assert new[0].company_name == "贵州茅台新名"
    assert "贵州茅台旧名" in new[0].aliases
    assert repo.resolve("贵州茅台旧名", as_of_time=changed_at + timedelta(hours=1)) == new


def test_future_observation_cannot_leak_into_historical_resolution(tmp_path):
    repo = repository(tmp_path)
    repo.write_snapshot((security(),), provider_id="tushare", observed_at=OBSERVED)

    with pytest.raises(SecurityMasterUnavailableError, match="no snapshot is visible"):
        repo.resolve("600519.SH", as_of_time=OBSERVED - timedelta(seconds=1))


def test_delisted_missing_and_ambiguous_resolution(tmp_path):
    repo = repository(tmp_path)
    delisted = security(
        effective_to=date(2026, 9, 18), is_active=False, status="delisted",
    )
    other = security(
        symbol="000001.SZ", name="同名公司", exchange="SZSE",
        effective_from=date(1991, 4, 3), aliases=("共同别名",),
    )
    third = security(
        symbol="600000.SH", name="另一公司", effective_from=date(1999, 11, 10),
        aliases=("共同别名",),
    )
    repo.write_snapshot(
        (delisted, other, third), provider_id="tushare", observed_at=OBSERVED,
    )

    assert repo.resolve("600519.SH", as_of_time=OBSERVED) == ()
    assert repo.resolve("不存在", as_of_time=OBSERVED) == ()
    assert {item.symbol for item in repo.resolve("共同别名", as_of_time=OBSERVED)} == {
        "000001.SZ", "600000.SH",
    }
