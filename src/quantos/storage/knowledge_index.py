"""Append-only persistence for rebuildable lexical knowledge indexes."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping

from quantos.config import DEFAULT_SETTINGS, Settings
from quantos.serialization import canonical_json_bytes
from quantos.schemas._validation import require_aware
from quantos.schemas.knowledge import KnowledgeSourceType, KnowledgeTemporalClass
from quantos.schemas.knowledge_retrieval import (
    KNOWLEDGE_CHUNK_SCHEMA_VERSION,
    KNOWLEDGE_LEXICAL_INDEX_SCHEMA_VERSION,
    TOKENIZER_NAME,
    TOKENIZER_VERSION,
    KnowledgeChunk,
    KnowledgeLexicalIndex,
    KnowledgeLexicalIndexEntry,
)
from .market import StorageError


_INDEX_FIELDS = {
    "schema_version", "lexical_index_version", "corpus_version",
    "chunk_manifest_version", "tokenizer_name", "tokenizer_version",
    "retrieval_algorithm", "retrieval_version", "entries", "built_at",
}
_ENTRY_FIELDS = {"chunk", "tokens"}
_CHUNK_FIELDS = {
    "schema_version", "chunk_id", "document_id", "document_family_id",
    "document_version", "chunk_index", "start_offset", "end_offset", "content",
    "content_hash", "source", "title", "source_type", "entity_refs", "published_at",
    "available_at", "effective_from", "effective_to", "temporal_class",
    "provenance_source_identifier", "origin_reference", "chunking_algorithm",
    "chunking_version",
}


class KnowledgeIndexStorageError(StorageError):
    pass


class KnowledgeIndexCorruptionError(KnowledgeIndexStorageError):
    pass


class KnowledgeIndexCollisionError(KnowledgeIndexStorageError):
    pass


class KnowledgeIndexStaleError(KnowledgeIndexStorageError):
    pass


class KnowledgeIndexNotFoundError(KnowledgeIndexStorageError):
    pass


def knowledge_lexical_index_record(index: KnowledgeLexicalIndex) -> dict[str, object]:
    if not isinstance(index, KnowledgeLexicalIndex):
        raise TypeError("KnowledgeLexicalIndex required")
    return {
        "schema_version": index.schema_version,
        "lexical_index_version": index.lexical_index_version,
        "corpus_version": index.corpus_version,
        "chunk_manifest_version": index.chunk_manifest_version,
        "tokenizer_name": index.tokenizer_name,
        "tokenizer_version": index.tokenizer_version,
        "retrieval_algorithm": index.retrieval_algorithm,
        "retrieval_version": index.retrieval_version,
        "entries": [
            {"chunk": _chunk_record(entry.chunk), "tokens": list(entry.tokens)}
            for entry in index.entries
        ],
        "built_at": index.built_at.isoformat(),
    }


def knowledge_lexical_index_from_record(record: Mapping[str, object]) -> KnowledgeLexicalIndex:
    try:
        if not isinstance(record, Mapping) or set(record) != _INDEX_FIELDS:
            raise ValueError("invalid lexical index fields")
        if record["schema_version"] != KNOWLEDGE_LEXICAL_INDEX_SCHEMA_VERSION:
            raise ValueError("unsupported lexical index schema")
        raw_entries = record["entries"]
        if type(raw_entries) is not list:
            raise ValueError("lexical index entries must be a list")
        entries = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, Mapping) or set(raw_entry) != _ENTRY_FIELDS:
                raise ValueError("invalid lexical index entry")
            if type(raw_entry["tokens"]) is not list:
                raise ValueError("lexical index tokens must be a list")
            entries.append(KnowledgeLexicalIndexEntry(
                chunk=_chunk_from_record(raw_entry["chunk"]),
                tokens=tuple(raw_entry["tokens"]),
            ))
        index = KnowledgeLexicalIndex(
            schema_version=record["schema_version"],
            lexical_index_version=record["lexical_index_version"],
            corpus_version=record["corpus_version"],
            chunk_manifest_version=record["chunk_manifest_version"],
            tokenizer_name=record["tokenizer_name"],
            tokenizer_version=record["tokenizer_version"],
            retrieval_algorithm=record["retrieval_algorithm"],
            retrieval_version=record["retrieval_version"],
            entries=tuple(entries),
            built_at=_datetime(record["built_at"]),
        )
        if canonical_json_bytes(knowledge_lexical_index_record(index)) != canonical_json_bytes(record):
            raise ValueError("noncanonical lexical index record")
        return index
    except (TypeError, ValueError, KeyError):
        raise KnowledgeIndexCorruptionError("knowledge lexical index is corrupt") from None


class KnowledgeLexicalIndexRepository:
    def __init__(self, root: Path | None = None, *, settings: Settings = DEFAULT_SETTINGS):
        self.root = (
            Path(root) if root is not None
            else settings.data_root / "knowledge" / "indexes" / "lexical"
        )

    def store(self, index: KnowledgeLexicalIndex) -> Path:
        if not isinstance(index, KnowledgeLexicalIndex):
            raise TypeError("KnowledgeLexicalIndex required")
        record = knowledge_lexical_index_record(index)
        knowledge_lexical_index_from_record(record)
        target = self.root / f"lexical_index_version={index.lexical_index_version}.json"
        temporary = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with _exclusive_lock(self.root):
                if target.exists():
                    return self._verify_existing(target, index)
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=self.root, prefix=".lexical-index-", suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(canonical_json_bytes(record) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    return self._verify_existing(target, index)
                return target
        except KnowledgeIndexStorageError:
            raise
        except Exception:
            raise KnowledgeIndexStorageError("failed to persist knowledge lexical index") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read(self, path: Path) -> KnowledgeLexicalIndex:
        if not Path(path).is_file():
            raise KnowledgeIndexNotFoundError("knowledge lexical index was not found")
        try:
            value = knowledge_lexical_index_from_record(json.loads(
                Path(path).read_text(encoding="utf-8"),
            ))
            if Path(path).resolve() != self._path_for(value.lexical_index_version).resolve():
                raise ValueError("lexical index path does not match identity")
            return value
        except KnowledgeIndexCorruptionError:
            raise
        except Exception:
            raise KnowledgeIndexCorruptionError("knowledge lexical index is corrupt") from None

    def load(
        self, lexical_index_version: str, *, expected_corpus_version: str | None = None,
        expected_chunk_manifest_version: str | None = None,
        expected_tokenizer_name: str = TOKENIZER_NAME,
        expected_tokenizer_version: str = TOKENIZER_VERSION,
    ) -> KnowledgeLexicalIndex:
        value = self.read(self._path_for(lexical_index_version))
        if expected_corpus_version is not None and value.corpus_version != expected_corpus_version:
            raise KnowledgeIndexStaleError("knowledge lexical index corpus is stale")
        if (expected_chunk_manifest_version is not None
                and value.chunk_manifest_version != expected_chunk_manifest_version):
            raise KnowledgeIndexStaleError("knowledge lexical index chunk manifest is stale")
        if (value.tokenizer_name != expected_tokenizer_name
                or value.tokenizer_version != expected_tokenizer_version):
            raise KnowledgeIndexStaleError("knowledge lexical index tokenizer is stale")
        return value

    def _path_for(self, version: str) -> Path:
        if type(version) is not str or len(version) != 64 or any(
            character not in "0123456789abcdef" for character in version
        ):
            raise KnowledgeIndexCorruptionError("invalid lexical index version")
        return self.root / f"lexical_index_version={version}.json"

    def _verify_existing(self, target: Path, index: KnowledgeLexicalIndex) -> Path:
        try:
            existing = self.read(target)
        except KnowledgeIndexCorruptionError:
            raise KnowledgeIndexCollisionError("lexical index collision or corruption") from None
        existing_record = knowledge_lexical_index_record(existing)
        requested_record = knowledge_lexical_index_record(index)
        existing_record.pop("built_at")
        requested_record.pop("built_at")
        if existing_record != requested_record:
            raise KnowledgeIndexCollisionError("lexical index identity collision")
        return target


def _chunk_record(chunk: KnowledgeChunk) -> dict[str, object]:
    return {
        "schema_version": chunk.schema_version,
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "document_family_id": chunk.document_family_id,
        "document_version": chunk.document_version,
        "chunk_index": chunk.chunk_index,
        "start_offset": chunk.start_offset,
        "end_offset": chunk.end_offset,
        "content": chunk.content,
        "content_hash": chunk.content_hash,
        "source": chunk.source,
        "title": chunk.title,
        "source_type": chunk.source_type.value,
        "entity_refs": list(chunk.entity_refs),
        "published_at": _iso(chunk.published_at),
        "available_at": _iso(chunk.available_at),
        "effective_from": _iso(chunk.effective_from),
        "effective_to": _iso(chunk.effective_to),
        "temporal_class": chunk.temporal_class.value,
        "provenance_source_identifier": chunk.provenance_source_identifier,
        "origin_reference": chunk.origin_reference,
        "chunking_algorithm": chunk.chunking_algorithm,
        "chunking_version": chunk.chunking_version,
    }


def _chunk_from_record(record) -> KnowledgeChunk:
    if not isinstance(record, Mapping) or set(record) != _CHUNK_FIELDS:
        raise ValueError("invalid persisted chunk fields")
    if record["schema_version"] != KNOWLEDGE_CHUNK_SCHEMA_VERSION:
        raise ValueError("unsupported persisted chunk schema")
    if type(record["entity_refs"]) is not list:
        raise ValueError("persisted entity references must be a list")
    return KnowledgeChunk(
        schema_version=record["schema_version"],
        chunk_id=record["chunk_id"],
        document_id=record["document_id"],
        document_family_id=record["document_family_id"],
        document_version=record["document_version"],
        chunk_index=record["chunk_index"],
        start_offset=record["start_offset"],
        end_offset=record["end_offset"],
        content=record["content"],
        content_hash=record["content_hash"],
        source=record["source"],
        title=record["title"],
        source_type=KnowledgeSourceType(record["source_type"]),
        entity_refs=tuple(record["entity_refs"]),
        published_at=_optional_datetime(record["published_at"]),
        available_at=_datetime(record["available_at"]),
        effective_from=_optional_datetime(record["effective_from"]),
        effective_to=_optional_datetime(record["effective_to"]),
        temporal_class=KnowledgeTemporalClass(record["temporal_class"]),
        provenance_source_identifier=record["provenance_source_identifier"],
        origin_reference=record["origin_reference"],
        chunking_algorithm=record["chunking_algorithm"],
        chunking_version=record["chunking_version"],
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime(value) -> datetime:
    if type(value) is not str:
        raise ValueError("persisted timestamp must be an ISO string")
    result = datetime.fromisoformat(value)
    require_aware(result, "persisted timestamp")
    return result


def _optional_datetime(value) -> datetime | None:
    return None if value is None else _datetime(value)


@contextmanager
def _exclusive_lock(directory: Path):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
