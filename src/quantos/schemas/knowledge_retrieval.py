"""Immutable contracts for deterministic knowledge chunking and retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import math
import re
import unicodedata

from quantos.serialization import canonical_json_bytes

from ._validation import require_aware
from .knowledge import (
    KnowledgeQueryMode,
    KnowledgeSourceType,
    KnowledgeTemporalClass,
)


KNOWLEDGE_CHUNK_SCHEMA_VERSION = "quantos-knowledge-chunk-v1"
KNOWLEDGE_CHUNK_MANIFEST_SCHEMA_VERSION = "quantos-knowledge-chunk-manifest-v1"
KNOWLEDGE_LEXICAL_INDEX_SCHEMA_VERSION = "quantos-knowledge-lexical-index-v1"
KNOWLEDGE_RETRIEVAL_REQUEST_SCHEMA_VERSION = "quantos-knowledge-retrieval-request-v1"
KNOWLEDGE_RETRIEVAL_RESULT_SCHEMA_VERSION = "quantos-knowledge-retrieval-result-v1"

CHUNKING_ALGORITHM = "paragraph_window"
CHUNKING_VERSION = "v1"
CHUNK_SIZE_UNIT = "unicode_code_points"
TARGET_CHUNK_SIZE = 800
MAX_CHUNK_SIZE = 1000
OVERLAP_SIZE = 0

TOKENIZER_NAME = "mixed_lexical"
TOKENIZER_VERSION = "v1"
RETRIEVAL_ALGORITHM = "BM25"
RETRIEVAL_VERSION = "v1"
BM25_K1 = 1.5
BM25_B = 0.75
MAX_RETRIEVAL_LIMIT = 100

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_FAMILY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SYMBOL = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


class KnowledgeRetrievalErrorCode(str, Enum):
    EMPTY_QUERY = "EMPTY_QUERY"
    EMPTY_QUERY_TOKENS = "EMPTY_QUERY_TOKENS"
    UNSUPPORTED_CHUNKING_VERSION = "UNSUPPORTED_CHUNKING_VERSION"
    UNSUPPORTED_TOKENIZER_VERSION = "UNSUPPORTED_TOKENIZER_VERSION"
    UNSUPPORTED_RETRIEVAL_VERSION = "UNSUPPORTED_RETRIEVAL_VERSION"
    STALE_INDEX = "STALE_INDEX"
    CHUNK_MANIFEST_MISMATCH = "CHUNK_MANIFEST_MISMATCH"
    TOKENIZER_MISMATCH = "TOKENIZER_MISMATCH"
    CORRUPT_INDEX = "CORRUPT_INDEX"


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    schema_version: str
    chunk_id: str
    document_id: str
    document_family_id: str
    document_version: int
    chunk_index: int
    start_offset: int
    end_offset: int
    content: str
    content_hash: str
    source: str
    title: str
    source_type: KnowledgeSourceType
    entity_refs: tuple[str, ...]
    published_at: datetime | None
    available_at: datetime
    effective_from: datetime | None
    effective_to: datetime | None
    temporal_class: KnowledgeTemporalClass
    provenance_source_identifier: str
    origin_reference: str | None
    chunking_algorithm: str = CHUNKING_ALGORITHM
    chunking_version: str = CHUNKING_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != KNOWLEDGE_CHUNK_SCHEMA_VERSION:
            raise ValueError("unsupported knowledge chunk schema")
        _digest(self.chunk_id, "chunk_id")
        _digest(self.document_id, "document_id")
        _digest(self.content_hash, "content_hash")
        _family(self.document_family_id)
        if type(self.document_version) is not int or self.document_version < 1:
            raise ValueError("document_version must be a positive integer")
        if type(self.chunk_index) is not int or self.chunk_index < 0:
            raise ValueError("chunk_index must be a non-negative integer")
        if (type(self.start_offset) is not int or type(self.end_offset) is not int
                or self.start_offset < 0 or self.end_offset <= self.start_offset):
            raise ValueError("invalid chunk offsets")
        _canonical_chunk_content(self.content)
        if self.end_offset - self.start_offset != len(self.content):
            raise ValueError("chunk offsets do not match content length")
        if self.content_hash != chunk_content_hash(self.content):
            raise ValueError("chunk content hash mismatch")
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
        if self.chunking_algorithm != CHUNKING_ALGORITHM:
            raise ValueError("unsupported chunking algorithm")
        if self.chunking_version != CHUNKING_VERSION:
            raise ValueError("unsupported chunking version")
        if self.chunk_id != knowledge_chunk_id(
            document_id=self.document_id,
            chunk_index=self.chunk_index,
            start_offset=self.start_offset,
            end_offset=self.end_offset,
            content_hash=self.content_hash,
            chunking_algorithm=self.chunking_algorithm,
            chunking_version=self.chunking_version,
        ):
            raise ValueError("knowledge chunk identity mismatch")


@dataclass(frozen=True, slots=True)
class KnowledgeChunkManifest:
    schema_version: str
    corpus_version: str
    chunking_algorithm: str
    chunking_version: str
    chunk_ids: tuple[str, ...]
    document_ids: tuple[str, ...]
    chunk_count: int
    document_count: int
    chunk_manifest_version: str
    generated_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != KNOWLEDGE_CHUNK_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported knowledge chunk manifest schema")
        for value, name in (
            (self.corpus_version, "corpus_version"),
            (self.chunk_manifest_version, "chunk_manifest_version"),
        ):
            _digest(value, name)
        if self.chunking_algorithm != CHUNKING_ALGORITHM:
            raise ValueError("unsupported chunking algorithm")
        if self.chunking_version != CHUNKING_VERSION:
            raise ValueError("unsupported chunking version")
        _sorted_digests(self.chunk_ids, "chunk_ids")
        _sorted_digests(self.document_ids, "document_ids")
        if type(self.chunk_count) is not int or self.chunk_count != len(self.chunk_ids):
            raise ValueError("chunk manifest count mismatch")
        if type(self.document_count) is not int or self.document_count != len(self.document_ids):
            raise ValueError("chunk manifest document count mismatch")
        require_aware(self.generated_at, "generated_at")
        if self.chunk_manifest_version != knowledge_chunk_manifest_version(
            corpus_version=self.corpus_version,
            chunk_ids=self.chunk_ids,
            chunking_algorithm=self.chunking_algorithm,
            chunking_version=self.chunking_version,
        ):
            raise ValueError("knowledge chunk manifest identity mismatch")


@dataclass(frozen=True, slots=True)
class KnowledgeLexicalIndexEntry:
    chunk: KnowledgeChunk
    tokens: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.chunk, KnowledgeChunk):
            raise ValueError("lexical index entry requires a knowledge chunk")
        if type(self.tokens) is not tuple:
            raise ValueError("lexical index entry tokens must be a tuple")
        if any(type(item) is not str or not item for item in self.tokens):
            raise ValueError("invalid lexical token")


@dataclass(frozen=True, slots=True)
class KnowledgeLexicalIndex:
    schema_version: str
    lexical_index_version: str
    corpus_version: str
    chunk_manifest_version: str
    tokenizer_name: str
    tokenizer_version: str
    retrieval_algorithm: str
    retrieval_version: str
    entries: tuple[KnowledgeLexicalIndexEntry, ...]
    built_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != KNOWLEDGE_LEXICAL_INDEX_SCHEMA_VERSION:
            raise ValueError("unsupported knowledge lexical index schema")
        for value, name in (
            (self.lexical_index_version, "lexical_index_version"),
            (self.corpus_version, "corpus_version"),
            (self.chunk_manifest_version, "chunk_manifest_version"),
        ):
            _digest(value, name)
        if self.tokenizer_name != TOKENIZER_NAME or self.tokenizer_version != TOKENIZER_VERSION:
            raise ValueError("unsupported tokenizer identity")
        if (self.retrieval_algorithm != RETRIEVAL_ALGORITHM
                or self.retrieval_version != RETRIEVAL_VERSION):
            raise ValueError("unsupported retrieval identity")
        require_aware(self.built_at, "built_at")
        if type(self.entries) is not tuple or any(
            not isinstance(item, KnowledgeLexicalIndexEntry) for item in self.entries
        ):
            raise ValueError("invalid lexical index entries")
        order = tuple(_chunk_order(item.chunk) for item in self.entries)
        if order != tuple(sorted(order)) or len({item.chunk.chunk_id for item in self.entries}) != len(self.entries):
            raise ValueError("lexical index entries must be unique and sorted")
        if self.lexical_index_version != knowledge_lexical_index_version(
            corpus_version=self.corpus_version,
            chunk_manifest_version=self.chunk_manifest_version,
            entries=self.entries,
            tokenizer_name=self.tokenizer_name,
            tokenizer_version=self.tokenizer_version,
            retrieval_algorithm=self.retrieval_algorithm,
            retrieval_version=self.retrieval_version,
        ):
            raise ValueError("knowledge lexical index identity mismatch")


@dataclass(frozen=True, slots=True)
class KnowledgeRetrievalRequest:
    query: str
    mode: KnowledgeQueryMode
    as_of_time: datetime
    corpus_cutoff: datetime | None = None
    entity_refs: tuple[str, ...] = ()
    source_types: tuple[KnowledgeSourceType, ...] = ()
    temporal_classes: tuple[KnowledgeTemporalClass, ...] = ()
    document_family_ids: tuple[str, ...] = ()
    effective_at: datetime | None = None
    limit: int = 10
    schema_version: str = KNOWLEDGE_RETRIEVAL_REQUEST_SCHEMA_VERSION
    tokenizer_name: str = TOKENIZER_NAME
    tokenizer_version: str = TOKENIZER_VERSION
    retrieval_algorithm: str = RETRIEVAL_ALGORITHM
    retrieval_version: str = RETRIEVAL_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != KNOWLEDGE_RETRIEVAL_REQUEST_SCHEMA_VERSION:
            raise ValueError("unsupported knowledge retrieval request schema")
        normalized = normalize_lexical_query(self.query)
        object.__setattr__(self, "query", normalized)
        if not isinstance(self.mode, KnowledgeQueryMode):
            raise ValueError("invalid knowledge query mode")
        require_aware(self.as_of_time, "as_of_time")
        if self.effective_at is not None:
            require_aware(self.effective_at, "effective_at")
        if self.mode is KnowledgeQueryMode.STRICT_LIVE:
            if self.corpus_cutoff is not None:
                raise ValueError("strict retrieval does not accept corpus_cutoff")
        else:
            if self.corpus_cutoff is None:
                raise ValueError("research retrieval requires corpus_cutoff")
            require_aware(self.corpus_cutoff, "corpus_cutoff")
            if self.as_of_time > self.corpus_cutoff:
                raise ValueError("historical as-of cannot exceed corpus cutoff")
        object.__setattr__(self, "entity_refs", _normalized_entities(self.entity_refs))
        object.__setattr__(self, "source_types", _normalized_enums(
            self.source_types, KnowledgeSourceType, "source_types",
        ))
        object.__setattr__(self, "temporal_classes", _normalized_enums(
            self.temporal_classes, KnowledgeTemporalClass, "temporal_classes",
        ))
        object.__setattr__(self, "document_family_ids", _normalized_families(
            self.document_family_ids,
        ))
        if type(self.limit) is not int or not 1 <= self.limit <= MAX_RETRIEVAL_LIMIT:
            raise ValueError("retrieval limit is outside the supported range")
        if self.tokenizer_name != TOKENIZER_NAME or self.tokenizer_version != TOKENIZER_VERSION:
            raise ValueError("unsupported tokenizer identity")
        if (self.retrieval_algorithm != RETRIEVAL_ALGORITHM
                or self.retrieval_version != RETRIEVAL_VERSION):
            raise ValueError("unsupported retrieval identity")


@dataclass(frozen=True, slots=True)
class KnowledgeRetrievalHit:
    rank: int
    chunk_id: str
    document_id: str
    document_family_id: str
    document_version: int
    chunk_index: int
    score: float
    content: str
    source: str
    title: str
    source_type: KnowledgeSourceType
    entity_refs: tuple[str, ...]
    available_at: datetime
    published_at: datetime | None
    effective_from: datetime | None
    effective_to: datetime | None
    provenance_source_identifier: str
    origin_reference: str | None
    known_by_historical_as_of: bool
    knowledge_after_event: bool

    def __post_init__(self) -> None:
        if type(self.rank) is not int or self.rank < 1:
            raise ValueError("invalid retrieval rank")
        _digest(self.chunk_id, "chunk_id")
        _digest(self.document_id, "document_id")
        _family(self.document_family_id)
        if type(self.score) is not float or not math.isfinite(self.score) or self.score <= 0:
            raise ValueError("invalid lexical retrieval score")
        if self.known_by_historical_as_of == self.knowledge_after_event:
            raise ValueError("knowledge timing flags must be complementary")


@dataclass(frozen=True, slots=True)
class KnowledgeRetrievalResult:
    schema_version: str
    retrieval_request_id: str
    query: str
    mode: KnowledgeQueryMode
    as_of_time: datetime
    corpus_cutoff: datetime | None
    corpus_version: str
    chunk_manifest_version: str
    lexical_index_version: str
    retrieval_algorithm: str
    retrieval_version: str
    tokenizer_name: str
    tokenizer_version: str
    eligible_chunk_count: int
    hits: tuple[KnowledgeRetrievalHit, ...]
    generated_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != KNOWLEDGE_RETRIEVAL_RESULT_SCHEMA_VERSION:
            raise ValueError("unsupported knowledge retrieval result schema")
        for value, name in (
            (self.retrieval_request_id, "retrieval_request_id"),
            (self.corpus_version, "corpus_version"),
            (self.chunk_manifest_version, "chunk_manifest_version"),
            (self.lexical_index_version, "lexical_index_version"),
        ):
            _digest(value, name)
        require_aware(self.generated_at, "generated_at")
        if type(self.eligible_chunk_count) is not int or self.eligible_chunk_count < len(self.hits):
            raise ValueError("invalid eligible chunk count")
        if type(self.hits) is not tuple or tuple(hit.rank for hit in self.hits) != tuple(
            range(1, len(self.hits) + 1)
        ):
            raise ValueError("retrieval hits must have contiguous ranks")


def chunk_content_hash(content: str) -> str:
    _canonical_chunk_content(content)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def knowledge_chunk_id(*, document_id: str, chunk_index: int, start_offset: int,
                       end_offset: int, content_hash: str,
                       chunking_algorithm: str = CHUNKING_ALGORITHM,
                       chunking_version: str = CHUNKING_VERSION) -> str:
    _digest(document_id, "document_id")
    _digest(content_hash, "content_hash")
    identity = {
        "schema_version": KNOWLEDGE_CHUNK_SCHEMA_VERSION,
        "document_id": document_id,
        "chunking_algorithm": chunking_algorithm,
        "chunking_version": chunking_version,
        "chunk_index": chunk_index,
        "start_offset": start_offset,
        "end_offset": end_offset,
        "content_hash": content_hash,
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def knowledge_chunk_manifest_version(*, corpus_version: str, chunk_ids: tuple[str, ...],
                                     chunking_algorithm: str = CHUNKING_ALGORITHM,
                                     chunking_version: str = CHUNKING_VERSION) -> str:
    _digest(corpus_version, "corpus_version")
    _sorted_digests(chunk_ids, "chunk_ids")
    identity = {
        "schema_version": KNOWLEDGE_CHUNK_MANIFEST_SCHEMA_VERSION,
        "corpus_version": corpus_version,
        "chunking_algorithm": chunking_algorithm,
        "chunking_version": chunking_version,
        "chunk_ids": chunk_ids,
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def knowledge_lexical_index_version(*, corpus_version: str, chunk_manifest_version: str,
                                    entries: tuple[KnowledgeLexicalIndexEntry, ...],
                                    tokenizer_name: str = TOKENIZER_NAME,
                                    tokenizer_version: str = TOKENIZER_VERSION,
                                    retrieval_algorithm: str = RETRIEVAL_ALGORITHM,
                                    retrieval_version: str = RETRIEVAL_VERSION) -> str:
    identity = {
        "schema_version": KNOWLEDGE_LEXICAL_INDEX_SCHEMA_VERSION,
        "corpus_version": corpus_version,
        "chunk_manifest_version": chunk_manifest_version,
        "tokenizer_name": tokenizer_name,
        "tokenizer_version": tokenizer_version,
        "retrieval_algorithm": retrieval_algorithm,
        "retrieval_version": retrieval_version,
        "entries": tuple((item.chunk.chunk_id, item.tokens) for item in entries),
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def normalize_lexical_query(query: str) -> str:
    if type(query) is not str:
        raise ValueError("retrieval query must be text")
    value = " ".join(unicodedata.normalize("NFC", query).split())
    if not value:
        raise ValueError("retrieval query must not be empty")
    return value


def knowledge_retrieval_request_id(request: KnowledgeRetrievalRequest, *, corpus_version: str,
                                   chunk_manifest_version: str,
                                   lexical_index_version: str) -> str:
    if not isinstance(request, KnowledgeRetrievalRequest):
        raise TypeError("knowledge retrieval request required")
    identity = {
        "schema_version": KNOWLEDGE_RETRIEVAL_REQUEST_SCHEMA_VERSION,
        "query": request.query,
        "mode": request.mode.value,
        "as_of_time": _instant(request.as_of_time),
        "corpus_cutoff": _instant(request.corpus_cutoff),
        "entity_refs": request.entity_refs,
        "source_types": tuple(item.value for item in request.source_types),
        "temporal_classes": tuple(item.value for item in request.temporal_classes),
        "document_family_ids": request.document_family_ids,
        "effective_at": _instant(request.effective_at),
        "limit": request.limit,
        "tokenizer_name": request.tokenizer_name,
        "tokenizer_version": request.tokenizer_version,
        "retrieval_algorithm": request.retrieval_algorithm,
        "retrieval_version": request.retrieval_version,
        "corpus_version": corpus_version,
        "chunk_manifest_version": chunk_manifest_version,
        "lexical_index_version": lexical_index_version,
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def _canonical_chunk_content(value: str) -> None:
    if type(value) is not str or not value or not value.strip():
        raise ValueError("chunk content must not be empty")
    if value != unicodedata.normalize("NFC", value) or "\r" in value or value.startswith("\ufeff"):
        raise ValueError("chunk content must be canonical")
    if len(value) > MAX_CHUNK_SIZE:
        raise ValueError("chunk content exceeds maximum size")


def _canonical_text(value: str, name: str) -> None:
    if type(value) is not str or not value or value != unicodedata.normalize("NFC", value).strip():
        raise ValueError(f"{name} must be canonical text")


def _digest(value: str, name: str) -> None:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be a SHA-256 digest")


def _family(value: str) -> None:
    if type(value) is not str or not _FAMILY_ID.fullmatch(value):
        raise ValueError("invalid document family ID")


def _sorted_digests(values: tuple[str, ...], name: str) -> None:
    if type(values) is not tuple or values != tuple(sorted(set(values))):
        raise ValueError(f"{name} must be unique and sorted")
    for value in values:
        _digest(value, name)


def _entity_refs(values: tuple[str, ...]) -> None:
    if type(values) is not tuple or values != tuple(sorted(set(values))):
        raise ValueError("entity references must be unique and sorted")
    if any(type(value) is not str or not _SYMBOL.fullmatch(value) for value in values):
        raise ValueError("invalid canonical entity reference")


def _normalized_entities(values: tuple[str, ...]) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise ValueError("entity_refs must be a tuple")
    _entity_refs(tuple(sorted(set(values))))
    return tuple(sorted(set(values)))


def _normalized_enums(values: tuple, enum_type, name: str) -> tuple:
    if type(values) is not tuple or any(not isinstance(item, enum_type) for item in values):
        raise ValueError(f"{name} must contain canonical enum values")
    return tuple(sorted(set(values), key=lambda item: item.value))


def _normalized_families(values: tuple[str, ...]) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise ValueError("document_family_ids must be a tuple")
    result = tuple(sorted(set(values)))
    for value in result:
        _family(value)
    return result


def _chunk_order(chunk: KnowledgeChunk):
    return chunk.document_family_id, chunk.document_version, chunk.chunk_index, chunk.chunk_id


def _instant(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None
