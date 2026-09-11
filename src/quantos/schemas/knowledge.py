"""Canonical, immutable financial-knowledge contracts for Phase 4A."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import re
import unicodedata

from ._validation import require_aware, require_non_empty


KNOWLEDGE_DOCUMENT_SCHEMA_VERSION = "quantos-knowledge-document-v1"
KNOWLEDGE_CORPUS_SCHEMA_VERSION = "quantos-knowledge-corpus-v1"
MAX_CANONICAL_DOCUMENT_BYTES = 1024 * 1024

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_FAMILY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SYMBOL = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_SECRET_PATTERNS = (
    re.compile(r"authorization\s*:\s*(?:bearer|basic)\s+\S+", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret)\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{16,}",
        re.IGNORECASE,
    ),
)


class KnowledgeSourceType(str, Enum):
    COMPANY_PROFILE = "COMPANY_PROFILE"
    ANNUAL_REPORT = "ANNUAL_REPORT"
    RESEARCH_REPORT = "RESEARCH_REPORT"
    POLICY_DOCUMENT = "POLICY_DOCUMENT"
    INDUSTRY_WHITEPAPER = "INDUSTRY_WHITEPAPER"
    ACADEMIC_PAPER = "ACADEMIC_PAPER"
    REFERENCE_DOCUMENT = "REFERENCE_DOCUMENT"


class KnowledgeTemporalClass(str, Enum):
    TIMELESS = "TIMELESS"
    SEMI_STATIC = "SEMI_STATIC"
    TIME_BOUNDED = "TIME_BOUNDED"


class KnowledgeQueryMode(str, Enum):
    """One-to-one vocabulary alignment with the existing RunContext modes."""

    RESEARCH = "research"
    STRICT_LIVE = "strict_live"


@dataclass(frozen=True, slots=True)
class KnowledgeProvenance:
    source_identifier: str
    ingestion_adapter: str
    ingestion_adapter_version: str
    origin_reference: str | None = None

    def __post_init__(self) -> None:
        for name in ("source_identifier", "ingestion_adapter", "ingestion_adapter_version"):
            _validate_scalar(getattr(self, name), name, canonical=True)
        if self.origin_reference is not None:
            _validate_scalar(self.origin_reference, "origin_reference", canonical=True)


@dataclass(frozen=True, slots=True)
class KnowledgeDocumentInput:
    """Pre-normalized caller input; identity fields are intentionally absent."""

    document_family_id: str
    document_version: int
    source: str
    source_type: KnowledgeSourceType
    title: str
    content: str
    temporal_class: KnowledgeTemporalClass
    published_at: datetime | None
    available_at: datetime
    retrieved_at: datetime
    effective_from: datetime | None
    effective_to: datetime | None
    entity_refs: tuple[str, ...]
    provenance: KnowledgeProvenance

    def __post_init__(self) -> None:
        _validate_input(self)


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    schema_version: str
    document_id: str
    document_family_id: str
    document_version: int
    source: str
    source_type: KnowledgeSourceType
    title: str
    content: str
    content_hash: str
    temporal_class: KnowledgeTemporalClass
    published_at: datetime | None
    available_at: datetime
    retrieved_at: datetime
    effective_from: datetime | None
    effective_to: datetime | None
    entity_refs: tuple[str, ...]
    provenance: KnowledgeProvenance

    def __post_init__(self) -> None:
        if self.schema_version != KNOWLEDGE_DOCUMENT_SCHEMA_VERSION:
            raise ValueError("unsupported knowledge document schema")
        _validate_digest(self.document_id, "document_id")
        _validate_digest(self.content_hash, "content_hash")
        _validate_common(
            document_family_id=self.document_family_id,
            document_version=self.document_version,
            source=self.source,
            source_type=self.source_type,
            title=self.title,
            content=self.content,
            temporal_class=self.temporal_class,
            published_at=self.published_at,
            available_at=self.available_at,
            retrieved_at=self.retrieved_at,
            effective_from=self.effective_from,
            effective_to=self.effective_to,
            entity_refs=self.entity_refs,
            provenance=self.provenance,
            require_canonical=True,
        )
        if self.content_hash != knowledge_content_hash(self.content):
            raise ValueError("knowledge content hash mismatch")
        if self.document_id != knowledge_document_id(
            document_family_id=self.document_family_id,
            document_version=self.document_version,
            content_hash=self.content_hash,
        ):
            raise ValueError("knowledge document identity mismatch")


@dataclass(frozen=True, slots=True)
class KnowledgeResearchView:
    document: KnowledgeDocument
    corpus_cutoff: datetime
    historical_as_of: datetime
    known_by_historical_as_of: bool
    knowledge_after_event: bool
    mode: KnowledgeQueryMode = KnowledgeQueryMode.RESEARCH

    def __post_init__(self) -> None:
        if not isinstance(self.document, KnowledgeDocument):
            raise ValueError("research view requires a canonical document")
        require_aware(self.corpus_cutoff, "corpus_cutoff")
        require_aware(self.historical_as_of, "historical_as_of")
        if self.historical_as_of > self.corpus_cutoff:
            raise ValueError("historical_as_of cannot exceed corpus_cutoff")
        if self.document.available_at > self.corpus_cutoff:
            raise ValueError("research document is outside the readable corpus")
        expected_known = self.document.available_at <= self.historical_as_of
        if self.known_by_historical_as_of != expected_known:
            raise ValueError("research historical-knowledge flag mismatch")
        if self.knowledge_after_event != (not expected_known):
            raise ValueError("research retrospective flag mismatch")
        if self.mode != KnowledgeQueryMode.RESEARCH:
            raise ValueError("research view mode must be research")


@dataclass(frozen=True, slots=True)
class KnowledgeCorpusManifest:
    schema_version: str
    corpus_version: str
    document_ids: tuple[str, ...]
    document_family_ids: tuple[str, ...]
    document_count: int
    document_family_count: int
    generated_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != KNOWLEDGE_CORPUS_SCHEMA_VERSION:
            raise ValueError("unsupported knowledge corpus schema")
        _validate_digest(self.corpus_version, "corpus_version")
        require_aware(self.generated_at, "generated_at")
        if tuple(sorted(set(self.document_ids))) != self.document_ids:
            raise ValueError("manifest document_ids must be unique and sorted")
        if tuple(sorted(set(self.document_family_ids))) != self.document_family_ids:
            raise ValueError("manifest family IDs must be unique and sorted")
        if any(not _DIGEST.fullmatch(value) for value in self.document_ids):
            raise ValueError("manifest contains an invalid document ID")
        if any(not _FAMILY_ID.fullmatch(value) for value in self.document_family_ids):
            raise ValueError("manifest contains an invalid family ID")
        if type(self.document_count) is not int or self.document_count != len(self.document_ids):
            raise ValueError("manifest document count mismatch")
        if (type(self.document_family_count) is not int
                or self.document_family_count != len(self.document_family_ids)):
            raise ValueError("manifest family count mismatch")
        if self.document_family_count > self.document_count:
            raise ValueError("manifest family count cannot exceed document count")
        if self.corpus_version != knowledge_corpus_version(self.document_ids):
            raise ValueError("knowledge corpus identity mismatch")


def canonicalize_knowledge_document(value: KnowledgeDocumentInput) -> KnowledgeDocument:
    if not isinstance(value, KnowledgeDocumentInput):
        raise TypeError("canonicalization requires KnowledgeDocumentInput")
    content = normalize_knowledge_content(value.content)
    family_id = _normalize_scalar(value.document_family_id)
    source = _normalize_scalar(value.source)
    title = _normalize_scalar(value.title)
    provenance = KnowledgeProvenance(
        source_identifier=_normalize_scalar(value.provenance.source_identifier),
        ingestion_adapter=_normalize_scalar(value.provenance.ingestion_adapter),
        ingestion_adapter_version=_normalize_scalar(value.provenance.ingestion_adapter_version),
        origin_reference=(
            _normalize_scalar(value.provenance.origin_reference)
            if value.provenance.origin_reference is not None else None
        ),
    )
    refs = tuple(sorted(value.entity_refs))
    content_hash = knowledge_content_hash(content)
    document_id = knowledge_document_id(
        document_family_id=family_id,
        document_version=value.document_version,
        content_hash=content_hash,
    )
    return KnowledgeDocument(
        schema_version=KNOWLEDGE_DOCUMENT_SCHEMA_VERSION,
        document_id=document_id,
        document_family_id=family_id,
        document_version=value.document_version,
        source=source,
        source_type=value.source_type,
        title=title,
        content=content,
        content_hash=content_hash,
        temporal_class=value.temporal_class,
        published_at=value.published_at,
        available_at=value.available_at,
        retrieved_at=value.retrieved_at,
        effective_from=value.effective_from,
        effective_to=value.effective_to,
        entity_refs=refs,
        provenance=provenance,
    )


def normalize_knowledge_content(value: str) -> str:
    if type(value) is not str:
        raise ValueError("knowledge content must be text")
    without_bom = value.lstrip("\ufeff")
    normalized_newlines = without_bom.replace("\r\n", "\n").replace("\r", "\n")
    normalized = unicodedata.normalize("NFC", normalized_newlines)
    if not normalized.strip():
        raise ValueError("knowledge content must not be empty")
    if any(_is_forbidden_control(character) for character in normalized):
        raise ValueError("knowledge content contains control characters")
    if len(normalized.encode("utf-8")) > MAX_CANONICAL_DOCUMENT_BYTES:
        raise ValueError("knowledge content exceeds the canonical size limit")
    _reject_secret_material(normalized)
    return normalized


def knowledge_content_hash(content: str) -> str:
    canonical = normalize_knowledge_content(content)
    if canonical != content:
        raise ValueError("content must be canonical before hashing")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def knowledge_document_id(*, document_family_id: str, document_version: int,
                          content_hash: str) -> str:
    _validate_family_id(document_family_id)
    _validate_version(document_version)
    _validate_digest(content_hash, "content_hash")
    from quantos.serialization import canonical_json_bytes

    identity = {
        "schema_version": KNOWLEDGE_DOCUMENT_SCHEMA_VERSION,
        "document_family_id": document_family_id,
        "document_version": document_version,
        "content_hash": content_hash,
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def knowledge_corpus_version(document_ids: tuple[str, ...]) -> str:
    if type(document_ids) is not tuple or tuple(sorted(set(document_ids))) != document_ids:
        raise ValueError("corpus document IDs must be a unique sorted tuple")
    if any(not _DIGEST.fullmatch(value) for value in document_ids):
        raise ValueError("corpus contains an invalid document ID")
    from quantos.serialization import canonical_json_bytes

    identity = {
        "schema_version": KNOWLEDGE_CORPUS_SCHEMA_VERSION,
        "document_ids": document_ids,
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def build_knowledge_corpus_manifest(
    documents: tuple[KnowledgeDocument, ...], *, generated_at: datetime,
) -> KnowledgeCorpusManifest:
    if type(documents) is not tuple or any(not isinstance(item, KnowledgeDocument) for item in documents):
        raise ValueError("manifest requires canonical knowledge documents")
    require_aware(generated_at, "generated_at")
    document_ids = tuple(sorted(item.document_id for item in documents))
    if len(document_ids) != len(set(document_ids)):
        raise ValueError("manifest cannot contain duplicate documents")
    family_ids = tuple(sorted({item.document_family_id for item in documents}))
    return KnowledgeCorpusManifest(
        schema_version=KNOWLEDGE_CORPUS_SCHEMA_VERSION,
        corpus_version=knowledge_corpus_version(document_ids),
        document_ids=document_ids,
        document_family_ids=family_ids,
        document_count=len(document_ids),
        document_family_count=len(family_ids),
        generated_at=generated_at,
    )


def _validate_input(value: KnowledgeDocumentInput) -> None:
    _validate_common(
        document_family_id=value.document_family_id,
        document_version=value.document_version,
        source=value.source,
        source_type=value.source_type,
        title=value.title,
        content=value.content,
        temporal_class=value.temporal_class,
        published_at=value.published_at,
        available_at=value.available_at,
        retrieved_at=value.retrieved_at,
        effective_from=value.effective_from,
        effective_to=value.effective_to,
        entity_refs=value.entity_refs,
        provenance=value.provenance,
        require_canonical=False,
    )


def _validate_common(*, document_family_id, document_version, source, source_type,
                     title, content, temporal_class, published_at, available_at,
                     retrieved_at, effective_from, effective_to, entity_refs,
                     provenance, require_canonical: bool) -> None:
    _validate_family_id(document_family_id)
    _validate_version(document_version)
    _validate_scalar(source, "source", canonical=require_canonical)
    _validate_scalar(title, "title", canonical=require_canonical)
    if not isinstance(source_type, KnowledgeSourceType):
        raise ValueError("explicit knowledge source type required")
    if not isinstance(temporal_class, KnowledgeTemporalClass):
        raise ValueError("explicit knowledge temporal class required")
    normalized_content = normalize_knowledge_content(content)
    if require_canonical and normalized_content != content:
        raise ValueError("knowledge content is not canonical")
    for name, timestamp in (
        ("published_at", published_at), ("effective_from", effective_from),
        ("effective_to", effective_to),
    ):
        if timestamp is not None:
            require_aware(timestamp, name)
    require_aware(available_at, "available_at")
    require_aware(retrieved_at, "retrieved_at")
    if published_at is not None and available_at < published_at:
        raise ValueError("available_at cannot precede published_at")
    if available_at < retrieved_at:
        raise ValueError("available_at cannot precede retrieved_at")
    if effective_from is not None and effective_to is not None and effective_from > effective_to:
        raise ValueError("effective_from cannot exceed effective_to")
    if temporal_class == KnowledgeTemporalClass.TIMELESS:
        if effective_from is not None or effective_to is not None:
            raise ValueError("TIMELESS knowledge cannot have effective boundaries")
    elif temporal_class == KnowledgeTemporalClass.SEMI_STATIC:
        if effective_from is None:
            raise ValueError("SEMI_STATIC knowledge requires effective_from")
    elif effective_from is None or effective_to is None:
        raise ValueError("TIME_BOUNDED knowledge requires both effective boundaries")
    if type(entity_refs) is not tuple:
        raise ValueError("entity_refs must be a tuple")
    if len(entity_refs) != len(set(entity_refs)):
        raise ValueError("duplicate entity reference")
    if any(type(item) is not str or not _SYMBOL.fullmatch(item) for item in entity_refs):
        raise ValueError("entity refs must use canonical security symbols")
    if require_canonical and tuple(sorted(entity_refs)) != entity_refs:
        raise ValueError("canonical entity refs must be sorted")
    if not isinstance(provenance, KnowledgeProvenance):
        raise ValueError("typed knowledge provenance required")


def _normalize_scalar(value: str) -> str:
    if type(value) is not str:
        raise ValueError("canonical text field must be a string")
    return unicodedata.normalize("NFC", value).strip()


def _validate_scalar(value: str, field_name: str, *, canonical: bool) -> None:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a string")
    require_non_empty(value, field_name)
    normalized = _normalize_scalar(value)
    if canonical and value != normalized:
        raise ValueError(f"{field_name} is not canonical")
    if any(_is_forbidden_control(character) for character in normalized):
        raise ValueError(f"{field_name} contains control characters")
    _reject_secret_material(normalized)


def _validate_family_id(value: str) -> None:
    if type(value) is not str or not _FAMILY_ID.fullmatch(value):
        raise ValueError("document_family_id is invalid")
    _reject_secret_material(value)


def _validate_version(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("document_version must be a positive integer")


def _validate_digest(value: str, field_name: str) -> None:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ValueError(f"{field_name} must be a SHA-256 digest")


def _is_forbidden_control(value: str) -> bool:
    codepoint = ord(value)
    return (codepoint < 32 and value not in {"\n", "\t"}) or 127 <= codepoint <= 159


def _reject_secret_material(value: str) -> None:
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise ValueError("knowledge data contains credential material")
