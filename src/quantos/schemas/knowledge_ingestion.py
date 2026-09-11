"""Immutable contracts for deterministic local knowledge ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
import hashlib
import re
import unicodedata

from ._validation import require_aware, require_non_empty
from .knowledge import (
    KnowledgeSourceType, KnowledgeTemporalClass, normalize_knowledge_content,
)


KNOWLEDGE_SOURCE_ARTIFACT_SCHEMA_VERSION = "quantos-knowledge-source-artifact-v1"
PARSED_KNOWLEDGE_DOCUMENT_SCHEMA_VERSION = "quantos-parsed-knowledge-document-v1"
KNOWLEDGE_INGESTION_RESULT_SCHEMA_VERSION = "quantos-knowledge-ingestion-result-v1"
MAX_RAW_SOURCE_BYTES = 8 * 1024 * 1024

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class KnowledgeMediaType(str, Enum):
    TEXT_PLAIN = "text/plain"
    TEXT_MARKDOWN = "text/markdown"


class KnowledgeParseWarning(str, Enum):
    """Closed, non-sensitive parser warning vocabulary."""

    UTF8_BOM_REMOVED = "UTF8_BOM_REMOVED"


class KnowledgeIngestionErrorCode(str, Enum):
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    INVALID_ENCODING = "INVALID_ENCODING"
    MALFORMED_SOURCE = "MALFORMED_SOURCE"
    EMPTY_CONTENT = "EMPTY_CONTENT"
    CONTENT_TOO_LARGE = "CONTENT_TOO_LARGE"
    INVALID_METADATA = "INVALID_METADATA"
    UNSUPPORTED_PARSER_VERSION = "UNSUPPORTED_PARSER_VERSION"
    SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
    NOT_REGULAR_FILE = "NOT_REGULAR_FILE"
    SOURCE_CHANGED_DURING_READ = "SOURCE_CHANGED_DURING_READ"
    UNKNOWN_ENTITY = "UNKNOWN_ENTITY"
    CREDENTIAL_MATERIAL = "CREDENTIAL_MATERIAL"


class KnowledgeIngestionOutcome(str, Enum):
    STORED = "STORED"
    ALREADY_PRESENT = "ALREADY_PRESENT"


@dataclass(frozen=True, slots=True)
class KnowledgeIngestionRequest:
    source_path: Path
    source_type: KnowledgeSourceType
    media_type: str
    source_identifier: str
    source_document_key: str
    source_revision: str
    document_version: int
    title: str
    temporal_class: KnowledgeTemporalClass
    published_at: datetime | None
    retrieved_at: datetime
    available_at: datetime | None
    effective_from: datetime | None
    effective_to: datetime | None
    entity_hints: tuple[str, ...]
    origin_reference: str | None = None
    parser_name: str | None = None
    parser_version: str = "v1"

    def __post_init__(self) -> None:
        if not isinstance(self.source_path, Path):
            raise ValueError("source_path must be a Path")
        if not isinstance(self.source_type, KnowledgeSourceType):
            raise ValueError("explicit knowledge source type required")
        if not isinstance(self.temporal_class, KnowledgeTemporalClass):
            raise ValueError("explicit knowledge temporal class required")
        for name in (
            "media_type", "source_identifier", "source_document_key",
            "source_revision", "title", "parser_version",
        ):
            _require_text(getattr(self, name), name)
        if self.parser_name is not None:
            _require_text(self.parser_name, "parser_name")
        if self.origin_reference is not None:
            _require_text(self.origin_reference, "origin_reference")
        if type(self.document_version) is not int or self.document_version < 1:
            raise ValueError("document_version must be a positive integer")
        require_aware(self.retrieved_at, "retrieved_at")
        for name in ("available_at", "published_at", "effective_from", "effective_to"):
            value = getattr(self, name)
            if value is not None:
                require_aware(value, name)
        if self.available_at is not None and self.available_at < self.retrieved_at:
            raise ValueError("available_at cannot precede retrieved_at")
        if (self.available_at is not None and self.published_at is not None
                and self.available_at < self.published_at):
            raise ValueError("available_at cannot precede published_at")
        if type(self.entity_hints) is not tuple:
            raise ValueError("entity_hints must be a tuple")
        if any(
            type(item) is not str or item != unicodedata.normalize("NFC", item).strip()
            or not item for item in self.entity_hints
        ):
            raise ValueError("entity_hints must contain canonical text")
        if any(type(item) is not str or not item.strip() for item in self.entity_hints):
            raise ValueError("entity hints must be non-empty strings")


@dataclass(frozen=True, slots=True)
class KnowledgeSourceArtifact:
    schema_version: str
    source_artifact_id: str
    source_type: KnowledgeSourceType
    temporal_class: KnowledgeTemporalClass
    media_type: str
    source_identifier: str
    source_document_key: str
    source_revision: str
    origin_reference: str | None
    retrieved_at: datetime
    available_at: datetime
    published_at: datetime | None
    effective_from: datetime | None
    effective_to: datetime | None
    entity_hints: tuple[str, ...]
    raw_content_hash: str
    raw_byte_size: int
    parser_name: str
    parser_version: str

    def __post_init__(self) -> None:
        if self.schema_version != KNOWLEDGE_SOURCE_ARTIFACT_SCHEMA_VERSION:
            raise ValueError("unsupported knowledge source artifact schema")
        if not _DIGEST.fullmatch(self.source_artifact_id):
            raise ValueError("source_artifact_id must be a SHA-256 digest")
        if not _DIGEST.fullmatch(self.raw_content_hash):
            raise ValueError("raw_content_hash must be a SHA-256 digest")
        if self.source_artifact_id != knowledge_source_artifact_id(
            source_identifier=self.source_identifier,
            source_document_key=self.source_document_key,
            source_revision=self.source_revision,
            raw_hash=self.raw_content_hash,
        ):
            raise ValueError("knowledge source artifact identity mismatch")
        if type(self.raw_byte_size) is not int or not 0 <= self.raw_byte_size <= MAX_RAW_SOURCE_BYTES:
            raise ValueError("raw source byte size is invalid")
        if not isinstance(self.source_type, KnowledgeSourceType):
            raise ValueError("explicit knowledge source type required")
        if not isinstance(self.temporal_class, KnowledgeTemporalClass):
            raise ValueError("explicit knowledge temporal class required")
        for name in (
            "media_type", "source_identifier", "source_document_key",
            "source_revision", "parser_name", "parser_version",
        ):
            _require_canonical_text(getattr(self, name), name)
        if self.origin_reference is not None:
            _require_canonical_text(self.origin_reference, "origin_reference")
        require_aware(self.retrieved_at, "retrieved_at")
        require_aware(self.available_at, "available_at")
        for name in ("published_at", "effective_from", "effective_to"):
            value = getattr(self, name)
            if value is not None:
                require_aware(value, name)
        if self.available_at < self.retrieved_at:
            raise ValueError("available_at cannot precede retrieved_at")
        if self.published_at is not None and self.available_at < self.published_at:
            raise ValueError("available_at cannot precede published_at")
        if type(self.entity_hints) is not tuple:
            raise ValueError("entity_hints must be a tuple")


@dataclass(frozen=True, slots=True)
class ParsedKnowledgeDocument:
    schema_version: str
    source_artifact_id: str
    raw_content_hash: str
    parser_name: str
    parser_version: str
    content: str
    title_hint: str
    entity_hints: tuple[str, ...]
    parse_warnings: tuple[KnowledgeParseWarning, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != PARSED_KNOWLEDGE_DOCUMENT_SCHEMA_VERSION:
            raise ValueError("unsupported parsed knowledge document schema")
        for value, name in (
            (self.source_artifact_id, "source_artifact_id"),
            (self.raw_content_hash, "raw_content_hash"),
        ):
            if not _DIGEST.fullmatch(value):
                raise ValueError(f"{name} must be a SHA-256 digest")
        _require_canonical_text(self.parser_name, "parser_name")
        _require_canonical_text(self.parser_version, "parser_version")
        if normalize_knowledge_content(self.content) != self.content:
            raise ValueError("parsed knowledge content is not canonical")
        _require_canonical_text(self.title_hint, "title_hint")
        if type(self.entity_hints) is not tuple:
            raise ValueError("entity_hints must be a tuple")
        if any(
            type(item) is not str or item != unicodedata.normalize("NFC", item).strip()
            or not item for item in self.entity_hints
        ):
            raise ValueError("entity_hints must contain canonical text")
        if type(self.parse_warnings) is not tuple or any(
            not isinstance(item, KnowledgeParseWarning) for item in self.parse_warnings
        ):
            raise ValueError("parse_warnings must use the controlled vocabulary")


@dataclass(frozen=True, slots=True)
class KnowledgeIngestionResult:
    schema_version: str
    source_artifact_id: str
    raw_content_hash: str
    parser_name: str
    parser_version: str
    document_family_id: str
    document_version: int
    document_id: str
    content_hash: str
    repository_ref: str
    entity_refs: tuple[str, ...]
    outcome: KnowledgeIngestionOutcome
    warnings: tuple[KnowledgeParseWarning, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != KNOWLEDGE_INGESTION_RESULT_SCHEMA_VERSION:
            raise ValueError("unsupported knowledge ingestion result schema")
        for value, name in (
            (self.source_artifact_id, "source_artifact_id"),
            (self.raw_content_hash, "raw_content_hash"),
            (self.document_id, "document_id"),
            (self.content_hash, "content_hash"),
        ):
            if not _DIGEST.fullmatch(value):
                raise ValueError(f"{name} must be a SHA-256 digest")
        if type(self.document_version) is not int or self.document_version < 1:
            raise ValueError("document_version must be a positive integer")
        for name in ("parser_name", "parser_version", "document_family_id", "repository_ref"):
            _require_canonical_text(getattr(self, name), name)
        if type(self.entity_refs) is not tuple:
            raise ValueError("entity_refs must be a tuple")
        if not isinstance(self.outcome, KnowledgeIngestionOutcome):
            raise ValueError("explicit ingestion outcome required")
        if type(self.warnings) is not tuple or any(
            not isinstance(item, KnowledgeParseWarning) for item in self.warnings
        ):
            raise ValueError("warnings must use the controlled vocabulary")


def knowledge_source_artifact_id(
    *, source_identifier: str, source_document_key: str,
    source_revision: str, raw_hash: str,
) -> str:
    for value, name in (
        (source_identifier, "source_identifier"),
        (source_document_key, "source_document_key"),
        (source_revision, "source_revision"),
    ):
        _require_canonical_text(value, name)
    if type(raw_hash) is not str or not _DIGEST.fullmatch(raw_hash):
        raise ValueError("raw_hash must be a SHA-256 digest")
    from quantos.serialization import canonical_json_bytes

    identity = {
        "schema_version": KNOWLEDGE_SOURCE_ARTIFACT_SCHEMA_VERSION,
        "source_identifier": source_identifier,
        "source_document_key": source_document_key,
        "source_revision": source_revision,
        "raw_content_hash": raw_hash,
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def _require_text(value: str, name: str) -> None:
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")
    require_non_empty(value, name)
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value):
        raise ValueError(f"{name} contains control characters")


def _require_canonical_text(value: str, name: str) -> None:
    _require_text(value, name)
    if value != unicodedata.normalize("NFC", value).strip():
        raise ValueError(f"{name} is not canonical")
