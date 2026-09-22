"""Pure temporal policies for replay evidence, knowledge, and reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def _aware(value: datetime, name: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ReplayEvidence:
    ref: str
    published_at: datetime | None
    available_at: datetime
    strict_pit: bool
    source: str

    def __post_init__(self) -> None:
        if not self.ref or not self.source:
            raise ValueError("replay evidence identity is required")
        _aware(self.available_at, "available_at")
        if self.published_at is not None:
            _aware(self.published_at, "published_at")


@dataclass(frozen=True, slots=True)
class HistoricalEvidenceEvaluation:
    visible_refs: tuple[str, ...]
    strict_refs: tuple[str, ...]
    causal_eligible_refs: tuple[str, ...]
    reason_codes: tuple[str, ...]
    pit_rejection_count: int


def evaluate_historical_evidence(
    records: tuple[ReplayEvidence, ...], *, cutoff: datetime,
) -> HistoricalEvidenceEvaluation:
    _aware(cutoff, "cutoff")
    visible: list[str] = []
    strict: list[str] = []
    reasons: set[str] = set()
    rejected = 0
    for item in records:
        if item.available_at > cutoff or item.published_at is None or item.published_at > cutoff:
            rejected += 1
            reasons.add("PIT_REJECTED")
            continue
        visible.append(item.ref)
        if item.strict_pit:
            strict.append(item.ref)
        else:
            reasons.add("NON_STRICT_PIT_EVIDENCE")
    return HistoricalEvidenceEvaluation(
        tuple(sorted(visible)), tuple(sorted(strict)), tuple(sorted(strict)),
        tuple(sorted(reasons)), rejected,
    )


def knowledge_is_visible(*, available_at: datetime, published_at: datetime | None,
                         effective_from: datetime | None, effective_to: datetime | None,
                         cutoff: datetime) -> bool:
    for name, value in (("available_at", available_at), ("cutoff", cutoff)):
        _aware(value, name)
    optional = (published_at, effective_from, effective_to)
    if any(value is not None and value.utcoffset() is None for value in optional):
        raise ValueError("knowledge timestamps must be timezone-aware")
    return (
        available_at <= cutoff
        and (published_at is None or published_at <= cutoff)
        and (effective_from is None or effective_from <= cutoff)
        and (effective_to is None or cutoff <= effective_to)
    )


def report_is_visible(*, as_of_time: datetime, generated_at: datetime,
                      cutoff: datetime) -> bool:
    for name, value in (("as_of_time", as_of_time), ("generated_at", generated_at),
                        ("cutoff", cutoff)):
        _aware(value, name)
    return as_of_time <= cutoff and generated_at <= cutoff
