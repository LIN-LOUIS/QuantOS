"""Small Ask-facing result contracts over existing QuantOS capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _refs(values: tuple[str, ...], name: str) -> None:
    if type(values) is not tuple or len(values) != len(set(values)) or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise ValueError(f"INVALID_{name.upper()}")


def _facts(values: tuple[dict[str, Any], ...], name: str) -> None:
    if type(values) is not tuple or any(type(value) is not dict for value in values):
        raise ValueError(f"INVALID_{name.upper()}")


@dataclass(frozen=True, slots=True)
class AttributionLookupResult:
    """A projection of an existing Attribution result; it never ranks evidence."""

    evidence_refs: tuple[str, ...] = ()
    eligible_refs: tuple[str, ...] = ()
    strict_refs: tuple[str, ...] = ()
    causal_allowed: bool = False
    report_refs: tuple[str, ...] = ()
    limitation: str | None = None

    def __post_init__(self) -> None:
        for values, name in ((self.evidence_refs, "evidence refs"),
                             (self.eligible_refs, "eligible refs"),
                             (self.strict_refs, "strict refs"),
                             (self.report_refs, "report refs")):
            _refs(values, name)
        if not set(self.eligible_refs) <= set(self.evidence_refs):
            raise ValueError("INVALID_ATTRIBUTION_REFS")
        if self.causal_allowed and not self.strict_refs:
            raise ValueError("CAUSAL_ATTRIBUTION_REQUIRES_STRICT_EVIDENCE")
        if self.limitation is not None and (
            not isinstance(self.limitation, str) or not self.limitation.strip()
        ):
            raise ValueError("INVALID_ATTRIBUTION_LIMITATION")


@dataclass(frozen=True, slots=True)
class KnowledgeQueryResult:
    refs: tuple[str, ...]
    facts: tuple[dict[str, Any], ...]

    def __post_init__(self) -> None:
        _refs(self.refs, "knowledge refs")
        _facts(self.facts, "knowledge facts")


@dataclass(frozen=True, slots=True)
class ReportLookupResult:
    refs: tuple[str, ...]
    facts: tuple[dict[str, Any], ...]
    run_id: str | None = None

    def __post_init__(self) -> None:
        _refs(self.refs, "report refs")
        _facts(self.facts, "report facts")
        if self.run_id is not None and (not isinstance(self.run_id, str) or not self.run_id):
            raise ValueError("INVALID_RUN_ID")


@dataclass(frozen=True, slots=True)
class SectorPerformanceResult:
    refs: tuple[str, ...]
    facts: tuple[dict[str, Any], ...]

    def __post_init__(self) -> None:
        _refs(self.refs, "sector refs")
        _facts(self.facts, "sector facts")


@dataclass(frozen=True, slots=True)
class SystemStatusResult:
    facts: tuple[dict[str, Any], ...]

    def __post_init__(self) -> None:
        _facts(self.facts, "status facts")
