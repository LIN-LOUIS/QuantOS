from datetime import date, datetime
from zoneinfo import ZoneInfo

from quantos.moneyflow_backfill import backfill_moneyflows
from tests.storage.test_moneyflow import flow

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 9, 1, 23, 0, tzinfo=SHANGHAI)


class Provider:
    def __init__(self, failed=None):
        self.failed = failed

    def fetch_by_trade_date(self, *, trade_date, as_of_time):
        if trade_date == self.failed:
            raise RuntimeError("failed")
        return [flow(trade_date)]


class Repository:
    def __init__(self):
        self.ids = set()

    def write_records(self, records):
        new = [item for item in records if item.record_id not in self.ids]
        self.ids.update(item.record_id for item in new)
        return len(new)


def test_moneyflow_backfill_is_sorted_and_idempotent() -> None:
    repository = Repository()
    dates = [date(2026, 8, 31), date(2026, 8, 30)]
    first = backfill_moneyflows(Provider(), repository, trading_dates=dates, as_of_time=AS_OF)
    second = backfill_moneyflows(Provider(), repository, trading_dates=dates, as_of_time=AS_OF)
    assert first.trading_dates == tuple(sorted(dates))
    assert first.stored_records == 2
    assert second.stored_records == 0
    assert second.duplicate_records == 2


def test_moneyflow_backfill_reports_partial_provider_failure() -> None:
    failed = date(2026, 8, 31)
    report = backfill_moneyflows(
        Provider(failed), Repository(),
        trading_dates=[date(2026, 8, 30), failed], as_of_time=AS_OF,
    )
    assert report.days[0].status == "success"
    assert report.days[1].status == "failed"
    assert report.days[1].error_type == "RuntimeError"
