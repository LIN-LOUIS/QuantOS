"""Immutable observability records for a single QuantOS job run.

``JobContext`` remains the source of truth for run identity and Point-in-Time
cutoffs.  Observability records reference it instead of introducing a second,
overlapping run context.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping

from quantos.schemas import JobContext
from quantos.schemas._validation import require_aware, require_non_empty


class ResultStatus(str, Enum):
    SUCCESS = "success"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


class ValidationCategory(str, Enum):
    """Separates system correctness from legitimate market observations."""

    SYSTEM_INTEGRITY = "system_integrity"
    MARKET_OBSERVATION = "market_observation"


class RunHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    rule_name: str
    category: ValidationCategory
    status: ResultStatus
    checked_count: int
    failed_count: int = 0
    message: str | None = None
    metrics: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_non_empty(self.rule_name, "rule_name")
        if self.status is ResultStatus.SKIPPED:
            raise ValueError("validation status cannot be skipped")
        if self.checked_count < 0 or self.failed_count < 0:
            raise ValueError("validation counts must be non-negative")
        if self.failed_count > self.checked_count:
            raise ValueError("failed_count cannot exceed checked_count")
        if (
            self.category is ValidationCategory.MARKET_OBSERVATION
            and self.status is ResultStatus.FAILED
        ):
            raise ValueError("market observations cannot fail system health")


@dataclass(frozen=True, slots=True)
class ModuleResult:
    module_name: str
    job_context: JobContext
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    status: ResultStatus
    input_count: int
    output_count: int
    metrics: Mapping[str, object] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    validations: tuple[ValidationResult, ...] = ()
    dependency_failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_non_empty(self.module_name, "module_name")
        require_aware(self.started_at, "started_at")
        require_aware(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if not math.isfinite(self.duration_ms) or self.duration_ms < 0:
            raise ValueError("duration_ms must be finite and non-negative")
        if self.input_count < 0 or self.output_count < 0:
            raise ValueError("module counts must be non-negative")
        if self.status is ResultStatus.SKIPPED and not self.dependency_failures:
            raise ValueError("skipped modules require a failed dependency")
        if self.status is not ResultStatus.SKIPPED and self.dependency_failures:
            raise ValueError("dependency_failures are only valid for skipped modules")

    @property
    def run_id(self) -> str:
        return self.job_context.run_id

    @property
    def as_of_time(self) -> datetime:
        return self.job_context.as_of_time


@dataclass(frozen=True, slots=True)
class RunHealthReport:
    job_context: JobContext
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    status: RunHealth
    modules: tuple[ModuleResult, ...]
    warning_count: int
    error_count: int

    def __post_init__(self) -> None:
        require_aware(self.started_at, "started_at")
        require_aware(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if not math.isfinite(self.duration_ms) or self.duration_ms < 0:
            raise ValueError("duration_ms must be finite and non-negative")
        if self.warning_count < 0 or self.error_count < 0:
            raise ValueError("report counts must be non-negative")
        if any(module.job_context != self.job_context for module in self.modules):
            raise ValueError("all modules must reference the same JobContext as the report")

    @property
    def run_id(self) -> str:
        return self.job_context.run_id

    @property
    def as_of_time(self) -> datetime:
        return self.job_context.as_of_time

    @classmethod
    def from_modules(
        cls,
        *,
        job_context: JobContext,
        started_at: datetime,
        finished_at: datetime,
        duration_ms: float,
        modules: tuple[ModuleResult, ...],
    ) -> RunHealthReport:
        if any(
            module.status in {ResultStatus.FAILED, ResultStatus.SKIPPED}
            for module in modules
        ):
            health = RunHealth.UNHEALTHY
        elif any(module.status is ResultStatus.WARNING for module in modules):
            health = RunHealth.DEGRADED
        else:
            health = RunHealth.HEALTHY
        return cls(
            job_context=job_context,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            status=health,
            modules=modules,
            warning_count=sum(len(module.warnings) for module in modules),
            error_count=sum(len(module.errors) for module in modules),
        )
