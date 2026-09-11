"""Deterministic paragraph-aware chunk derivation from canonical knowledge."""

from __future__ import annotations

from datetime import datetime
import re

from quantos.schemas.knowledge import KnowledgeCorpusManifest, KnowledgeDocument
from quantos.schemas.knowledge_retrieval import (
    CHUNKING_ALGORITHM,
    CHUNKING_VERSION,
    KNOWLEDGE_CHUNK_MANIFEST_SCHEMA_VERSION,
    KNOWLEDGE_CHUNK_SCHEMA_VERSION,
    MAX_CHUNK_SIZE,
    OVERLAP_SIZE,
    TARGET_CHUNK_SIZE,
    KnowledgeChunk,
    KnowledgeChunkManifest,
    chunk_content_hash,
    knowledge_chunk_id,
    knowledge_chunk_manifest_version,
)


_PARAGRAPH_BOUNDARY = re.compile(r"\n[ \t]*\n")


def chunk_knowledge_document(document: KnowledgeDocument) -> tuple[KnowledgeChunk, ...]:
    """Derive ordered, non-empty Unicode-code-point slices from one document."""
    if not isinstance(document, KnowledgeDocument):
        raise TypeError("canonical KnowledgeDocument required")
    content = document.content
    ranges = _chunk_ranges(content)
    chunks = []
    for chunk_index, (start, end) in enumerate(ranges):
        value = content[start:end]
        digest = chunk_content_hash(value)
        chunks.append(KnowledgeChunk(
            schema_version=KNOWLEDGE_CHUNK_SCHEMA_VERSION,
            chunk_id=knowledge_chunk_id(
                document_id=document.document_id,
                chunk_index=chunk_index,
                start_offset=start,
                end_offset=end,
                content_hash=digest,
            ),
            document_id=document.document_id,
            document_family_id=document.document_family_id,
            document_version=document.document_version,
            chunk_index=chunk_index,
            start_offset=start,
            end_offset=end,
            content=value,
            content_hash=digest,
            source=document.source,
            title=document.title,
            source_type=document.source_type,
            entity_refs=document.entity_refs,
            published_at=document.published_at,
            available_at=document.available_at,
            effective_from=document.effective_from,
            effective_to=document.effective_to,
            temporal_class=document.temporal_class,
            provenance_source_identifier=document.provenance.source_identifier,
            origin_reference=document.provenance.origin_reference,
            chunking_algorithm=CHUNKING_ALGORITHM,
            chunking_version=CHUNKING_VERSION,
        ))
    if not chunks:
        raise ValueError("canonical document produced no knowledge chunks")
    return tuple(chunks)


def chunk_knowledge_corpus(
    documents: tuple[KnowledgeDocument, ...],
) -> tuple[KnowledgeChunk, ...]:
    if type(documents) is not tuple or any(
        not isinstance(item, KnowledgeDocument) for item in documents
    ):
        raise TypeError("canonical knowledge document tuple required")
    ordered = tuple(sorted(
        documents,
        key=lambda item: (item.document_family_id, item.document_version, item.document_id),
    ))
    if len({item.document_id for item in ordered}) != len(ordered):
        raise ValueError("knowledge corpus contains duplicate documents")
    return tuple(chunk for document in ordered for chunk in chunk_knowledge_document(document))


def build_knowledge_chunk_manifest(
    chunks: tuple[KnowledgeChunk, ...], *, corpus_manifest: KnowledgeCorpusManifest,
    generated_at: datetime,
) -> KnowledgeChunkManifest:
    if type(chunks) is not tuple or any(not isinstance(item, KnowledgeChunk) for item in chunks):
        raise TypeError("knowledge chunk tuple required")
    if not isinstance(corpus_manifest, KnowledgeCorpusManifest):
        raise TypeError("knowledge corpus manifest required")
    chunk_ids = tuple(sorted(item.chunk_id for item in chunks))
    if len(chunk_ids) != len(set(chunk_ids)):
        raise ValueError("chunk manifest cannot contain duplicate chunks")
    document_ids = tuple(sorted({item.document_id for item in chunks}))
    if document_ids != corpus_manifest.document_ids:
        raise ValueError("chunk documents do not match corpus manifest")
    version = knowledge_chunk_manifest_version(
        corpus_version=corpus_manifest.corpus_version,
        chunk_ids=chunk_ids,
    )
    return KnowledgeChunkManifest(
        schema_version=KNOWLEDGE_CHUNK_MANIFEST_SCHEMA_VERSION,
        corpus_version=corpus_manifest.corpus_version,
        chunking_algorithm=CHUNKING_ALGORITHM,
        chunking_version=CHUNKING_VERSION,
        chunk_ids=chunk_ids,
        document_ids=document_ids,
        chunk_count=len(chunk_ids),
        document_count=len(document_ids),
        chunk_manifest_version=version,
        generated_at=generated_at,
    )


def _chunk_ranges(content: str) -> tuple[tuple[int, int], ...]:
    if OVERLAP_SIZE != 0:
        raise ValueError("paragraph_window:v1 requires zero overlap")
    ranges = []
    cursor = 0
    length = len(content)
    while cursor < length:
        while cursor < length and content[cursor].isspace():
            cursor += 1
        if cursor >= length:
            break
        if length - cursor <= MAX_CHUNK_SIZE:
            raw_end = length
        else:
            maximum = cursor + MAX_CHUNK_SIZE
            target = min(cursor + TARGET_CHUNK_SIZE, maximum)
            paragraph_ends = tuple(
                match.start() for match in _PARAGRAPH_BOUNDARY.finditer(content, cursor, maximum + 1)
                if match.start() > cursor
            )
            line_ends = tuple(
                position for position in range(cursor + 1, maximum + 1)
                if content[position - 1] == "\n"
            )
            raw_end = (
                _preferred_boundary(paragraph_ends, target)
                or _preferred_boundary(line_ends, target)
                or maximum
            )
        end = raw_end
        while end > cursor and content[end - 1].isspace():
            end -= 1
        if end > cursor:
            ranges.append((cursor, end))
        cursor = raw_end
    if any(end - start > MAX_CHUNK_SIZE for start, end in ranges):
        raise ValueError("chunk range exceeds maximum size")
    return tuple(ranges)


def _preferred_boundary(boundaries: tuple[int, ...], target: int) -> int | None:
    before = tuple(value for value in boundaries if value <= target)
    if before:
        return max(before)
    after = tuple(value for value in boundaries if value > target)
    return min(after) if after else None
