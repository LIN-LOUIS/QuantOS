"""Immutable contracts for PIT-explicit knowledge context artifacts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import math
import re
import unicodedata

from quantos.serialization import canonical_json_bytes

from ._validation import require_aware
from .knowledge import KnowledgeQueryMode, KnowledgeSourceType, KnowledgeTemporalClass


KNOWLEDGE_CONTEXT_POLICY_SCHEMA_VERSION = "quantos-knowledge-context-policy-v1"
KNOWLEDGE_CONTEXT_ITEM_SCHEMA_VERSION = "quantos-knowledge-context-item-v1"
KNOWLEDGE_CONTEXT_BUNDLE_SCHEMA_VERSION = "quantos-knowledge-context-bundle-v1"
CONTEXT_SELECTION_ALGORITHM = "rank_prefix"
CONTEXT_SELECTION_VERSION = "v1"
MAX_CONTEXT_ITEMS = 100

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_FAMILY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SYMBOL = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


class KnowledgeContextLane(str, Enum):
    HISTORICALLY_KNOWN = "historically_known"
    RETROSPECTIVE_AFTER_EVENT = "retrospective_after_event"


class KnowledgeContextStopReason(str, Enum):
    NO_HITS = "no_hits"
    ALL_SELECTED = "all_selected"
    MAX_ITEMS = "max_items"
    CHAR_BUDGET = "char_budget"
    RETROSPECTIVE_NOT_ALLOWED = "retrospective_not_allowed"


class KnowledgeContextIntegrityError(ValueError):
    """Raised when an in-memory context artifact fails canonical validation."""


@dataclass(frozen=True, slots=True)
class KnowledgeContextPolicy:
    max_items: int
    max_context_chars: int
    allow_retrospective_research_context: bool
    schema_version: str = KNOWLEDGE_CONTEXT_POLICY_SCHEMA_VERSION
    selection_algorithm: str = CONTEXT_SELECTION_ALGORITHM
    selection_version: str = CONTEXT_SELECTION_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != KNOWLEDGE_CONTEXT_POLICY_SCHEMA_VERSION:
            raise ValueError("unsupported knowledge context policy schema")
        if (self.selection_algorithm != CONTEXT_SELECTION_ALGORITHM
                or self.selection_version != CONTEXT_SELECTION_VERSION):
            raise ValueError("unsupported knowledge context selection policy")
        if type(self.max_items) is not int or not 1 <= self.max_items <= MAX_CONTEXT_ITEMS:
            raise ValueError("context max_items is outside the supported range")
        if type(self.max_context_chars) is not int or self.max_context_chars < 1:
            raise ValueError("max_context_chars must be a positive integer")
        if type(self.allow_retrospective_research_context) is not bool:
            raise ValueError("retrospective context policy must be boolean")


@dataclass(frozen=True, slots=True)
class KnowledgeContextItem:
    schema_version: str
    lane: KnowledgeContextLane
    chunk_id: str
    document_id: str
    document_family_id: str
    document_version: int
    retrieval_rank: int
    retrieval_score: float
    content: str
    content_hash: str
    content_char_count: int
    source: str
    source_type: KnowledgeSourceType
    title: str
    published_at: datetime | None
    available_at: datetime
    effective_from: datetime | None
    effective_to: datetime | None
    temporal_class: KnowledgeTemporalClass
    entity_refs: tuple[str, ...]
    provenance_source_identifier: str
    origin_reference: str | None
    known_by_historical_as_of: bool
    knowledge_after_event: bool

    def __post_init__(self) -> None:
        if self.schema_version != KNOWLEDGE_CONTEXT_ITEM_SCHEMA_VERSION:
            raise ValueError("unsupported knowledge context item schema")
        if not isinstance(self.lane, KnowledgeContextLane):
            raise ValueError("invalid knowledge context lane")
        _digest(self.chunk_id, "chunk_id")
        _digest(self.document_id, "document_id")
        _digest(self.content_hash, "content_hash")
        _family(self.document_family_id)
        if type(self.document_version) is not int or self.document_version < 1:
            raise ValueError("document_version must be a positive integer")
        if type(self.retrieval_rank) is not int or self.retrieval_rank < 1:
            raise ValueError("retrieval_rank must be a positive integer")
        if (type(self.retrieval_score) is not float
                or not math.isfinite(self.retrieval_score) or self.retrieval_score <= 0):
            raise ValueError("invalid lexical retrieval score")
        if type(self.content) is not str or not self.content or not self.content.strip():
            raise ValueError("context content must not be empty")
        if self.content != unicodedata.normalize("NFC", self.content) or "\r" in self.content:
            raise ValueError("context content must be canonical")
        if hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.content_hash:
            raise ValueError("context content hash mismatch")
        if type(self.content_char_count) is not int or self.content_char_count != len(self.content):
            raise ValueError("context character count mismatch")
        for value, name in (
            (self.source, "source"), (self.title, "title"),
            (self.provenance_source_identifier, "provenance_source_identifier"),
        ):
            _canonical_text(value, name)
        if self.origin_reference is not None:
            _canonical_text(self.origin_reference, "origin_reference")
        if not isinstance(self.source_type, KnowledgeSourceType):
            raise ValueError("invalid knowledge source type")
        if not isinstance(self.temporal_class, KnowledgeTemporalClass):
            raise ValueError("invalid knowledge temporal class")
        _entity_refs(self.entity_refs)
        require_aware(self.available_at, "available_at")
        for value, name in (
            (self.published_at, "published_at"),
            (self.effective_from, "effective_from"),
            (self.effective_to, "effective_to"),
        ):
            if value is not None:
                require_aware(value, name)
        if self.published_at is not None and self.available_at < self.published_at:
            raise ValueError("available_at cannot precede published_at")
        if self.temporal_class is KnowledgeTemporalClass.TIMELESS:
            if self.effective_from is not None or self.effective_to is not None:
                raise ValueError("timeless context cannot have effective bounds")
        elif self.temporal_class is KnowledgeTemporalClass.SEMI_STATIC:
            if self.effective_from is None:
                raise ValueError("semi-static context requires effective_from")
        elif self.effective_from is None or self.effective_to is None:
            raise ValueError("time-bounded context requires both effective bounds")
        if (self.effective_from is not None and self.effective_to is not None
                and self.effective_to < self.effective_from):
            raise ValueError("effective_to cannot precede effective_from")
        if (type(self.known_by_historical_as_of) is not bool
                or type(self.knowledge_after_event) is not bool):
            raise ValueError("knowledge timing flags must be boolean")
        if self.known_by_historical_as_of == self.knowledge_after_event:
            raise ValueError("knowledge timing flags must be complementary")
        if self.lane is KnowledgeContextLane.HISTORICALLY_KNOWN:
            if not self.known_by_historical_as_of or self.knowledge_after_event:
                raise ValueError("historical lane requires historically known knowledge")
        elif self.known_by_historical_as_of or not self.knowledge_after_event:
            raise ValueError("retrospective lane requires after-event knowledge")


@dataclass(frozen=True, slots=True)
class KnowledgeContextBundle:
    """Structured context whose character budget sums item content code points only."""

    schema_version: str
    context_id: str
    policy: KnowledgeContextPolicy
    retrieval_request_id: str
    mode: KnowledgeQueryMode
    as_of_time: datetime
    corpus_cutoff: datetime | None
    corpus_version: str
    chunk_manifest_version: str
    lexical_index_version: str
    retrieved_hit_count: int
    selected_item_count: int
    selected_char_count: int
    historical_item_count: int
    retrospective_item_count: int
    omitted_hit_count: int
    selection_stop_reason: KnowledgeContextStopReason
    historical_items: tuple[KnowledgeContextItem, ...]
    retrospective_items: tuple[KnowledgeContextItem, ...]
    generated_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != KNOWLEDGE_CONTEXT_BUNDLE_SCHEMA_VERSION:
            raise ValueError("unsupported knowledge context bundle schema")
        _digest(self.context_id, "context_id")
        _digest(self.retrieval_request_id, "retrieval_request_id")
        _digest(self.corpus_version, "corpus_version")
        _digest(self.chunk_manifest_version, "chunk_manifest_version")
        _digest(self.lexical_index_version, "lexical_index_version")
        if not isinstance(self.policy, KnowledgeContextPolicy):
            raise ValueError("knowledge context policy required")
        if not isinstance(self.mode, KnowledgeQueryMode):
            raise ValueError("invalid knowledge context mode")
        if not isinstance(self.selection_stop_reason, KnowledgeContextStopReason):
            raise ValueError("invalid knowledge context stop reason")
        require_aware(self.as_of_time, "as_of_time")
        require_aware(self.generated_at, "generated_at")
        if self.mode is KnowledgeQueryMode.STRICT_LIVE:
            if self.corpus_cutoff is not None:
                raise ValueError("strict context does not accept corpus_cutoff")
        else:
            if self.corpus_cutoff is None:
                raise ValueError("research context requires corpus_cutoff")
            require_aware(self.corpus_cutoff, "corpus_cutoff")
            if self.as_of_time > self.corpus_cutoff:
                raise ValueError("historical as-of cannot exceed corpus cutoff")
        for values, lane, name in (
            (self.historical_items, KnowledgeContextLane.HISTORICALLY_KNOWN,
             "historical_items"),
            (self.retrospective_items, KnowledgeContextLane.RETROSPECTIVE_AFTER_EVENT,
             "retrospective_items"),
        ):
            if type(values) is not tuple or any(
                not isinstance(item, KnowledgeContextItem) or item.lane is not lane
                for item in values
            ):
                raise ValueError(f"invalid {name}")
            ranks = tuple(item.retrieval_rank for item in values)
            if ranks != tuple(sorted(ranks)):
                raise ValueError(f"{name} must preserve retrieval rank")
        selected = tuple(sorted(
            self.historical_items + self.retrospective_items,
            key=lambda item: item.retrieval_rank,
        ))
        if tuple(item.retrieval_rank for item in selected) != tuple(
            range(1, len(selected) + 1)
        ):
            raise ValueError("selected context must be a retrieval-rank prefix")
        if len({item.chunk_id for item in selected}) != len(selected):
            raise ValueError("context contains duplicate chunk IDs")
        for value, name in (
            (self.retrieved_hit_count, "retrieved_hit_count"),
            (self.selected_item_count, "selected_item_count"),
            (self.selected_char_count, "selected_char_count"),
            (self.historical_item_count, "historical_item_count"),
            (self.retrospective_item_count, "retrospective_item_count"),
            (self.omitted_hit_count, "omitted_hit_count"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        expected_chars = sum(item.content_char_count for item in selected)
        expected_counts = (
            len(selected), expected_chars, len(self.historical_items),
            len(self.retrospective_items), self.retrieved_hit_count - len(selected),
        )
        actual_counts = (
            self.selected_item_count, self.selected_char_count,
            self.historical_item_count, self.retrospective_item_count,
            self.omitted_hit_count,
        )
        if actual_counts != expected_counts or self.retrieved_hit_count < len(selected):
            raise ValueError("knowledge context counts are inconsistent")
        if len(selected) > self.policy.max_items or expected_chars > self.policy.max_context_chars:
            raise ValueError("knowledge context exceeds policy budget")
        if self.mode is KnowledgeQueryMode.STRICT_LIVE and self.retrospective_items:
            raise ValueError("strict context cannot contain retrospective knowledge")
        if self.retrospective_items and not self.policy.allow_retrospective_research_context:
            raise ValueError("policy forbids retrospective context")
        for item in self.historical_items:
            if item.available_at > self.as_of_time:
                raise ValueError("historical context violates PIT cutoff")
        for item in self.retrospective_items:
            if (item.available_at <= self.as_of_time or self.corpus_cutoff is None
                    or item.available_at > self.corpus_cutoff):
                raise ValueError("retrospective context violates research cutoff")
        _validate_stop_reason(self, len(selected))
        if self.context_id != knowledge_context_id(
            policy=self.policy,
            retrieval_request_id=self.retrieval_request_id,
            mode=self.mode,
            as_of_time=self.as_of_time,
            corpus_cutoff=self.corpus_cutoff,
            corpus_version=self.corpus_version,
            chunk_manifest_version=self.chunk_manifest_version,
            lexical_index_version=self.lexical_index_version,
            retrieved_hit_count=self.retrieved_hit_count,
            selection_stop_reason=self.selection_stop_reason,
            selected_items=selected,
        ):
            raise ValueError("knowledge context identity mismatch")

    @property
    def selected_items(self) -> tuple[KnowledgeContextItem, ...]:
        return tuple(sorted(
            self.historical_items + self.retrospective_items,
            key=lambda item: item.retrieval_rank,
        ))


def knowledge_context_id(
    *, policy: KnowledgeContextPolicy, retrieval_request_id: str,
    mode: KnowledgeQueryMode, as_of_time: datetime, corpus_cutoff: datetime | None,
    corpus_version: str, chunk_manifest_version: str, lexical_index_version: str,
    retrieved_hit_count: int, selection_stop_reason: KnowledgeContextStopReason,
    selected_items: tuple[KnowledgeContextItem, ...],
) -> str:
    if not isinstance(policy, KnowledgeContextPolicy):
        raise TypeError("KnowledgeContextPolicy required")
    for value, name in (
        (retrieval_request_id, "retrieval_request_id"),
        (corpus_version, "corpus_version"),
        (chunk_manifest_version, "chunk_manifest_version"),
        (lexical_index_version, "lexical_index_version"),
    ):
        _digest(value, name)
    if not isinstance(mode, KnowledgeQueryMode):
        raise ValueError("invalid knowledge context mode")
    require_aware(as_of_time, "as_of_time")
    if corpus_cutoff is not None:
        require_aware(corpus_cutoff, "corpus_cutoff")
    if type(retrieved_hit_count) is not int or retrieved_hit_count < 0:
        raise ValueError("invalid retrieved hit count")
    if not isinstance(selection_stop_reason, KnowledgeContextStopReason):
        raise ValueError("invalid context stop reason")
    if type(selected_items) is not tuple:
        raise ValueError("selected context items must be a tuple")
    identity = {
        "schema_version": KNOWLEDGE_CONTEXT_BUNDLE_SCHEMA_VERSION,
        "policy": _policy_identity(policy),
        "retrieval_request_id": retrieval_request_id,
        "mode": mode.value,
        "as_of_time": _instant(as_of_time),
        "corpus_cutoff": _instant(corpus_cutoff),
        "corpus_version": corpus_version,
        "chunk_manifest_version": chunk_manifest_version,
        "lexical_index_version": lexical_index_version,
        "retrieved_hit_count": retrieved_hit_count,
        "selection_stop_reason": selection_stop_reason.value,
        "selected_items": tuple(_item_identity(item) for item in selected_items),
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def validate_knowledge_context_bundle(bundle: KnowledgeContextBundle) -> None:
    """Re-run every nested schema and identity guard on a supplied artifact."""
    if not isinstance(bundle, KnowledgeContextBundle):
        raise TypeError("KnowledgeContextBundle required")
    try:
        policy = replace(bundle.policy)
        historical = tuple(replace(item) for item in bundle.historical_items)
        retrospective = tuple(replace(item) for item in bundle.retrospective_items)
        canonical = replace(
            bundle,
            policy=policy,
            historical_items=historical,
            retrospective_items=retrospective,
        )
    except (TypeError, ValueError):
        raise KnowledgeContextIntegrityError(
            "knowledge context failed canonical integrity validation"
        ) from None
    if canonical != bundle:
        raise KnowledgeContextIntegrityError(
            "knowledge context failed canonical integrity validation"
        )


def _validate_stop_reason(bundle: KnowledgeContextBundle, selected_count: int) -> None:
    reason = bundle.selection_stop_reason
    if reason is KnowledgeContextStopReason.NO_HITS:
        valid = bundle.retrieved_hit_count == selected_count == 0
    elif reason is KnowledgeContextStopReason.ALL_SELECTED:
        valid = bundle.retrieved_hit_count == selected_count and selected_count > 0
    elif reason is KnowledgeContextStopReason.MAX_ITEMS:
        valid = selected_count == bundle.policy.max_items < bundle.retrieved_hit_count
    elif reason is KnowledgeContextStopReason.CHAR_BUDGET:
        valid = selected_count < bundle.retrieved_hit_count
    else:
        valid = (
            selected_count < bundle.retrieved_hit_count
            and not bundle.policy.allow_retrospective_research_context
            and bundle.mode is KnowledgeQueryMode.RESEARCH
        )
    if not valid:
        raise ValueError("knowledge context stop reason is inconsistent")


def _policy_identity(policy: KnowledgeContextPolicy) -> dict[str, object]:
    return {
        "schema_version": policy.schema_version,
        "selection_algorithm": policy.selection_algorithm,
        "selection_version": policy.selection_version,
        "max_items": policy.max_items,
        "max_context_chars": policy.max_context_chars,
        "allow_retrospective_research_context": (
            policy.allow_retrospective_research_context
        ),
    }


def _item_identity(item: KnowledgeContextItem) -> dict[str, object]:
    if not isinstance(item, KnowledgeContextItem):
        raise ValueError("invalid selected context item")
    return {
        "schema_version": item.schema_version,
        "lane": item.lane.value,
        "chunk_id": item.chunk_id,
        "document_id": item.document_id,
        "document_family_id": item.document_family_id,
        "document_version": item.document_version,
        "retrieval_rank": item.retrieval_rank,
        "retrieval_score": item.retrieval_score,
        "content": item.content,
        "content_hash": item.content_hash,
        "content_char_count": item.content_char_count,
        "source": item.source,
        "source_type": item.source_type.value,
        "title": item.title,
        "published_at": _instant(item.published_at),
        "available_at": _instant(item.available_at),
        "effective_from": _instant(item.effective_from),
        "effective_to": _instant(item.effective_to),
        "temporal_class": item.temporal_class.value,
        "entity_refs": item.entity_refs,
        "provenance_source_identifier": item.provenance_source_identifier,
        "origin_reference": item.origin_reference,
        "known_by_historical_as_of": item.known_by_historical_as_of,
        "knowledge_after_event": item.knowledge_after_event,
    }


def _digest(value: str, name: str) -> None:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be a SHA-256 digest")


def _family(value: str) -> None:
    if type(value) is not str or not _FAMILY_ID.fullmatch(value):
        raise ValueError("invalid document family ID")


def _canonical_text(value: str, name: str) -> None:
    if type(value) is not str or not value or value != unicodedata.normalize("NFC", value).strip():
        raise ValueError(f"{name} must be canonical text")


def _entity_refs(values: tuple[str, ...]) -> None:
    if type(values) is not tuple or values != tuple(sorted(set(values))):
        raise ValueError("entity references must be unique and sorted")
    if any(type(value) is not str or not _SYMBOL.fullmatch(value) for value in values):
        raise ValueError("invalid canonical entity reference")


def _instant(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None
