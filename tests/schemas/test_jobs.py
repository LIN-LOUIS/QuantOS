from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from quantos.schemas import JobContext, JobType

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_job_context_requires_aware_as_of_time() -> None:
    with pytest.raises(ValueError, match="as_of_time"):
        JobContext(
            run_id="run-1",
            job_type=JobType.MARKET_INGESTION,
            trade_date=date(2026, 8, 26),
            as_of_time=datetime(2026, 8, 26, 9, 40),
        )


def test_job_context_accepts_explicit_market_timezone() -> None:
    context = JobContext(
        run_id="run-1",
        job_type=JobType.MARKET_INGESTION,
        trade_date=date(2026, 8, 26),
        as_of_time=datetime(2026, 8, 26, 9, 40, tzinfo=SHANGHAI),
    )
    assert context.as_of_time.utcoffset().total_seconds() == 8 * 3600
