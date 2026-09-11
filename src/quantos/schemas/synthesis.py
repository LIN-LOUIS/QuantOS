"""Strict immutable schemas for evidence-only LLM synthesis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import re

from ._validation import require_aware, require_non_empty
from .knowledge_context import (
    KnowledgeContextBundle,
    KnowledgeContextIntegrityError,
    validate_knowledge_context_bundle,
)

SYNTHESIS_MODES = frozenset({"research", "strict_live"})
CAUSAL_STATUSES = frozenset({"associated_not_proven", "insufficient_evidence"})
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_KNOWLEDGE_REF = re.compile(r"^K[1-9][0-9]*$")


@dataclass(frozen=True, slots=True)
class SynthesisCandidate:
    symbol: str
    company_name: str
    rank: int
    trade_date: date
    market_event_time: datetime

    def __post_init__(self) -> None:
        require_non_empty(self.symbol, "symbol")
        require_non_empty(self.company_name, "company_name")
        if self.rank < 1:
            raise ValueError("rank must be positive")
        require_aware(self.market_event_time, "market_event_time")


@dataclass(frozen=True, slots=True)
class SynthesisMarketFacts:
    return_pct: Decimal | None
    return_zscore: Decimal | None
    volume_ratio: Decimal | None
    amount_ratio: Decimal | None
    signal_type: str
    signal_count: int


@dataclass(frozen=True, slots=True)
class SynthesisFundFlowFacts:
    net_mf_amount: Decimal
    net_mf_ratio: Decimal | None
    large_plus_extra_net_amount: Decimal
    large_plus_extra_ratio: Decimal | None
    flow_direction: str


@dataclass(frozen=True, slots=True)
class SynthesisEvidence:
    evidence_id: str
    provider: str
    evidence_basis: str
    evidence_tier: int
    title: str
    snippet: str
    published_at: datetime | None
    available_at: datetime
    source_temporal_relation: str
    knowledge_temporal_relation: str
    entity_strength: str
    match_method: str
    pit_mode: str
    strict_pit: bool
    domain: str
    url: str

    def __post_init__(self) -> None:
        require_non_empty(self.evidence_id, "evidence_id")
        require_aware(self.available_at, "available_at")
        if self.published_at is not None:
            require_aware(self.published_at, "published_at")


@dataclass(frozen=True, slots=True)
class SynthesisInput:
    mode: str
    candidate: SynthesisCandidate
    market_facts: SynthesisMarketFacts
    fund_flow: SynthesisFundFlowFacts | None
    attribution_evidence: tuple[SynthesisEvidence, ...]
    post_event_context: tuple[SynthesisEvidence, ...]
    sector_context: str | None
    input_bundle_id: str
    prompt_version: str = "quantos-synthesis-v1"
    schema_version: str = "v1"
    knowledge_context: KnowledgeContextBundle | None = None

    def __post_init__(self) -> None:
        if self.mode not in SYNTHESIS_MODES:
            raise ValueError("synthesis mode is invalid")
        require_non_empty(self.input_bundle_id, "input_bundle_id")
        if self.knowledge_context is not None:
            try:
                validate_knowledge_context_bundle(self.knowledge_context)
            except (TypeError, KnowledgeContextIntegrityError) as exc:
                raise ValueError("knowledge context failed synthesis validation") from exc
            if self.knowledge_context.mode.value != self.mode:
                raise ValueError("knowledge context mode does not match synthesis mode")
            if self.knowledge_context.as_of_time != self.candidate.market_event_time:
                raise ValueError("knowledge context as_of_time does not match market event time")
            if self.mode == "strict_live" and self.knowledge_context.retrospective_items:
                raise ValueError("strict synthesis cannot consume retrospective knowledge")

    @property
    def symbol(self) -> str:
        return self.candidate.symbol


@dataclass(frozen=True, slots=True)
class PossibleExplanation:
    statement: str
    supporting_evidence_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    causal_status: str

    def __post_init__(self) -> None:
        require_non_empty(self.statement, "statement")
        if self.causal_status not in CAUSAL_STATUSES:
            raise ValueError("causal_status is invalid")


@dataclass(frozen=True, slots=True)
class KnowledgeBackgroundStatement:
    statement: str
    knowledge_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        require_non_empty(self.statement, "statement")
        if (type(self.knowledge_refs) is not tuple or not self.knowledge_refs
                or len(self.knowledge_refs) != len(set(self.knowledge_refs))
                or any(
                    type(item) is not str or not _KNOWLEDGE_REF.fullmatch(item)
                    for item in self.knowledge_refs
                )):
            raise ValueError("knowledge references must be non-empty and unique")


@dataclass(frozen=True, slots=True)
class EvidenceSynthesis:
    symbol: str
    mode: str
    evidence_summary: str
    possible_explanations: tuple[PossibleExplanation, ...]
    contradicting_signals: tuple[str, ...]
    post_event_notes: tuple[str, ...]
    insufficient_evidence: bool
    limitations: tuple[str, ...]
    used_evidence_ids: tuple[str, ...]
    prompt_version: str
    schema_version: str
    model_provider: str
    model_name: str
    generated_at: datetime
    input_bundle_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    duration_ms: int | None = None
    knowledge_background: tuple[KnowledgeBackgroundStatement, ...] = ()
    retrospective_knowledge_context: tuple[KnowledgeBackgroundStatement, ...] = ()
    supplied_context_id: str | None = None
    supplied_knowledge_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in SYNTHESIS_MODES:
            raise ValueError("synthesis mode is invalid")
        require_aware(self.generated_at, "generated_at")
        for values, name in (
            (self.knowledge_background, "knowledge_background"),
            (self.retrospective_knowledge_context, "retrospective_knowledge_context"),
        ):
            if type(values) is not tuple or any(
                not isinstance(item, KnowledgeBackgroundStatement) for item in values
            ):
                raise ValueError(f"invalid {name}")
        if type(self.supplied_knowledge_refs) is not tuple or len(
            self.supplied_knowledge_refs
        ) != len(set(self.supplied_knowledge_refs)) or any(
            type(item) is not str or not _KNOWLEDGE_REF.fullmatch(item)
            for item in self.supplied_knowledge_refs
        ):
            raise ValueError("supplied knowledge references must be unique")
        if self.supplied_context_id is None:
            if self.supplied_knowledge_refs or self.knowledge_background or self.retrospective_knowledge_context:
                raise ValueError("knowledge output requires a supplied context")
        else:
            if not _DIGEST.fullmatch(self.supplied_context_id):
                raise ValueError("supplied_context_id must be a SHA-256 digest")
        claimed_refs = {
            reference
            for item in (*self.knowledge_background, *self.retrospective_knowledge_context)
            for reference in item.knowledge_refs
        }
        if not claimed_refs <= set(self.supplied_knowledge_refs):
            raise ValueError("knowledge output references were not supplied")
