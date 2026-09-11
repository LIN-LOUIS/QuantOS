"""PIT-correct deterministic lexical retrieval over canonical knowledge chunks."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import math
import re
import unicodedata

from quantos.knowledge_chunking import (
    build_knowledge_chunk_manifest,
    chunk_knowledge_corpus,
)
from quantos.schemas.knowledge import KnowledgeQueryMode, KnowledgeTemporalClass
from quantos.schemas.knowledge_retrieval import (
    BM25_B,
    BM25_K1,
    KNOWLEDGE_LEXICAL_INDEX_SCHEMA_VERSION,
    KNOWLEDGE_RETRIEVAL_RESULT_SCHEMA_VERSION,
    RETRIEVAL_ALGORITHM,
    RETRIEVAL_VERSION,
    TOKENIZER_NAME,
    TOKENIZER_VERSION,
    KnowledgeChunk,
    KnowledgeChunkManifest,
    KnowledgeLexicalIndex,
    KnowledgeLexicalIndexEntry,
    KnowledgeRetrievalErrorCode,
    KnowledgeRetrievalHit,
    KnowledgeRetrievalRequest,
    KnowledgeRetrievalResult,
    knowledge_lexical_index_version,
    knowledge_retrieval_request_id,
    normalize_lexical_query,
)
from quantos.storage.knowledge import KnowledgeRepository


_TOKEN_RUN = re.compile(r"[A-Za-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]+")
_SAFE_ERRORS = {
    KnowledgeRetrievalErrorCode.EMPTY_QUERY: "knowledge retrieval query is empty",
    KnowledgeRetrievalErrorCode.EMPTY_QUERY_TOKENS: "knowledge retrieval query has no lexical tokens",
    KnowledgeRetrievalErrorCode.UNSUPPORTED_CHUNKING_VERSION: "knowledge chunking version is unsupported",
    KnowledgeRetrievalErrorCode.UNSUPPORTED_TOKENIZER_VERSION: "knowledge tokenizer version is unsupported",
    KnowledgeRetrievalErrorCode.UNSUPPORTED_RETRIEVAL_VERSION: "knowledge retrieval version is unsupported",
    KnowledgeRetrievalErrorCode.STALE_INDEX: "knowledge lexical index is stale",
    KnowledgeRetrievalErrorCode.CHUNK_MANIFEST_MISMATCH: "knowledge chunk manifest does not match index",
    KnowledgeRetrievalErrorCode.TOKENIZER_MISMATCH: "knowledge tokenizer does not match index",
    KnowledgeRetrievalErrorCode.CORRUPT_INDEX: "knowledge lexical index is corrupt",
}


class KnowledgeRetrievalError(ValueError):
    def __init__(self, code: KnowledgeRetrievalErrorCode) -> None:
        self.code = code
        super().__init__(_SAFE_ERRORS[code])


class KnowledgeRetrievalResultIntegrityError(ValueError):
    """Raised when a supplied retrieval artifact is not the canonical result."""


def tokenize_lexical(text: str) -> tuple[str, ...]:
    """Tokenize ASCII words/numbers and Chinese character bigrams deterministically."""
    if type(text) is not str:
        raise TypeError("lexical tokenizer requires text")
    normalized = unicodedata.normalize("NFC", text)
    tokens = []
    for match in _TOKEN_RUN.finditer(normalized):
        value = match.group(0)
        if value[0].isascii():
            tokens.append(value.lower())
        elif len(value) == 1:
            tokens.append(value)
        else:
            tokens.extend(value[index:index + 2] for index in range(len(value) - 1))
    return tuple(tokens)


def build_knowledge_lexical_index(
    repository: KnowledgeRepository, *, built_at: datetime,
) -> KnowledgeLexicalIndex:
    """Explicitly rebuild an immutable lexical index from canonical documents."""
    if not isinstance(repository, KnowledgeRepository):
        raise TypeError("KnowledgeRepository required")
    documents = repository.list_documents()
    corpus_manifest = repository.build_manifest(generated_at=built_at)
    chunks = chunk_knowledge_corpus(documents)
    chunk_manifest = build_knowledge_chunk_manifest(
        chunks, corpus_manifest=corpus_manifest, generated_at=built_at,
    )
    entries = tuple(KnowledgeLexicalIndexEntry(
        chunk=chunk, tokens=tokenize_lexical(chunk.content),
    ) for chunk in chunks)
    version = knowledge_lexical_index_version(
        corpus_version=corpus_manifest.corpus_version,
        chunk_manifest_version=chunk_manifest.chunk_manifest_version,
        entries=entries,
    )
    return KnowledgeLexicalIndex(
        schema_version=KNOWLEDGE_LEXICAL_INDEX_SCHEMA_VERSION,
        lexical_index_version=version,
        corpus_version=corpus_manifest.corpus_version,
        chunk_manifest_version=chunk_manifest.chunk_manifest_version,
        tokenizer_name=TOKENIZER_NAME,
        tokenizer_version=TOKENIZER_VERSION,
        retrieval_algorithm=RETRIEVAL_ALGORITHM,
        retrieval_version=RETRIEVAL_VERSION,
        entries=entries,
        built_at=built_at,
    )


def current_chunk_manifest(
    repository: KnowledgeRepository, *, generated_at: datetime,
) -> KnowledgeChunkManifest:
    documents = repository.list_documents()
    corpus_manifest = repository.build_manifest(generated_at=generated_at)
    chunks = chunk_knowledge_corpus(documents)
    return build_knowledge_chunk_manifest(
        chunks, corpus_manifest=corpus_manifest, generated_at=generated_at,
    )


def retrieve_knowledge(
    repository: KnowledgeRepository,
    index: KnowledgeLexicalIndex,
    request: KnowledgeRetrievalRequest,
    *,
    generated_at: datetime,
) -> KnowledgeRetrievalResult:
    """Query an explicitly supplied index; never rebuild it implicitly."""
    if not isinstance(repository, KnowledgeRepository):
        raise TypeError("KnowledgeRepository required")
    if not isinstance(index, KnowledgeLexicalIndex):
        raise TypeError("KnowledgeLexicalIndex required")
    if not isinstance(request, KnowledgeRetrievalRequest):
        raise TypeError("KnowledgeRetrievalRequest required")
    query_tokens = tokenize_lexical(normalize_lexical_query(request.query))
    if not query_tokens:
        raise KnowledgeRetrievalError(KnowledgeRetrievalErrorCode.EMPTY_QUERY_TOKENS)

    corpus_manifest = repository.build_manifest(generated_at=generated_at)
    if index.corpus_version != corpus_manifest.corpus_version:
        raise KnowledgeRetrievalError(KnowledgeRetrievalErrorCode.STALE_INDEX)
    chunks = chunk_knowledge_corpus(repository.list_documents())
    chunk_manifest = build_knowledge_chunk_manifest(
        chunks, corpus_manifest=corpus_manifest, generated_at=generated_at,
    )
    if index.chunk_manifest_version != chunk_manifest.chunk_manifest_version:
        raise KnowledgeRetrievalError(KnowledgeRetrievalErrorCode.CHUNK_MANIFEST_MISMATCH)
    if (index.tokenizer_name != request.tokenizer_name
            or index.tokenizer_version != request.tokenizer_version):
        raise KnowledgeRetrievalError(KnowledgeRetrievalErrorCode.TOKENIZER_MISMATCH)
    if (index.retrieval_algorithm != request.retrieval_algorithm
            or index.retrieval_version != request.retrieval_version):
        raise KnowledgeRetrievalError(KnowledgeRetrievalErrorCode.UNSUPPORTED_RETRIEVAL_VERSION)
    canonical_entries = tuple(KnowledgeLexicalIndexEntry(
        chunk=chunk, tokens=tokenize_lexical(chunk.content),
    ) for chunk in chunks)
    if index.entries != canonical_entries:
        raise KnowledgeRetrievalError(KnowledgeRetrievalErrorCode.CORRUPT_INDEX)

    eligible = tuple(
        entry for entry in index.entries if _retrieval_eligible(entry.chunk, request)
    )
    scores = _bm25_scores(eligible, query_tokens)
    ranked = sorted(
        ((entry, scores.get(entry.chunk.chunk_id, 0.0)) for entry in eligible),
        key=lambda item: (
            -item[1], item[0].chunk.document_family_id,
            item[0].chunk.document_version, item[0].chunk.chunk_index,
            item[0].chunk.chunk_id,
        ),
    )
    matched = tuple((entry, score) for entry, score in ranked if score > 0)[:request.limit]
    hits = tuple(_hit(rank, entry.chunk, score, request) for rank, (entry, score) in enumerate(
        matched, start=1,
    ))
    request_id = knowledge_retrieval_request_id(
        request,
        corpus_version=corpus_manifest.corpus_version,
        chunk_manifest_version=chunk_manifest.chunk_manifest_version,
        lexical_index_version=index.lexical_index_version,
    )
    return KnowledgeRetrievalResult(
        schema_version=KNOWLEDGE_RETRIEVAL_RESULT_SCHEMA_VERSION,
        retrieval_request_id=request_id,
        query=request.query,
        mode=request.mode,
        as_of_time=request.as_of_time,
        corpus_cutoff=request.corpus_cutoff,
        corpus_version=corpus_manifest.corpus_version,
        chunk_manifest_version=chunk_manifest.chunk_manifest_version,
        lexical_index_version=index.lexical_index_version,
        retrieval_algorithm=index.retrieval_algorithm,
        retrieval_version=index.retrieval_version,
        tokenizer_name=index.tokenizer_name,
        tokenizer_version=index.tokenizer_version,
        eligible_chunk_count=len(eligible),
        hits=hits,
        generated_at=generated_at,
    )


def validate_knowledge_retrieval_result(
    repository: KnowledgeRepository,
    index: KnowledgeLexicalIndex,
    request: KnowledgeRetrievalRequest,
    result: KnowledgeRetrievalResult,
) -> None:
    """Fail closed unless ``result`` exactly matches deterministic Phase 4C output."""
    if not isinstance(result, KnowledgeRetrievalResult):
        raise TypeError("KnowledgeRetrievalResult required")
    expected = retrieve_knowledge(
        repository, index, request, generated_at=result.generated_at,
    )
    if result != expected:
        raise KnowledgeRetrievalResultIntegrityError(
            "knowledge retrieval result failed canonical integrity validation"
        )


def _bm25_scores(
    entries: tuple[KnowledgeLexicalIndexEntry, ...], query_tokens: tuple[str, ...],
) -> dict[str, float]:
    if not entries:
        return {}
    query_terms = tuple(sorted(set(query_tokens)))
    frequencies = {entry.chunk.chunk_id: Counter(entry.tokens) for entry in entries}
    lengths = {chunk_id: sum(counter.values()) for chunk_id, counter in frequencies.items()}
    average_length = sum(lengths.values()) / len(entries)
    if average_length == 0:
        return {}
    document_frequency = {
        term: sum(1 for counter in frequencies.values() if counter.get(term, 0) > 0)
        for term in query_terms
    }
    size = len(entries)
    scores = {}
    for entry in entries:
        chunk_id = entry.chunk.chunk_id
        length = lengths[chunk_id]
        score = 0.0
        for term in query_terms:
            frequency = frequencies[chunk_id].get(term, 0)
            if frequency == 0:
                continue
            df = document_frequency[term]
            inverse_frequency = math.log(1.0 + (size - df + 0.5) / (df + 0.5))
            denominator = frequency + BM25_K1 * (
                1.0 - BM25_B + BM25_B * length / average_length
            )
            score += inverse_frequency * frequency * (BM25_K1 + 1.0) / denominator
        scores[chunk_id] = score
    return scores


def _retrieval_eligible(chunk: KnowledgeChunk, request: KnowledgeRetrievalRequest) -> bool:
    cutoff = (
        request.as_of_time
        if request.mode is KnowledgeQueryMode.STRICT_LIVE
        else request.corpus_cutoff
    )
    if cutoff is None or chunk.available_at > cutoff:
        return False
    if request.entity_refs and not set(request.entity_refs).intersection(chunk.entity_refs):
        return False
    if request.source_types and chunk.source_type not in request.source_types:
        return False
    if request.temporal_classes and chunk.temporal_class not in request.temporal_classes:
        return False
    if request.document_family_ids and chunk.document_family_id not in request.document_family_ids:
        return False
    return request.effective_at is None or _effective_at(chunk, request.effective_at)


def _effective_at(chunk: KnowledgeChunk, instant: datetime) -> bool:
    if chunk.temporal_class is KnowledgeTemporalClass.TIMELESS:
        return True
    if chunk.effective_from is None or instant < chunk.effective_from:
        return False
    return chunk.effective_to is None or instant <= chunk.effective_to


def _hit(rank: int, chunk: KnowledgeChunk, score: float,
         request: KnowledgeRetrievalRequest) -> KnowledgeRetrievalHit:
    known = chunk.available_at <= request.as_of_time
    return KnowledgeRetrievalHit(
        rank=rank,
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        document_family_id=chunk.document_family_id,
        document_version=chunk.document_version,
        chunk_index=chunk.chunk_index,
        score=float(score),
        content=chunk.content,
        source=chunk.source,
        title=chunk.title,
        source_type=chunk.source_type,
        entity_refs=chunk.entity_refs,
        available_at=chunk.available_at,
        published_at=chunk.published_at,
        effective_from=chunk.effective_from,
        effective_to=chunk.effective_to,
        provenance_source_identifier=chunk.provenance_source_identifier,
        origin_reference=chunk.origin_reference,
        known_by_historical_as_of=known,
        knowledge_after_event=not known,
    )
