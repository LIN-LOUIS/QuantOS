"""Append-only canonical Financial Knowledge storage with PIT-safe queries."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping

from quantos.config import DEFAULT_SETTINGS, Settings
from quantos.schemas._validation import require_aware
from quantos.schemas.knowledge import (
    KNOWLEDGE_CORPUS_SCHEMA_VERSION,
    KNOWLEDGE_DOCUMENT_SCHEMA_VERSION,
    KnowledgeCorpusManifest,
    KnowledgeDocument,
    KnowledgeProvenance,
    KnowledgeResearchView,
    KnowledgeSourceType,
    KnowledgeTemporalClass,
    build_knowledge_corpus_manifest,
)
from .market import StorageError


_DOCUMENT_FIELDS = {
    "schema_version", "document_id", "document_family_id", "document_version",
    "source", "source_type", "title", "content", "content_hash",
    "temporal_class", "published_at", "available_at", "retrieved_at",
    "effective_from", "effective_to", "entity_refs", "provenance",
}
_PROVENANCE_FIELDS = {
    "source_identifier", "ingestion_adapter", "ingestion_adapter_version",
    "origin_reference",
}
_MANIFEST_FIELDS = {
    "schema_version", "corpus_version", "document_ids", "document_family_ids",
    "document_count", "document_family_count", "generated_at",
}
_DOCUMENT_ID = re.compile(r"^[0-9a-f]{64}$")


class KnowledgeStorageError(StorageError):
    pass


class KnowledgeCorruptionError(KnowledgeStorageError):
    pass


class KnowledgeCollisionError(KnowledgeStorageError):
    pass


class KnowledgeNotFoundError(KnowledgeStorageError):
    pass


def knowledge_document_record(value: KnowledgeDocument) -> dict[str, object]:
    if not isinstance(value, KnowledgeDocument):
        raise TypeError("canonical knowledge document required")
    return {
        "schema_version": value.schema_version,
        "document_id": value.document_id,
        "document_family_id": value.document_family_id,
        "document_version": value.document_version,
        "source": value.source,
        "source_type": value.source_type.value,
        "title": value.title,
        "content": value.content,
        "content_hash": value.content_hash,
        "temporal_class": value.temporal_class.value,
        "published_at": _iso(value.published_at),
        "available_at": _iso(value.available_at),
        "retrieved_at": _iso(value.retrieved_at),
        "effective_from": _iso(value.effective_from),
        "effective_to": _iso(value.effective_to),
        "entity_refs": list(value.entity_refs),
        "provenance": {
            "source_identifier": value.provenance.source_identifier,
            "ingestion_adapter": value.provenance.ingestion_adapter,
            "ingestion_adapter_version": value.provenance.ingestion_adapter_version,
            "origin_reference": value.provenance.origin_reference,
        },
    }


def knowledge_document_from_record(record: Mapping[str, object]) -> KnowledgeDocument:
    if not isinstance(record, Mapping) or set(record) != _DOCUMENT_FIELDS:
        raise ValueError("invalid knowledge document fields")
    if record["schema_version"] != KNOWLEDGE_DOCUMENT_SCHEMA_VERSION:
        raise ValueError("unsupported knowledge document schema")
    provenance_record = record["provenance"]
    if not isinstance(provenance_record, Mapping) or set(provenance_record) != _PROVENANCE_FIELDS:
        raise ValueError("invalid knowledge provenance fields")
    entity_refs = record["entity_refs"]
    if type(entity_refs) is not list:
        raise ValueError("persisted entity_refs must be a list")
    provenance = KnowledgeProvenance(
        source_identifier=provenance_record["source_identifier"],
        ingestion_adapter=provenance_record["ingestion_adapter"],
        ingestion_adapter_version=provenance_record["ingestion_adapter_version"],
        origin_reference=provenance_record["origin_reference"],
    )
    value = KnowledgeDocument(
        schema_version=record["schema_version"],
        document_id=record["document_id"],
        document_family_id=record["document_family_id"],
        document_version=record["document_version"],
        source=record["source"],
        source_type=KnowledgeSourceType(record["source_type"]),
        title=record["title"],
        content=record["content"],
        content_hash=record["content_hash"],
        temporal_class=KnowledgeTemporalClass(record["temporal_class"]),
        published_at=_datetime(record["published_at"]),
        available_at=_required_datetime(record["available_at"]),
        retrieved_at=_required_datetime(record["retrieved_at"]),
        effective_from=_datetime(record["effective_from"]),
        effective_to=_datetime(record["effective_to"]),
        entity_refs=tuple(entity_refs),
        provenance=provenance,
    )
    if _canonical_bytes(knowledge_document_record(value)) != _canonical_bytes(record):
        raise ValueError("noncanonical knowledge document record")
    return value


def knowledge_manifest_record(value: KnowledgeCorpusManifest) -> dict[str, object]:
    if not isinstance(value, KnowledgeCorpusManifest):
        raise TypeError("knowledge corpus manifest required")
    return {
        "schema_version": value.schema_version,
        "corpus_version": value.corpus_version,
        "document_ids": list(value.document_ids),
        "document_family_ids": list(value.document_family_ids),
        "document_count": value.document_count,
        "document_family_count": value.document_family_count,
        "generated_at": value.generated_at.isoformat(),
    }


def knowledge_manifest_from_record(record: Mapping[str, object]) -> KnowledgeCorpusManifest:
    if not isinstance(record, Mapping) or set(record) != _MANIFEST_FIELDS:
        raise ValueError("invalid knowledge manifest fields")
    if record["schema_version"] != KNOWLEDGE_CORPUS_SCHEMA_VERSION:
        raise ValueError("unsupported knowledge corpus schema")
    if type(record["document_ids"]) is not list or type(record["document_family_ids"]) is not list:
        raise ValueError("persisted manifest IDs must be lists")
    value = KnowledgeCorpusManifest(
        schema_version=record["schema_version"],
        corpus_version=record["corpus_version"],
        document_ids=tuple(record["document_ids"]),
        document_family_ids=tuple(record["document_family_ids"]),
        document_count=record["document_count"],
        document_family_count=record["document_family_count"],
        generated_at=_required_datetime(record["generated_at"]),
    )
    if _canonical_bytes(knowledge_manifest_record(value)) != _canonical_bytes(record):
        raise ValueError("noncanonical knowledge manifest record")
    return value


class KnowledgeRepository:
    def __init__(self, root: Path | None = None, *, settings: Settings = DEFAULT_SETTINGS):
        self.root = Path(root) if root is not None else settings.data_root / "knowledge"
        self.documents_root = self.root / "documents"

    def store(self, document: KnowledgeDocument) -> Path:
        if not isinstance(document, KnowledgeDocument):
            raise TypeError("repository accepts only canonical KnowledgeDocument")
        record = knowledge_document_record(document)
        knowledge_document_from_record(record)
        directory = self.documents_root / f"document_family_id={document.document_family_id}"
        target = directory / f"document_id={document.document_id}.json"
        temporary = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with _exclusive_directory_lock(directory):
                self._guard_family_version(document, target)
                if target.exists():
                    return self._verify_existing(target, document)
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=directory, prefix=".knowledge-", suffix=".tmp", delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(_canonical_bytes(record) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    return self._verify_existing(target, document)
                return target
        except KnowledgeStorageError:
            raise
        except Exception:
            raise KnowledgeStorageError("failed to persist canonical knowledge document") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read(self, path: Path) -> KnowledgeDocument:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            value = knowledge_document_from_record(raw)
            if Path(path).resolve() != self._path_for(value).resolve():
                raise ValueError("knowledge document path does not match identity")
            return value
        except Exception:
            raise KnowledgeCorruptionError("canonical knowledge document is corrupt") from None

    def list_documents(self) -> tuple[KnowledgeDocument, ...]:
        if not self.documents_root.exists():
            return ()
        values = tuple(self.read(path) for path in sorted(self.documents_root.rglob("*.json")))
        return tuple(sorted(values, key=_document_order))

    def get_document(self, document_id: str) -> KnowledgeDocument:
        """Return one canonical document by exact identity without mutating storage."""
        if type(document_id) is not str or not _DOCUMENT_ID.fullmatch(document_id):
            raise ValueError("document_id must be a SHA-256 digest")
        matches = tuple(
            item for item in self.list_documents() if item.document_id == document_id
        )
        if not matches:
            raise KnowledgeNotFoundError("knowledge document was not found")
        if len(matches) != 1:
            raise KnowledgeCorruptionError("knowledge document identity is duplicated")
        return matches[0]

    def query_as_of(
        self, as_of_time: datetime, *, source_type: KnowledgeSourceType | None = None,
        entity_ref: str | None = None,
        temporal_class: KnowledgeTemporalClass | None = None,
        document_family_id: str | None = None,
    ) -> tuple[KnowledgeDocument, ...]:
        require_aware(as_of_time, "as_of_time")
        _validate_filters(source_type, entity_ref, temporal_class, document_family_id)
        return tuple(
            item for item in self.list_documents()
            if item.available_at <= as_of_time
            and _matches(item, source_type, entity_ref, temporal_class, document_family_id)
        )

    def query_research(
        self, *, corpus_cutoff: datetime, historical_as_of: datetime,
        source_type: KnowledgeSourceType | None = None,
        entity_ref: str | None = None,
        temporal_class: KnowledgeTemporalClass | None = None,
        document_family_id: str | None = None,
    ) -> tuple[KnowledgeResearchView, ...]:
        require_aware(corpus_cutoff, "corpus_cutoff")
        require_aware(historical_as_of, "historical_as_of")
        if historical_as_of > corpus_cutoff:
            raise ValueError("historical_as_of cannot exceed corpus_cutoff")
        documents = self.query_as_of(
            corpus_cutoff, source_type=source_type, entity_ref=entity_ref,
            temporal_class=temporal_class, document_family_id=document_family_id,
        )
        return tuple(KnowledgeResearchView(
            document=item,
            corpus_cutoff=corpus_cutoff,
            historical_as_of=historical_as_of,
            known_by_historical_as_of=item.available_at <= historical_as_of,
            knowledge_after_event=item.available_at > historical_as_of,
        ) for item in documents)

    def build_manifest(self, *, generated_at: datetime) -> KnowledgeCorpusManifest:
        return build_knowledge_corpus_manifest(self.list_documents(), generated_at=generated_at)

    def _path_for(self, document: KnowledgeDocument) -> Path:
        return (
            self.documents_root
            / f"document_family_id={document.document_family_id}"
            / f"document_id={document.document_id}.json"
        )

    def _guard_family_version(self, document: KnowledgeDocument, target: Path) -> None:
        if not target.parent.exists():
            return
        for path in sorted(target.parent.glob("document_id=*.json")):
            existing = self.read(path)
            if existing.document_version == document.document_version and existing.document_id != document.document_id:
                raise KnowledgeCollisionError("knowledge family/version collision")

    def _verify_existing(self, target: Path, document: KnowledgeDocument) -> Path:
        try:
            existing = self.read(target)
        except KnowledgeCorruptionError:
            raise KnowledgeCollisionError("knowledge identity collision or corruption") from None
        if existing != document:
            raise KnowledgeCollisionError("knowledge identity collision")
        return target


def _matches(document, source_type, entity_ref, temporal_class, family_id):
    return (
        (source_type is None or document.source_type == source_type)
        and (entity_ref is None or entity_ref in document.entity_refs)
        and (temporal_class is None or document.temporal_class == temporal_class)
        and (family_id is None or document.document_family_id == family_id)
    )


def _validate_filters(source_type, entity_ref, temporal_class, family_id):
    if source_type is not None and not isinstance(source_type, KnowledgeSourceType):
        raise ValueError("invalid knowledge source filter")
    if temporal_class is not None and not isinstance(temporal_class, KnowledgeTemporalClass):
        raise ValueError("invalid knowledge temporal filter")
    if entity_ref is not None:
        from quantos.schemas.securities import _SYMBOL_PATTERN
        if type(entity_ref) is not str or not _SYMBOL_PATTERN.fullmatch(entity_ref):
            raise ValueError("invalid canonical entity reference")
    if family_id is not None and (type(family_id) is not str or not family_id.strip()):
        raise ValueError("invalid document family filter")


def _document_order(value: KnowledgeDocument):
    return value.document_family_id, value.document_version, value.document_id


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime(value) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("knowledge timestamp must be an ISO string")
    result = datetime.fromisoformat(value)
    require_aware(result, "knowledge timestamp")
    return result


def _required_datetime(value) -> datetime:
    result = _datetime(value)
    if result is None:
        raise ValueError("required knowledge timestamp is missing")
    return result


def _canonical_bytes(value) -> bytes:
    from quantos.serialization import canonical_json_bytes

    return canonical_json_bytes(value)


@contextmanager
def _exclusive_directory_lock(directory: Path):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
