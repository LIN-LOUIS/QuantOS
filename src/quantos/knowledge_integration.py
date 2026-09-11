"""Deterministic per-candidate Knowledge preparation for guarded synthesis.

This integration layer consumes prebuilt local Knowledge artifacts.  It never
ingests documents, rebuilds indexes, retrieves from a network, or publishes a
product report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Sequence

from quantos.config import (
    KNOWLEDGE_QUERY_STRATEGY_VERSION,
    KnowledgeIntegrationSettings,
)
from quantos.knowledge_context import (
    KnowledgeContextAssemblyError,
    assemble_knowledge_context,
)
from quantos.knowledge_retrieval import KnowledgeRetrievalError, retrieve_knowledge
from quantos.schemas._validation import require_aware, require_non_empty
from quantos.schemas.knowledge import KnowledgeQueryMode
from quantos.schemas.knowledge_context import KnowledgeContextBundle, KnowledgeContextPolicy
from quantos.schemas.knowledge_retrieval import (
    KnowledgeRetrievalErrorCode,
    KnowledgeRetrievalRequest,
    KnowledgeRetrievalResult,
    normalize_lexical_query,
)
from quantos.schemas.synthesis import SynthesisInput
from quantos.storage.knowledge import (
    KnowledgeCorruptionError,
    KnowledgeRepository,
    KnowledgeStorageError,
)
from quantos.storage.knowledge_context import (
    KnowledgeContextCollisionError,
    KnowledgeContextCorruptionError,
    KnowledgeContextRepository,
    KnowledgeContextStorageError,
)
from quantos.storage.knowledge_index import (
    KnowledgeIndexCorruptionError,
    KnowledgeIndexNotFoundError,
    KnowledgeIndexStaleError,
    KnowledgeIndexStorageError,
    KnowledgeLexicalIndexRepository,
)
from quantos.synthesis import build_synthesis_input


class KnowledgePreparationStatus(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    EMPTY = "EMPTY"
    READY = "READY"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    CORRUPT = "CORRUPT"
    FAILED = "FAILED"
    FAILED_INTEGRITY = "FAILED_INTEGRITY"


class KnowledgePreparationError(RuntimeError):
    """A configured Knowledge dependency failed before synthesis invocation."""

    def __init__(self, status: KnowledgePreparationStatus, safe_reason_code: str) -> None:
        if status in {
            KnowledgePreparationStatus.NOT_CONFIGURED,
            KnowledgePreparationStatus.EMPTY,
            KnowledgePreparationStatus.READY,
        }:
            raise ValueError("knowledge preparation error requires a hard-failure status")
        self.status = status
        self.safe_reason_code = safe_reason_code
        super().__init__(safe_reason_code)


@dataclass(frozen=True, slots=True)
class KnowledgePreparationResult:
    status: KnowledgePreparationStatus
    synthesis_input: SynthesisInput
    query_strategy_version: str
    query: str | None = None
    retrieval_request: KnowledgeRetrievalRequest | None = None
    retrieval_result: KnowledgeRetrievalResult | None = None
    knowledge_context: KnowledgeContextBundle | None = None
    context_path: Path | None = None
    safe_reason_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, KnowledgePreparationStatus):
            raise ValueError("invalid knowledge preparation status")
        if not isinstance(self.synthesis_input, SynthesisInput):
            raise ValueError("knowledge preparation requires a synthesis input")
        if self.query_strategy_version != KNOWLEDGE_QUERY_STRATEGY_VERSION:
            raise ValueError("unsupported knowledge query strategy")
        artifacts = (
            self.query,
            self.retrieval_request,
            self.retrieval_result,
            self.knowledge_context,
            self.context_path,
        )
        if self.status is KnowledgePreparationStatus.NOT_CONFIGURED:
            if any(item is not None for item in artifacts):
                raise ValueError("disabled knowledge cannot retain preparation artifacts")
            if self.synthesis_input.knowledge_context is not None:
                raise ValueError("disabled knowledge must use the legacy synthesis channel")
        elif self.status in {KnowledgePreparationStatus.EMPTY, KnowledgePreparationStatus.READY}:
            if any(item is None for item in artifacts):
                raise ValueError("configured knowledge requires complete preparation artifacts")
            if self.synthesis_input.knowledge_context != self.knowledge_context:
                raise ValueError("synthesis input does not reference prepared context")
            selected = self.knowledge_context.selected_item_count
            if (self.status is KnowledgePreparationStatus.EMPTY) != (selected == 0):
                raise ValueError("knowledge preparation status disagrees with context")
        else:
            raise ValueError("hard failures must be represented by KnowledgePreparationError")

    @property
    def context_id(self) -> str | None:
        return self.knowledge_context.context_id if self.knowledge_context is not None else None

    @property
    def retrieval_request_id(self) -> str | None:
        return (
            self.retrieval_result.retrieval_request_id
            if self.retrieval_result is not None else None
        )


def candidate_subject_query(*, company_name: str, sector_name: str | None, symbol: str) -> str:
    """Build the frozen candidate_subject:v1 query in one stable field order."""
    require_non_empty(company_name, "company_name")
    require_non_empty(symbol, "symbol")
    if sector_name is not None:
        require_non_empty(sector_name, "sector_name")
    return normalize_lexical_query(" ".join(
        item for item in (company_name, sector_name, symbol) if item is not None
    ))


def prepare_candidate_synthesis_input(
    attribution_bundle,
    attribution_facts: Sequence,
    *,
    mode: str,
    company_name: str,
    market_event_time: datetime,
    research_corpus_cutoff: datetime | None,
    generated_at: datetime,
    settings: KnowledgeIntegrationSettings,
    knowledge_repository: KnowledgeRepository | None = None,
    index_repository: KnowledgeLexicalIndexRepository | None = None,
    context_repository: KnowledgeContextRepository | None = None,
) -> KnowledgePreparationResult:
    """Prepare one candidate input or fail closed for configured Knowledge errors."""
    if not isinstance(settings, KnowledgeIntegrationSettings):
        raise TypeError("KnowledgeIntegrationSettings required")
    try:
        require_aware(market_event_time, "market_event_time")
        require_aware(generated_at, "generated_at")
    except (AttributeError, ValueError) as error:
        _fail(
            KnowledgePreparationStatus.FAILED_INTEGRITY,
            "KNOWLEDGE_TIME_INVALID",
            error,
        )

    if not settings.enabled:
        synthesis_input = build_synthesis_input(
            attribution_bundle,
            attribution_facts,
            mode=mode,
            company_name=company_name,
            market_event_time=market_event_time,
        )
        return KnowledgePreparationResult(
            status=KnowledgePreparationStatus.NOT_CONFIGURED,
            synthesis_input=synthesis_input,
            query_strategy_version=KNOWLEDGE_QUERY_STRATEGY_VERSION,
            safe_reason_code="KNOWLEDGE_NOT_CONFIGURED",
        )

    query_mode = _query_mode(mode)
    cutoff = _research_cutoff(query_mode, market_event_time, research_corpus_cutoff)
    repository = (
        knowledge_repository if knowledge_repository is not None else KnowledgeRepository()
    )
    indexes = (
        index_repository if index_repository is not None else KnowledgeLexicalIndexRepository()
    )
    contexts = (
        context_repository if context_repository is not None else KnowledgeContextRepository()
    )
    if not repository.documents_root.is_dir():
        _fail(KnowledgePreparationStatus.UNAVAILABLE, "KNOWLEDGE_REPOSITORY_UNAVAILABLE")

    try:
        query = candidate_subject_query(
            company_name=company_name,
            sector_name=attribution_bundle.candidate.sector_name,
            symbol=attribution_bundle.candidate.symbol,
        )
        request = KnowledgeRetrievalRequest(
            query=query,
            mode=query_mode,
            as_of_time=market_event_time,
            corpus_cutoff=cutoff,
            entity_refs=(attribution_bundle.candidate.symbol,),
            effective_at=market_event_time,
            limit=settings.retrieval_limit,
        )
        policy = KnowledgeContextPolicy(
            max_items=settings.context_max_items,
            max_context_chars=settings.context_max_chars,
            allow_retrospective_research_context=query_mode is KnowledgeQueryMode.RESEARCH,
        )
    except (AttributeError, TypeError, ValueError) as error:
        _fail(KnowledgePreparationStatus.FAILED_INTEGRITY,
              "KNOWLEDGE_REQUEST_CONSTRUCTION_FAILED", error)

    try:
        index = indexes.load(settings.lexical_index_version)
        retrieval_result = retrieve_knowledge(
            repository, index, request, generated_at=generated_at,
        )
        context = assemble_knowledge_context(
            repository,
            index,
            request,
            retrieval_result,
            policy,
            generated_at=generated_at,
        )
    except KnowledgeIndexNotFoundError as error:
        _fail(KnowledgePreparationStatus.UNAVAILABLE, "KNOWLEDGE_INDEX_UNAVAILABLE", error)
    except KnowledgeIndexStaleError as error:
        _fail(KnowledgePreparationStatus.STALE, "KNOWLEDGE_INDEX_STALE", error)
    except (KnowledgeIndexCorruptionError, KnowledgeCorruptionError) as error:
        _fail(KnowledgePreparationStatus.CORRUPT, "KNOWLEDGE_ARTIFACT_CORRUPT", error)
    except KnowledgeRetrievalError as error:
        _fail_retrieval(error)
    except KnowledgeContextAssemblyError as error:
        _fail(KnowledgePreparationStatus.FAILED, "KNOWLEDGE_CONTEXT_ASSEMBLY_FAILED", error)
    except (KnowledgeStorageError, KnowledgeIndexStorageError) as error:
        _fail(KnowledgePreparationStatus.FAILED, "KNOWLEDGE_STORAGE_FAILED", error)
    except Exception as error:
        _fail(KnowledgePreparationStatus.FAILED, "KNOWLEDGE_PREPARATION_FAILED", error)

    try:
        synthesis_input = build_synthesis_input(
            attribution_bundle,
            attribution_facts,
            mode=mode,
            company_name=company_name,
            market_event_time=market_event_time,
            knowledge_context=context,
        )
    except (TypeError, ValueError) as error:
        _fail(KnowledgePreparationStatus.FAILED_INTEGRITY,
              "KNOWLEDGE_INTEGRITY_VALIDATION_FAILED", error)
    except Exception as error:
        _fail(KnowledgePreparationStatus.FAILED, "KNOWLEDGE_PREPARATION_FAILED", error)

    try:
        context_path = contexts.store(context)
    except (KnowledgeContextCorruptionError, KnowledgeContextCollisionError) as error:
        _fail(KnowledgePreparationStatus.CORRUPT, "KNOWLEDGE_ARTIFACT_CORRUPT", error)
    except KnowledgeContextStorageError as error:
        _fail(KnowledgePreparationStatus.FAILED, "KNOWLEDGE_STORAGE_FAILED", error)
    except Exception as error:
        _fail(KnowledgePreparationStatus.FAILED, "KNOWLEDGE_STORAGE_FAILED", error)

    status = (
        KnowledgePreparationStatus.EMPTY
        if context.selected_item_count == 0 else KnowledgePreparationStatus.READY
    )
    return KnowledgePreparationResult(
        status=status,
        synthesis_input=synthesis_input,
        query_strategy_version=KNOWLEDGE_QUERY_STRATEGY_VERSION,
        query=query,
        retrieval_request=request,
        retrieval_result=retrieval_result,
        knowledge_context=context,
        context_path=context_path,
        safe_reason_code=("KNOWLEDGE_CONTEXT_EMPTY" if status is KnowledgePreparationStatus.EMPTY
                          else "KNOWLEDGE_CONTEXT_READY"),
    )


def _query_mode(mode: str) -> KnowledgeQueryMode:
    try:
        return KnowledgeQueryMode(mode)
    except ValueError as error:
        _fail(KnowledgePreparationStatus.FAILED_INTEGRITY, "KNOWLEDGE_MODE_MISMATCH", error)


def _research_cutoff(
    mode: KnowledgeQueryMode,
    market_event_time: datetime,
    value: datetime | None,
) -> datetime | None:
    if mode is KnowledgeQueryMode.STRICT_LIVE:
        if value is not None:
            _fail(KnowledgePreparationStatus.FAILED_INTEGRITY,
                  "STRICT_KNOWLEDGE_CUTOFF_MUST_BE_NONE")
        return None
    if value is None:
        _fail(KnowledgePreparationStatus.FAILED_INTEGRITY,
              "RESEARCH_KNOWLEDGE_CUTOFF_REQUIRED")
    try:
        require_aware(value, "research_corpus_cutoff")
    except ValueError as error:
        _fail(KnowledgePreparationStatus.FAILED_INTEGRITY,
              "RESEARCH_KNOWLEDGE_CUTOFF_INVALID", error)
    if value < market_event_time:
        _fail(KnowledgePreparationStatus.FAILED_INTEGRITY,
              "RESEARCH_KNOWLEDGE_CUTOFF_BEFORE_EVENT")
    return value


def _fail_retrieval(error: KnowledgeRetrievalError) -> None:
    if error.code in {
        KnowledgeRetrievalErrorCode.STALE_INDEX,
        KnowledgeRetrievalErrorCode.CHUNK_MANIFEST_MISMATCH,
        KnowledgeRetrievalErrorCode.TOKENIZER_MISMATCH,
    }:
        _fail(KnowledgePreparationStatus.STALE, "KNOWLEDGE_INDEX_STALE", error)
    if error.code is KnowledgeRetrievalErrorCode.CORRUPT_INDEX:
        _fail(KnowledgePreparationStatus.CORRUPT, "KNOWLEDGE_INDEX_CORRUPT", error)
    _fail(KnowledgePreparationStatus.FAILED_INTEGRITY,
          "KNOWLEDGE_RETRIEVAL_CONTRACT_FAILED", error)


def _fail(
    status: KnowledgePreparationStatus,
    safe_reason_code: str,
    cause: Exception | None = None,
) -> None:
    error = KnowledgePreparationError(status, safe_reason_code)
    if cause is None:
        raise error
    raise error from cause
