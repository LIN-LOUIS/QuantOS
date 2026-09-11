"""Deterministic assembly of PIT-explicit knowledge context bundles."""

from __future__ import annotations

from datetime import datetime

from quantos.knowledge_retrieval import (
    KnowledgeRetrievalError,
    KnowledgeRetrievalResultIntegrityError,
    validate_knowledge_retrieval_result,
)
from quantos.schemas.knowledge_context import (
    KNOWLEDGE_CONTEXT_BUNDLE_SCHEMA_VERSION,
    KNOWLEDGE_CONTEXT_ITEM_SCHEMA_VERSION,
    KnowledgeContextBundle,
    KnowledgeContextItem,
    KnowledgeContextLane,
    KnowledgeContextPolicy,
    KnowledgeContextStopReason,
    knowledge_context_id,
    validate_knowledge_context_bundle,
)
from quantos.schemas.knowledge_retrieval import (
    KnowledgeLexicalIndex,
    KnowledgeRetrievalHit,
    KnowledgeRetrievalRequest,
    KnowledgeRetrievalResult,
)
from quantos.storage.knowledge import KnowledgeRepository


class KnowledgeContextAssemblyError(ValueError):
    """Raised when a retrieval artifact cannot safely become context."""


def assemble_knowledge_context(
    repository: KnowledgeRepository,
    index: KnowledgeLexicalIndex,
    request: KnowledgeRetrievalRequest,
    retrieval_result: KnowledgeRetrievalResult,
    policy: KnowledgeContextPolicy,
    *,
    generated_at: datetime,
) -> KnowledgeContextBundle:
    """Compile a validated retrieval artifact with rank-prefix character budgets."""
    if not isinstance(policy, KnowledgeContextPolicy):
        raise TypeError("KnowledgeContextPolicy required")
    try:
        validate_knowledge_retrieval_result(
            repository, index, request, retrieval_result,
        )
    except (KnowledgeRetrievalError, KnowledgeRetrievalResultIntegrityError,
            TypeError, ValueError) as error:
        raise KnowledgeContextAssemblyError(
            "knowledge retrieval result failed context integrity validation"
        ) from error

    chunks = {entry.chunk.chunk_id: entry.chunk for entry in index.entries}
    selected: list[KnowledgeContextItem] = []
    selected_chars = 0
    if not retrieval_result.hits:
        stop_reason = KnowledgeContextStopReason.NO_HITS
    else:
        stop_reason = KnowledgeContextStopReason.ALL_SELECTED
        for hit in retrieval_result.hits:
            if len(selected) == policy.max_items:
                stop_reason = KnowledgeContextStopReason.MAX_ITEMS
                break
            if (hit.knowledge_after_event
                    and not policy.allow_retrospective_research_context):
                stop_reason = KnowledgeContextStopReason.RETROSPECTIVE_NOT_ALLOWED
                break
            char_count = len(hit.content)
            if selected_chars + char_count > policy.max_context_chars:
                stop_reason = KnowledgeContextStopReason.CHAR_BUDGET
                break
            chunk = chunks[hit.chunk_id]
            selected.append(_context_item(hit, chunk.content_hash, chunk.temporal_class))
            selected_chars += char_count

    historical = tuple(
        item for item in selected
        if item.lane is KnowledgeContextLane.HISTORICALLY_KNOWN
    )
    retrospective = tuple(
        item for item in selected
        if item.lane is KnowledgeContextLane.RETROSPECTIVE_AFTER_EVENT
    )
    selected_items = tuple(selected)
    context_id = knowledge_context_id(
        policy=policy,
        retrieval_request_id=retrieval_result.retrieval_request_id,
        mode=retrieval_result.mode,
        as_of_time=retrieval_result.as_of_time,
        corpus_cutoff=retrieval_result.corpus_cutoff,
        corpus_version=retrieval_result.corpus_version,
        chunk_manifest_version=retrieval_result.chunk_manifest_version,
        lexical_index_version=retrieval_result.lexical_index_version,
        retrieved_hit_count=len(retrieval_result.hits),
        selection_stop_reason=stop_reason,
        selected_items=selected_items,
    )
    bundle = KnowledgeContextBundle(
        schema_version=KNOWLEDGE_CONTEXT_BUNDLE_SCHEMA_VERSION,
        context_id=context_id,
        policy=policy,
        retrieval_request_id=retrieval_result.retrieval_request_id,
        mode=retrieval_result.mode,
        as_of_time=retrieval_result.as_of_time,
        corpus_cutoff=retrieval_result.corpus_cutoff,
        corpus_version=retrieval_result.corpus_version,
        chunk_manifest_version=retrieval_result.chunk_manifest_version,
        lexical_index_version=retrieval_result.lexical_index_version,
        retrieved_hit_count=len(retrieval_result.hits),
        selected_item_count=len(selected_items),
        selected_char_count=selected_chars,
        historical_item_count=len(historical),
        retrospective_item_count=len(retrospective),
        omitted_hit_count=len(retrieval_result.hits) - len(selected_items),
        selection_stop_reason=stop_reason,
        historical_items=historical,
        retrospective_items=retrospective,
        generated_at=generated_at,
    )
    validate_knowledge_context_bundle(bundle)
    return bundle


def _context_item(hit: KnowledgeRetrievalHit, content_hash: str,
                  temporal_class) -> KnowledgeContextItem:
    lane = (
        KnowledgeContextLane.HISTORICALLY_KNOWN
        if hit.known_by_historical_as_of
        else KnowledgeContextLane.RETROSPECTIVE_AFTER_EVENT
    )
    return KnowledgeContextItem(
        schema_version=KNOWLEDGE_CONTEXT_ITEM_SCHEMA_VERSION,
        lane=lane,
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        document_family_id=hit.document_family_id,
        document_version=hit.document_version,
        retrieval_rank=hit.rank,
        retrieval_score=hit.score,
        content=hit.content,
        content_hash=content_hash,
        content_char_count=len(hit.content),
        source=hit.source,
        source_type=hit.source_type,
        title=hit.title,
        published_at=hit.published_at,
        available_at=hit.available_at,
        effective_from=hit.effective_from,
        effective_to=hit.effective_to,
        temporal_class=temporal_class,
        entity_refs=hit.entity_refs,
        provenance_source_identifier=hit.provenance_source_identifier,
        origin_reference=hit.origin_reference,
        known_by_historical_as_of=hit.known_by_historical_as_of,
        knowledge_after_event=hit.knowledge_after_event,
    )
