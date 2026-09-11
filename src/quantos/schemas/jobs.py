"""Execution context used to enforce reproducibility and PIT cutoffs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from ._validation import require_aware, require_non_empty


class JobType(str, Enum):
    MARKET_INGESTION = "market_ingestion"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class JobContext:
    run_id: str
    job_type: JobType
    trade_date: date
    as_of_time: datetime
    status: JobStatus = JobStatus.PENDING
    data_version: str = "v1"

    def __post_init__(self) -> None:
        require_non_empty(self.run_id, "run_id")
        require_non_empty(self.data_version, "data_version")
        require_aware(self.as_of_time, "as_of_time")
