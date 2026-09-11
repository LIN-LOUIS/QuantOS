"""Phase 4D deterministic PIT-safe knowledge context assembly tests."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantos.knowledge_context import (
    KnowledgeContextAssemblyError,
    assemble_knowledge_context,
)
from quantos.knowledge_retrieval import (
    build_knowledge_lexical_index,
    retrieve_knowledge,
)
from quantos.schemas.knowledge import (
    KnowledgeDocumentInput,
    KnowledgeProvenance,
    KnowledgeQueryMode,
    KnowledgeSourceType,
    KnowledgeTemporalClass,
    canonicalize_knowledge_document,
)
from quantos.schemas.knowledge_context import (
    CONTEXT_SELECTION_ALGORITHM,
    CONTEXT_SELECTION_VERSION,
    KnowledgeContextBundle,
    KnowledgeContextItem,
    KnowledgeContextLane,
    KnowledgeContextPolicy,
    KnowledgeContextStopReason,
)
from quantos.schemas.knowledge_retrieval import KnowledgeRetrievalRequest
from quantos.storage.knowledge import KnowledgeRepository


SH = ZoneInfo("Asia/Shanghai")
HISTORICAL = datetime(2026, 9, 1, 15, tzinfo=SH)
GENERATED = datetime(2026, 9, 8, 12, tzinfo=SH)


def document(family, content, *, available=HISTORICAL):
    return canonicalize_knowledge_document(KnowledgeDocumentInput(
        document_family_id=family,
        document_version=1,
        source="official-source",
        source_type=KnowledgeSourceType.REFERENCE_DOCUMENT,
        title=f"Title {family}",
        content=content,
        temporal_class=KnowledgeTemporalClass.TIMELESS,
        published_at=available - timedelta(days=1),
        available_at=available,
        retrieved_at=available,
        effective_from=None,
        effective_to=None,
        entity_refs=("600519.SH",),
        provenance=KnowledgeProvenance(
            source_identifier=f"artifact:{family}",
            ingestion_adapter="fixture",
            ingestion_adapter_version="v1",
            origin_reference=f"https://example.invalid/{family}",
        ),
    ))


def artifacts(tmp_path: Path, documents, *, mode=KnowledgeQueryMode.STRICT_LIVE,
              query="alpha", limit=10):
    repository = KnowledgeRepository(tmp_path / "documents")
    for value in documents:
        repository.store(value)
    index = build_knowledge_lexical_index(repository, built_at=GENERATED)
    request = KnowledgeRetrievalRequest(
        query=query,
        mode=mode,
        as_of_time=HISTORICAL,
        corpus_cutoff=GENERATED if mode is KnowledgeQueryMode.RESEARCH else None,
        limit=limit,
    )
    result = retrieve_knowledge(repository, index, request, generated_at=GENERATED)
    return repository, index, request, result


def policy(*, max_items=10, max_chars=10_000, retrospective=False):
    return KnowledgeContextPolicy(
        max_items=max_items,
        max_context_chars=max_chars,
        allow_retrospective_research_context=retrospective,
    )


def assemble(values, selected_policy=None, generated_at=GENERATED):
    repository, index, request, result = values
    return assemble_knowledge_context(
        repository, index, request, result,
        selected_policy or policy(), generated_at=generated_at,
    )


def test_policy_and_basic_context_contract(tmp_path):
    values = artifacts(tmp_path, (document("knowledge:a", "alpha beta"),))
    bundle = assemble(values)
    assert (bundle.policy.selection_algorithm, bundle.policy.selection_version) == (
        CONTEXT_SELECTION_ALGORITHM, CONTEXT_SELECTION_VERSION,
    ) == ("rank_prefix", "v1")
    assert bundle.selected_item_count == bundle.historical_item_count == 1
    assert bundle.retrospective_item_count == bundle.omitted_hit_count == 0
    assert bundle.selection_stop_reason is KnowledgeContextStopReason.ALL_SELECTED
    assert bundle.selected_items == bundle.historical_items


@pytest.mark.parametrize("changes", [
    {"max_items": 0}, {"max_items": 101}, {"max_context_chars": 0},
    {"allow_retrospective_research_context": 1},
    {"selection_version": "v2"}, {"selection_algorithm": "skip_shorter"},
])
def test_policy_rejects_invalid_values(changes):
    values = {
        "max_items": 10,
        "max_context_chars": 1000,
        "allow_retrospective_research_context": False,
    }
    values.update(changes)
    with pytest.raises(ValueError):
        KnowledgeContextPolicy(**values)


def test_max_items_applies_after_retrieval_rank(tmp_path):
    values = artifacts(tmp_path, (
        document("knowledge:b", "alpha"), document("knowledge:a", "alpha"),
    ))
    bundle = assemble(values, policy(max_items=1))
    assert bundle.selection_stop_reason is KnowledgeContextStopReason.MAX_ITEMS
    assert tuple(item.document_family_id for item in bundle.selected_items) == ("knowledge:a",)
    assert bundle.omitted_hit_count == 1


def test_exact_character_budget_and_unicode_code_points(tmp_path):
    content = "alpha贵州"
    values = artifacts(tmp_path, (document("knowledge:a", content),))
    exact = assemble(values, policy(max_chars=len(content)))
    assert exact.selected_char_count == len(content) == exact.historical_items[0].content_char_count
    assert exact.selection_stop_reason is KnowledgeContextStopReason.ALL_SELECTED
    too_small = assemble(values, policy(max_chars=len(content) - 1))
    assert too_small.selected_items == ()
    assert too_small.selection_stop_reason is KnowledgeContextStopReason.CHAR_BUDGET


def test_no_hits_and_budget_empty_are_distinct(tmp_path):
    empty = artifacts(tmp_path / "empty", (document("knowledge:a", "beta"),))
    no_hits = assemble(empty)
    assert no_hits.selection_stop_reason is KnowledgeContextStopReason.NO_HITS
    assert no_hits.retrieved_hit_count == no_hits.selected_item_count == 0
    blocked = artifacts(tmp_path / "blocked", (document("knowledge:a", "alpha"),))
    budget_empty = assemble(blocked, policy(max_chars=1))
    assert budget_empty.selection_stop_reason is KnowledgeContextStopReason.CHAR_BUDGET
    assert budget_empty.retrieved_hit_count == 1 and budget_empty.selected_item_count == 0


def test_rank_prefix_does_not_skip_oversized_hit_for_shorter_later_hit(tmp_path):
    values = artifacts(tmp_path, (
        document("knowledge:a", "alpha " * 20),
        document("knowledge:b", "alpha"),
    ))
    assert values[3].hits[0].document_family_id == "knowledge:a"
    bundle = assemble(values, policy(max_chars=10))
    assert bundle.selected_items == ()
    assert bundle.omitted_hit_count == 2
    assert bundle.selection_stop_reason is KnowledgeContextStopReason.CHAR_BUDGET


def test_context_never_truncates_or_changes_score_and_rank(tmp_path):
    values = artifacts(tmp_path, (document("knowledge:a", "alpha贵州"),))
    hit = values[3].hits[0]
    item = assemble(values).selected_items[0]
    assert item.content == hit.content
    assert item.retrieval_score == hit.score
    assert item.retrieval_rank == hit.rank


def test_generated_at_is_observational_not_context_identity(tmp_path):
    values = artifacts(tmp_path, (document("knowledge:a", "alpha"),))
    first = assemble(values)
    second = assemble(values, generated_at=GENERATED + timedelta(hours=1))
    assert first.context_id == second.context_id
    assert first.generated_at != second.generated_at
    assert first.selected_items == second.selected_items


def test_policy_changes_context_identity(tmp_path):
    values = artifacts(tmp_path, (document("knowledge:a", "alpha"),))
    first = assemble(values, policy(max_chars=100))
    second = assemble(values, policy(max_chars=101))
    assert first.context_id != second.context_id


def test_strict_context_has_only_historical_lane(tmp_path):
    values = artifacts(tmp_path, (document("knowledge:a", "alpha"),))
    bundle = assemble(values, policy(retrospective=True))
    assert bundle.historical_items
    assert bundle.retrospective_items == ()
    assert all(item.available_at <= bundle.as_of_time for item in bundle.historical_items)


def test_research_context_physically_separates_lanes_and_preserves_global_rank(tmp_path):
    values = artifacts(tmp_path, (
        document("knowledge:a", "alpha", available=HISTORICAL),
        document("knowledge:b", "alpha", available=HISTORICAL + timedelta(days=1)),
    ), mode=KnowledgeQueryMode.RESEARCH)
    bundle = assemble(values, policy(retrospective=True))
    assert tuple(item.retrieval_rank for item in bundle.historical_items) == (1,)
    assert tuple(item.retrieval_rank for item in bundle.retrospective_items) == (2,)
    assert tuple(item.retrieval_rank for item in bundle.selected_items) == (1, 2)
    assert bundle.historical_items[0].known_by_historical_as_of
    assert bundle.retrospective_items[0].knowledge_after_event


def test_historical_equality_is_known_and_research_after_event_is_retrospective(tmp_path):
    values = artifacts(tmp_path, (
        document("knowledge:a", "alpha", available=HISTORICAL),
        document("knowledge:b", "alpha", available=GENERATED),
    ), mode=KnowledgeQueryMode.RESEARCH)
    bundle = assemble(values, policy(retrospective=True))
    assert bundle.historical_items[0].available_at == HISTORICAL
    assert bundle.retrospective_items[0].available_at == GENERATED


def test_policy_can_stop_before_retrospective_without_skipping_it(tmp_path):
    values = artifacts(tmp_path, (
        document("knowledge:a", "alpha", available=HISTORICAL),
        document("knowledge:b", "alpha", available=HISTORICAL + timedelta(days=1)),
        document("knowledge:c", "alpha", available=HISTORICAL),
    ), mode=KnowledgeQueryMode.RESEARCH)
    bundle = assemble(values, policy(retrospective=False))
    assert tuple(item.retrieval_rank for item in bundle.selected_items) == (1,)
    assert bundle.selection_stop_reason is KnowledgeContextStopReason.RETROSPECTIVE_NOT_ALLOWED
    assert bundle.omitted_hit_count == 2


def test_context_item_traverses_to_full_canonical_provenance(tmp_path):
    original = document("knowledge:a", "alpha")
    values = artifacts(tmp_path, (original,))
    item = assemble(values).selected_items[0]
    recovered = values[0].get_document(item.document_id)
    assert recovered == original
    assert recovered.provenance.source_identifier == item.provenance_source_identifier
    assert recovered.provenance.ingestion_adapter == "fixture"
    assert recovered.provenance.ingestion_adapter_version == "v1"
    assert recovered.provenance.origin_reference == item.origin_reference


def test_context_contract_is_structured_immutable_and_not_evidence_or_prompt(tmp_path):
    bundle = assemble(artifacts(tmp_path, (document("knowledge:a", "alpha"),)))
    forbidden = {
        "evidence_id", "strict_attribution_eligible", "research_attribution_eligible",
        "attribution_score", "causal_score", "prompt", "messages", "model",
        "provider", "temperature",
    }
    assert forbidden.isdisjoint({value.name for value in fields(KnowledgeContextItem)})
    assert forbidden.isdisjoint({value.name for value in fields(KnowledgeContextBundle)})
    assert not isinstance(bundle, (str, list, dict))
    with pytest.raises(FrozenInstanceError):
        bundle.selected_item_count = 2


@pytest.mark.parametrize("mutation", [
    "available_at", "content", "chunk_id", "document_id", "pit_flags",
    "retrieval_request_id", "lexical_index_version",
])
def test_tampered_retrieval_result_fails_closed(tmp_path, mutation):
    values = artifacts(tmp_path, (document("knowledge:a", "alpha"),))
    repository, index, request, result = values
    hit = result.hits[0]
    if mutation == "available_at":
        hit = replace(hit, available_at=hit.available_at - timedelta(seconds=1))
        result = replace(result, hits=(hit,))
    elif mutation == "content":
        result = replace(result, hits=(replace(hit, content="tampered"),))
    elif mutation == "chunk_id":
        result = replace(result, hits=(replace(hit, chunk_id="0" * 64),))
    elif mutation == "document_id":
        result = replace(result, hits=(replace(hit, document_id="0" * 64),))
    elif mutation == "pit_flags":
        result = replace(result, hits=(replace(
            hit, known_by_historical_as_of=False, knowledge_after_event=True,
        ),))
    elif mutation == "retrieval_request_id":
        result = replace(result, retrieval_request_id="0" * 64)
    else:
        result = replace(result, lexical_index_version="0" * 64)
    with pytest.raises(KnowledgeContextAssemblyError):
        assemble_knowledge_context(
            repository, index, request, result, policy(), generated_at=GENERATED,
        )


def test_tampered_retrieval_rank_and_duplicate_chunk_fail_closed(tmp_path):
    values = artifacts(tmp_path, (
        document("knowledge:a", "alpha"), document("knowledge:b", "alpha"),
    ))
    repository, index, request, result = values
    swapped = (
        replace(result.hits[1], rank=1),
        replace(result.hits[0], rank=2),
    )
    duplicate = (result.hits[0], replace(result.hits[0], rank=2))
    for hits in (swapped, duplicate):
        with pytest.raises(KnowledgeContextAssemblyError):
            assemble_knowledge_context(
                repository, index, request, replace(result, hits=hits),
                policy(), generated_at=GENERATED,
            )


def test_assembly_is_read_only_and_does_not_persist_or_expand_retrieval(tmp_path):
    values = artifacts(tmp_path, (document("knowledge:a", "alpha"),))
    before = tuple(
        (path, path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(values[0].root.rglob("*.json"))
    )
    assemble(values)
    after = tuple(
        (path, path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(values[0].root.rglob("*.json"))
    )
    assert after == before
