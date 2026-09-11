from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest

from quantos.config import Settings
from quantos.schemas import SectorMembership, SectorSnapshot
from quantos.sectors import create_sector_membership_snapshot
from quantos.storage import SectorDataRepository

SHANGHAI = ZoneInfo("Asia/Shanghai")


def snapshot(
    trade_date: date,
    *,
    sector_code: str = "C15",
    available_at: datetime | None = None,
    mode: str = "strict_pit",
) -> SectorSnapshot:
    cutoff = datetime.combine(trade_date, datetime.min.time(), tzinfo=SHANGHAI).replace(
        hour=20
    )
    return SectorSnapshot(
        trade_date=trade_date,
        as_of_time=cutoff,
        sector_code=sector_code,
        sector_name="酒、饮料和精制茶制造业",
        classification="证监会行业分类",
        member_count=2,
        valid_bar_count=2,
        coverage_ratio=Decimal("1"),
        equal_weight_return_pct=Decimal("1.5"),
        median_return_pct=Decimal("1.5"),
        advancer_count=2,
        decliner_count=0,
        flat_count=0,
        advancer_ratio=Decimal("1"),
        total_volume=200,
        total_amount=Decimal("3000"),
        top_gainer_symbol="600519.SH",
        top_gainer_return_pct=Decimal("2"),
        top_loser_symbol="000001.SZ",
        top_loser_return_pct=Decimal("1"),
        available_at=available_at or cutoff.replace(hour=18),
        historical_membership_mode=mode,
    )


def membership(available_at: datetime) -> SectorMembership:
    return SectorMembership(
        symbol="600519.SH",
        company_name="贵州茅台",
        exchange="SHSE",
        sector_code="C15",
        sector_name="酒、饮料和精制茶制造业",
        classification="证监会行业分类",
        raw_industry="C15酒、饮料和精制茶制造业",
        source="baostock",
        effective_from=date(2001, 8, 27),
        available_at=available_at,
    )


@pytest.fixture
def repository(tmp_path: Path) -> SectorDataRepository:
    return SectorDataRepository(Settings.from_project_root(tmp_path))


def test_sector_snapshot_persistence_is_idempotent_and_round_trips(
    repository: SectorDataRepository,
) -> None:
    item = snapshot(date(2026, 9, 1))
    assert repository.write_snapshots([item, item]) == 1
    assert repository.write_snapshots([item]) == 0
    loaded = repository.load_snapshot_history(
        "C15",
        end_date=item.trade_date,
        lookback=20,
        as_of_time=item.as_of_time,
    )
    assert len(loaded) == 1
    assert loaded[0].sector_code == item.sector_code
    assert loaded[0].equal_weight_return_pct == item.equal_weight_return_pct
    assert loaded[0].historical_membership_mode == "strict_pit"


def test_sector_snapshot_history_is_ascending_and_lookback_limited(
    repository: SectorDataRepository,
) -> None:
    dates = [date(2026, 7, 1) + timedelta(days=index) for index in range(25)]
    repository.write_snapshots([snapshot(item) for item in dates])
    loaded = repository.load_snapshot_history(
        "C15",
        end_date=dates[-1],
        lookback=20,
        as_of_time=datetime(2026, 9, 1, 20, 0, tzinfo=SHANGHAI),
    )
    assert [item.trade_date for item in loaded] == dates[-20:]


def test_strict_and_proxy_sector_histories_cannot_be_confused(
    repository: SectorDataRepository,
) -> None:
    trade_date = date(2026, 8, 31)
    strict = snapshot(trade_date)
    proxy = replace(strict, historical_membership_mode="current_membership_proxy")
    assert repository.write_snapshots([strict, proxy]) == 2
    strict_rows = repository.load_snapshot_history(
        "C15",
        end_date=trade_date,
        lookback=20,
        as_of_time=strict.as_of_time,
        historical_membership_mode="strict_pit",
    )
    proxy_rows = repository.load_snapshot_history(
        "C15",
        end_date=trade_date,
        lookback=20,
        as_of_time=strict.as_of_time,
        historical_membership_mode="current_membership_proxy",
    )
    assert [item.historical_membership_mode for item in strict_rows] == ["strict_pit"]
    assert [item.historical_membership_mode for item in proxy_rows] == [
        "current_membership_proxy"
    ]


def test_daily_membership_snapshot_persistence_and_idempotency(
    repository: SectorDataRepository,
) -> None:
    as_of_date = date(2026, 9, 1)
    rows = create_sector_membership_snapshot(
        [membership(datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI))],
        as_of_date=as_of_date,
    )
    assert repository.write_membership_snapshots(rows) == 1
    assert repository.write_membership_snapshots(rows) == 0
    target = next(repository.settings.sector_membership_snapshot_dir.rglob("*.parquet"))
    with duckdb.connect() as connection:
        stored = connection.execute(
            "SELECT as_of_date, symbol, sector_code, classification, "
            "historical_membership_mode FROM read_parquet(?)",
            [str(target)],
        ).fetchone()
    assert stored == (
        as_of_date,
        "600519.SH",
        "C15",
        "证监会行业分类",
        "strict_pit",
    )


def test_historical_membership_proxy_must_be_explicit() -> None:
    historical_date = date(2026, 8, 31)
    observed_later = membership(datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI))
    with pytest.raises(ValueError, match="strict PIT"):
        create_sector_membership_snapshot(
            [observed_later],
            as_of_date=historical_date,
            historical_membership_mode="strict_pit",
        )
    rows = create_sector_membership_snapshot(
        [observed_later],
        as_of_date=historical_date,
        historical_membership_mode="current_membership_proxy",
    )
    assert rows[0].historical_membership_mode == "current_membership_proxy"
