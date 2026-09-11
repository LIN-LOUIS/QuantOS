"""Deterministic, offline ingestion of bounded local knowledge documents."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import unicodedata

from quantos.serialization import canonical_json_bytes
from quantos.schemas.knowledge import (
    KnowledgeDocument,
    KnowledgeDocumentInput,
    KnowledgeProvenance,
    KnowledgeSourceType,
    canonicalize_knowledge_document,
    normalize_knowledge_content,
)
from quantos.schemas.knowledge_ingestion import (
    KNOWLEDGE_INGESTION_RESULT_SCHEMA_VERSION,
    KNOWLEDGE_SOURCE_ARTIFACT_SCHEMA_VERSION,
    MAX_RAW_SOURCE_BYTES,
    PARSED_KNOWLEDGE_DOCUMENT_SCHEMA_VERSION,
    KnowledgeIngestionErrorCode,
    KnowledgeIngestionOutcome,
    KnowledgeIngestionRequest,
    KnowledgeIngestionResult,
    KnowledgeMediaType,
    KnowledgeParseWarning,
    KnowledgeSourceArtifact,
    ParsedKnowledgeDocument,
    knowledge_source_artifact_id,
)
from quantos.storage.knowledge import KnowledgeRepository


_SYMBOL = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_PARSERS = {
    KnowledgeMediaType.TEXT_PLAIN.value: ("plain_text", "v1"),
    KnowledgeMediaType.TEXT_MARKDOWN.value: ("markdown", "v1"),
}
_ENTITY_REQUIRED = {
    KnowledgeSourceType.COMPANY_PROFILE,
    KnowledgeSourceType.ANNUAL_REPORT,
}
_SAFE_ERRORS = {
    KnowledgeIngestionErrorCode.UNSUPPORTED_MEDIA_TYPE: "knowledge media type is unsupported",
    KnowledgeIngestionErrorCode.INVALID_ENCODING: "knowledge source is not valid UTF-8 text",
    KnowledgeIngestionErrorCode.MALFORMED_SOURCE: "knowledge source could not be read safely",
    KnowledgeIngestionErrorCode.EMPTY_CONTENT: "parsed knowledge content is empty",
    KnowledgeIngestionErrorCode.CONTENT_TOO_LARGE: "parsed knowledge content exceeds its size limit",
    KnowledgeIngestionErrorCode.INVALID_METADATA: "knowledge ingestion metadata is invalid",
    KnowledgeIngestionErrorCode.UNSUPPORTED_PARSER_VERSION: "knowledge parser version is unsupported",
    KnowledgeIngestionErrorCode.SOURCE_TOO_LARGE: "raw knowledge source exceeds its size limit",
    KnowledgeIngestionErrorCode.NOT_REGULAR_FILE: "knowledge source must be a regular non-symlink file",
    KnowledgeIngestionErrorCode.SOURCE_CHANGED_DURING_READ: "knowledge source changed during bounded read",
    KnowledgeIngestionErrorCode.UNKNOWN_ENTITY: "knowledge entity hint is not a canonical security symbol",
    KnowledgeIngestionErrorCode.CREDENTIAL_MATERIAL: "knowledge source contains credential material",
}


class KnowledgeIngestionError(ValueError):
    """Controlled ingestion failure that never includes raw source material."""

    def __init__(
        self, code: KnowledgeIngestionErrorCode, *, source_artifact_id: str | None = None,
        parser_name: str | None = None, parser_version: str | None = None,
    ) -> None:
        self.code = code
        self.source_artifact_id = source_artifact_id
        self.parser_name = parser_name
        self.parser_version = parser_version
        super().__init__(_SAFE_ERRORS[code])


class KnowledgeParseError(KnowledgeIngestionError):
    pass


def raw_content_hash(raw_bytes: bytes) -> str:
    if type(raw_bytes) is not bytes:
        raise TypeError("raw knowledge content must be bytes")
    return hashlib.sha256(raw_bytes).hexdigest()


def resolve_document_family_id(
    *, source_type: KnowledgeSourceType, source_identifier: str, source_document_key: str,
) -> str:
    if not isinstance(source_type, KnowledgeSourceType):
        raise ValueError("explicit knowledge source type required")
    source_identifier = _canonical_text(source_identifier)
    source_document_key = _canonical_text(source_document_key)
    if not source_identifier or not source_document_key:
        raise ValueError("source family metadata must not be empty")
    identity = {
        "schema_version": "quantos-knowledge-family-v1",
        "source_type": source_type.value,
        "source_identifier": source_identifier,
        "source_document_key": source_document_key,
    }
    digest = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    return f"knowledge:{source_type.value.lower()}:{digest}"


def resolve_entity_refs(
    entity_hints: tuple[str, ...], *, source_type: KnowledgeSourceType,
) -> tuple[str, ...]:
    if type(entity_hints) is not tuple:
        raise KnowledgeIngestionError(KnowledgeIngestionErrorCode.INVALID_METADATA)
    normalized = tuple(_canonical_text(item) for item in entity_hints)
    if any(not _SYMBOL.fullmatch(item) for item in normalized):
        raise KnowledgeIngestionError(KnowledgeIngestionErrorCode.UNKNOWN_ENTITY)
    resolved = tuple(sorted(set(normalized)))
    if source_type in _ENTITY_REQUIRED and not resolved:
        raise KnowledgeIngestionError(KnowledgeIngestionErrorCode.INVALID_METADATA)
    return resolved


def read_knowledge_source_artifact(
    request: KnowledgeIngestionRequest,
) -> tuple[KnowledgeSourceArtifact, bytes]:
    if not isinstance(request, KnowledgeIngestionRequest):
        raise TypeError("KnowledgeIngestionRequest required")
    parser_name, parser_version = _resolve_parser(request)
    raw_bytes = _bounded_regular_file_read(request.source_path)
    digest = raw_content_hash(raw_bytes)
    source_identifier = _canonical_text(request.source_identifier)
    source_document_key = _canonical_text(request.source_document_key)
    source_revision = _canonical_text(request.source_revision)
    artifact_id = knowledge_source_artifact_id(
        source_identifier=source_identifier,
        source_document_key=source_document_key,
        source_revision=source_revision,
        raw_hash=digest,
    )
    try:
        artifact = KnowledgeSourceArtifact(
            schema_version=KNOWLEDGE_SOURCE_ARTIFACT_SCHEMA_VERSION,
            source_artifact_id=artifact_id,
            source_type=request.source_type,
            temporal_class=request.temporal_class,
            media_type=_canonical_media_type(request.media_type),
            source_identifier=source_identifier,
            source_document_key=source_document_key,
            source_revision=source_revision,
            origin_reference=(
                _canonical_text(request.origin_reference)
                if request.origin_reference is not None else None
            ),
            retrieved_at=request.retrieved_at,
            available_at=request.available_at or request.retrieved_at,
            published_at=request.published_at,
            effective_from=request.effective_from,
            effective_to=request.effective_to,
            entity_hints=tuple(_canonical_text(item) for item in request.entity_hints),
            raw_content_hash=digest,
            raw_byte_size=len(raw_bytes),
            parser_name=parser_name,
            parser_version=parser_version,
        )
    except ValueError:
        raise KnowledgeIngestionError(
            KnowledgeIngestionErrorCode.INVALID_METADATA,
            source_artifact_id=artifact_id,
            parser_name=parser_name,
            parser_version=parser_version,
        ) from None
    expected_id = knowledge_source_artifact_id(
        source_identifier=artifact.source_identifier,
        source_document_key=artifact.source_document_key,
        source_revision=artifact.source_revision,
        raw_hash=artifact.raw_content_hash,
    )
    if artifact.source_artifact_id != expected_id:
        raise KnowledgeIngestionError(KnowledgeIngestionErrorCode.INVALID_METADATA)
    return artifact, raw_bytes


def parse_knowledge_source(
    artifact: KnowledgeSourceArtifact, raw_bytes: bytes, *, title: str,
) -> ParsedKnowledgeDocument:
    if not isinstance(artifact, KnowledgeSourceArtifact) or type(raw_bytes) is not bytes:
        raise TypeError("canonical source artifact and raw bytes required")
    if len(raw_bytes) != artifact.raw_byte_size or raw_content_hash(raw_bytes) != artifact.raw_content_hash:
        raise KnowledgeParseError(
            KnowledgeIngestionErrorCode.MALFORMED_SOURCE,
            source_artifact_id=artifact.source_artifact_id,
            parser_name=artifact.parser_name,
            parser_version=artifact.parser_version,
        )
    expected_parser = _PARSERS.get(artifact.media_type)
    if expected_parser is None:
        raise _parse_error(KnowledgeIngestionErrorCode.UNSUPPORTED_MEDIA_TYPE, artifact)
    if artifact.parser_version != expected_parser[1]:
        raise _parse_error(KnowledgeIngestionErrorCode.UNSUPPORTED_PARSER_VERSION, artifact)
    if artifact.parser_name != expected_parser[0]:
        raise _parse_error(KnowledgeIngestionErrorCode.INVALID_METADATA, artifact)
    if b"\x00" in raw_bytes:
        raise _parse_error(KnowledgeIngestionErrorCode.INVALID_ENCODING, artifact)
    try:
        decoded = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _parse_error(KnowledgeIngestionErrorCode.INVALID_ENCODING, artifact) from None
    try:
        content = normalize_knowledge_content(decoded)
    except ValueError as exc:
        message = str(exc)
        if "must not be empty" in message:
            code = KnowledgeIngestionErrorCode.EMPTY_CONTENT
        elif "size limit" in message:
            code = KnowledgeIngestionErrorCode.CONTENT_TOO_LARGE
        elif "credential material" in message:
            code = KnowledgeIngestionErrorCode.CREDENTIAL_MATERIAL
        elif "control characters" in message:
            code = KnowledgeIngestionErrorCode.INVALID_ENCODING
        else:
            code = KnowledgeIngestionErrorCode.MALFORMED_SOURCE
        raise _parse_error(code, artifact) from None
    title_hint = _canonical_text(title)
    if not title_hint:
        raise _parse_error(KnowledgeIngestionErrorCode.INVALID_METADATA, artifact)
    warnings = (
        (KnowledgeParseWarning.UTF8_BOM_REMOVED,)
        if decoded.startswith("\ufeff") else ()
    )
    return ParsedKnowledgeDocument(
        schema_version=PARSED_KNOWLEDGE_DOCUMENT_SCHEMA_VERSION,
        source_artifact_id=artifact.source_artifact_id,
        raw_content_hash=artifact.raw_content_hash,
        parser_name=artifact.parser_name,
        parser_version=artifact.parser_version,
        content=content,
        title_hint=title_hint,
        entity_hints=artifact.entity_hints,
        parse_warnings=warnings,
    )


def build_ingested_knowledge_document(
    request: KnowledgeIngestionRequest,
    artifact: KnowledgeSourceArtifact,
    parsed: ParsedKnowledgeDocument,
) -> KnowledgeDocument:
    if (
        parsed.source_artifact_id != artifact.source_artifact_id
        or parsed.raw_content_hash != artifact.raw_content_hash
        or parsed.parser_name != artifact.parser_name
        or parsed.parser_version != artifact.parser_version
    ):
        raise KnowledgeIngestionError(KnowledgeIngestionErrorCode.INVALID_METADATA)
    entity_refs = resolve_entity_refs(parsed.entity_hints, source_type=artifact.source_type)
    family_id = resolve_document_family_id(
        source_type=artifact.source_type,
        source_identifier=artifact.source_identifier,
        source_document_key=artifact.source_document_key,
    )
    try:
        value = KnowledgeDocumentInput(
            document_family_id=family_id,
            document_version=request.document_version,
            source=artifact.source_identifier,
            source_type=artifact.source_type,
            title=parsed.title_hint,
            content=parsed.content,
            temporal_class=artifact.temporal_class,
            published_at=artifact.published_at,
            available_at=artifact.available_at,
            retrieved_at=artifact.retrieved_at,
            effective_from=artifact.effective_from,
            effective_to=artifact.effective_to,
            entity_refs=entity_refs,
            provenance=KnowledgeProvenance(
                source_identifier=artifact.source_artifact_id,
                ingestion_adapter=artifact.parser_name,
                ingestion_adapter_version=artifact.parser_version,
                origin_reference=artifact.origin_reference,
            ),
        )
        return canonicalize_knowledge_document(value)
    except ValueError:
        raise KnowledgeIngestionError(
            KnowledgeIngestionErrorCode.INVALID_METADATA,
            source_artifact_id=artifact.source_artifact_id,
            parser_name=artifact.parser_name,
            parser_version=artifact.parser_version,
        ) from None


def ingest_knowledge_document(
    request: KnowledgeIngestionRequest, *, repository: KnowledgeRepository,
) -> KnowledgeIngestionResult:
    artifact, raw_bytes = read_knowledge_source_artifact(request)
    parsed = parse_knowledge_source(artifact, raw_bytes, title=request.title)
    document = build_ingested_knowledge_document(request, artifact, parsed)
    existing_ids = {item.document_id for item in repository.list_documents()}
    path = repository.store(document)
    outcome = (
        KnowledgeIngestionOutcome.ALREADY_PRESENT
        if document.document_id in existing_ids else KnowledgeIngestionOutcome.STORED
    )
    return KnowledgeIngestionResult(
        schema_version=KNOWLEDGE_INGESTION_RESULT_SCHEMA_VERSION,
        source_artifact_id=artifact.source_artifact_id,
        raw_content_hash=artifact.raw_content_hash,
        parser_name=artifact.parser_name,
        parser_version=artifact.parser_version,
        document_family_id=document.document_family_id,
        document_version=document.document_version,
        document_id=document.document_id,
        content_hash=document.content_hash,
        repository_ref=path.relative_to(repository.root).as_posix(),
        entity_refs=document.entity_refs,
        outcome=outcome,
        warnings=parsed.parse_warnings,
    )


def _resolve_parser(request: KnowledgeIngestionRequest) -> tuple[str, str]:
    media_type = _canonical_media_type(request.media_type)
    expected = _PARSERS.get(media_type)
    if expected is None:
        raise KnowledgeParseError(KnowledgeIngestionErrorCode.UNSUPPORTED_MEDIA_TYPE)
    parser_name = _canonical_text(request.parser_name) if request.parser_name is not None else expected[0]
    parser_version = _canonical_text(request.parser_version)
    if parser_version != expected[1]:
        raise KnowledgeParseError(
            KnowledgeIngestionErrorCode.UNSUPPORTED_PARSER_VERSION,
            parser_name=parser_name, parser_version=parser_version,
        )
    if parser_name != expected[0]:
        raise KnowledgeParseError(
            KnowledgeIngestionErrorCode.INVALID_METADATA,
            parser_name=parser_name, parser_version=parser_version,
        )
    return parser_name, parser_version


def _bounded_regular_file_read(path: Path) -> bytes:
    try:
        path_status = os.lstat(path)
        if stat.S_ISLNK(path_status.st_mode) or not stat.S_ISREG(path_status.st_mode):
            raise KnowledgeParseError(KnowledgeIngestionErrorCode.NOT_REGULAR_FILE)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except KnowledgeParseError:
        raise
    except OSError:
        raise KnowledgeParseError(KnowledgeIngestionErrorCode.MALFORMED_SOURCE) from None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise KnowledgeParseError(KnowledgeIngestionErrorCode.NOT_REGULAR_FILE)
        if before.st_size > MAX_RAW_SOURCE_BYTES:
            raise KnowledgeParseError(KnowledgeIngestionErrorCode.SOURCE_TOO_LARGE)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw_bytes = stream.read(MAX_RAW_SOURCE_BYTES + 1)
        if len(raw_bytes) > MAX_RAW_SOURCE_BYTES:
            raise KnowledgeParseError(KnowledgeIngestionErrorCode.SOURCE_TOO_LARGE)
        after = os.fstat(descriptor)
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_identity != after_identity or len(raw_bytes) != after.st_size:
            raise KnowledgeParseError(KnowledgeIngestionErrorCode.SOURCE_CHANGED_DURING_READ)
        return raw_bytes
    except KnowledgeParseError:
        raise
    except OSError:
        raise KnowledgeParseError(KnowledgeIngestionErrorCode.MALFORMED_SOURCE) from None
    finally:
        os.close(descriptor)


def _parse_error(code: KnowledgeIngestionErrorCode, artifact: KnowledgeSourceArtifact):
    return KnowledgeParseError(
        code,
        source_artifact_id=artifact.source_artifact_id,
        parser_name=artifact.parser_name,
        parser_version=artifact.parser_version,
    )


def _canonical_text(value: str) -> str:
    if type(value) is not str:
        raise ValueError("metadata text must be a string")
    return unicodedata.normalize("NFC", value).strip()


def _canonical_media_type(value: str) -> str:
    return _canonical_text(value).lower()
