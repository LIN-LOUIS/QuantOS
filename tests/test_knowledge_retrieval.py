"""Deterministic PIT-safe lexical retrieval contracts for Phase 4C."""

from dataclasses import replace
from datetime import datetime, timedelta
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantos.knowledge_retrieval import (
    KnowledgeRetrievalError,
    build_knowledge_lexical_index,
    retrieve_knowledge,
    tokenize_lexical,
)
from quantos.schemas.knowledge import (
    KnowledgeDocumentInput,
    KnowledgeProvenance,
    KnowledgeQueryMode,
    KnowledgeSourceType,
    KnowledgeTemporalClass,
    canonicalize_knowledge_document,
)
from quantos.schemas.knowledge_retrieval import (
    BM25_B,
    BM25_K1,
    MAX_RETRIEVAL_LIMIT,
    KnowledgeLexicalIndexEntry,
    KnowledgeRetrievalErrorCode,
    KnowledgeRetrievalRequest,
    knowledge_lexical_index_version,
)
from quantos.storage.knowledge import (
    KnowledgeCorruptionError, KnowledgeNotFoundError, KnowledgeRepository,
)


SH = ZoneInfo("Asia/Shanghai")
HISTORICAL = datetime(2026, 9, 1, 15, tzinfo=SH)
BUILT_AT = datetime(2026, 9, 8, 12, tzinfo=SH)


def document(
    family: str,
    content: str,
    *,
    available: datetime = HISTORICAL,
    source_type: KnowledgeSourceType = KnowledgeSourceType.REFERENCE_DOCUMENT,
    temporal_class: KnowledgeTemporalClass = KnowledgeTemporalClass.TIMELESS,
    entity_refs: tuple[str, ...] = (),
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
):
    return canonicalize_knowledge_document(KnowledgeDocumentInput(
        document_family_id=family,
        document_version=1,
        source="official-source",
        source_type=source_type,
        title=f"Title {family}",
        content=content,
        temporal_class=temporal_class,
        published_at=available - timedelta(days=1),
        available_at=available,
        retrieved_at=available,
        effective_from=effective_from,
        effective_to=effective_to,
        entity_refs=entity_refs,
        provenance=KnowledgeProvenance(
            source_identifier=f"artifact:{family}",
            ingestion_adapter="fixture",
            ingestion_adapter_version="v1",
            origin_reference=f"https://example.invalid/{family}",
        ),
    ))


def repository(tmp_path: Path, documents) -> KnowledgeRepository:
    value = KnowledgeRepository(tmp_path)
    for item in documents:
        value.store(item)
    return value


def strict_request(query="alpha", **changes):
    values = {
        "query": query,
        "mode": KnowledgeQueryMode.STRICT_LIVE,
        "as_of_time": HISTORICAL,
    }
    values.update(changes)
    return KnowledgeRetrievalRequest(**values)


def research_request(query="alpha", **changes):
    values = {
        "query": query,
        "mode": KnowledgeQueryMode.RESEARCH,
        "as_of_time": HISTORICAL,
        "corpus_cutoff": BUILT_AT,
    }
    values.update(changes)
    return KnowledgeRetrievalRequest(**values)


def search(repo, request, generated_at=BUILT_AT):
    index = build_knowledge_lexical_index(repo, built_at=BUILT_AT)
    return retrieve_knowledge(repo, index, request, generated_at=generated_at)


def test_tokenizer_contract_is_local_mixed_lexical_v1():
    assert tokenize_lexical("ABC abc 600519.SH 贵州，茅台 A股") == (
        "abc", "abc", "600519", "sh", "贵州", "茅台", "a", "股",
    )
    assert tokenize_lexical("甲") == ("甲",)
    assert tokenize_lexical("甲乙丙") == ("甲乙", "乙丙")
    assert tokenize_lexical("--，。") == ()


def test_tokenizer_normalizes_case_and_unicode_nfc():
    assert tokenize_lexical("CAFÉ cafe\N{COMBINING ACUTE ACCENT}") == ("caf", "caf")
    assert tokenize_lexical("Ticker_600519") == ("ticker", "600519")


def test_basic_lexical_hit_and_no_hit(tmp_path):
    repo = repository(tmp_path, (document("knowledge:a", "alpha beta"),))
    result = search(repo, strict_request())
    assert len(result.hits) == 1
    assert result.hits[0].content == "alpha beta"
    assert result.hits[0].rank == 1 and result.hits[0].score > 0
    assert search(repo, strict_request("absent")).hits == ()


def test_bm25_contract_parameters_and_term_frequency(tmp_path):
    assert (BM25_K1, BM25_B) == (1.5, 0.75)
    frequent = document("knowledge:a", "alpha alpha alpha")
    sparse = document("knowledge:b", "alpha beta gamma delta epsilon")
    result = search(repository(tmp_path, (sparse, frequent)), strict_request())
    assert tuple(hit.document_family_id for hit in result.hits) == (
        "knowledge:a", "knowledge:b",
    )
    assert result.hits[0].score > result.hits[1].score


def test_single_document_bm25_score_matches_okapi_formula(tmp_path):
    repo = repository(tmp_path, (document("knowledge:a", "alpha alpha beta"),))
    score = search(repo, strict_request()).hits[0].score
    inverse_frequency = math.log(1.0 + 0.5 / 1.5)
    expected = inverse_frequency * 2.0 * (BM25_K1 + 1.0) / (2.0 + BM25_K1)
    assert math.isclose(score, expected, rel_tol=0.0, abs_tol=1e-15)


def test_repeated_query_term_does_not_change_bm25_hits(tmp_path):
    repo = repository(tmp_path, (
        document("knowledge:a", "alpha alpha"),
        document("knowledge:b", "alpha beta"),
    ))
    once = search(repo, strict_request("alpha"))
    repeated = search(repo, strict_request("alpha alpha alpha"))
    assert tuple((hit.document_id, hit.score) for hit in once.hits) == tuple(
        (hit.document_id, hit.score) for hit in repeated.hits
    )


def test_empty_eligible_corpus_is_a_valid_zero_hit_result(tmp_path):
    repo = repository(tmp_path, (
        document("knowledge:future", "alpha", available=HISTORICAL + timedelta(seconds=1)),
    ))
    result = search(repo, strict_request())
    assert result.eligible_chunk_count == 0 and result.hits == ()


def test_strict_cutoff_is_inclusive_and_future_is_absent(tmp_path):
    before = document("knowledge:before", "alpha", available=HISTORICAL - timedelta(seconds=1))
    equal = document("knowledge:equal", "alpha", available=HISTORICAL)
    future = document("knowledge:future", "alpha", available=HISTORICAL + timedelta(seconds=1))
    result = search(repository(tmp_path, (future, equal, before)), strict_request())
    assert {hit.document_family_id for hit in result.hits} == {
        "knowledge:before", "knowledge:equal",
    }
    assert all(hit.known_by_historical_as_of for hit in result.hits)
    assert all(not hit.knowledge_after_event for hit in result.hits)


def test_research_cutoff_boundaries_and_timing_flags(tmp_path):
    documents = (
        document("knowledge:before", "alpha", available=HISTORICAL - timedelta(seconds=1)),
        document("knowledge:equal-history", "alpha", available=HISTORICAL),
        document("knowledge:after-history", "alpha", available=HISTORICAL + timedelta(seconds=1)),
        document("knowledge:equal-corpus", "alpha", available=BUILT_AT),
        document("knowledge:after-corpus", "alpha", available=BUILT_AT + timedelta(seconds=1)),
    )
    result = search(repository(tmp_path, tuple(reversed(documents))), research_request())
    hits = {hit.document_family_id: hit for hit in result.hits}
    assert set(hits) == {
        "knowledge:before", "knowledge:equal-history",
        "knowledge:after-history", "knowledge:equal-corpus",
    }
    assert hits["knowledge:before"].known_by_historical_as_of
    assert hits["knowledge:equal-history"].known_by_historical_as_of
    assert hits["knowledge:after-history"].knowledge_after_event
    assert hits["knowledge:equal-corpus"].knowledge_after_event


def test_future_documents_do_not_change_historical_bm25_statistics(tmp_path):
    historical = document("knowledge:historical", "alpha beta")
    baseline_repo = repository(tmp_path / "baseline", (historical,))
    future_documents = tuple(
        document(
            f"knowledge:future-{index}", "alpha " * (index + 10),
            available=HISTORICAL + timedelta(days=1),
        ) for index in range(8)
    )
    expanded_repo = repository(tmp_path / "expanded", (historical, *future_documents))
    baseline = search(baseline_repo, strict_request())
    expanded = search(expanded_repo, strict_request())
    assert tuple((hit.document_id, hit.score) for hit in baseline.hits) == tuple(
        (hit.document_id, hit.score) for hit in expanded.hits
    )


def test_metadata_filters_are_anded_and_values_within_filter_are_ored(tmp_path):
    effective = HISTORICAL - timedelta(days=10)
    first = document(
        "knowledge:a", "alpha", source_type=KnowledgeSourceType.COMPANY_PROFILE,
        temporal_class=KnowledgeTemporalClass.SEMI_STATIC,
        entity_refs=("000001.SZ", "600519.SH"), effective_from=effective,
    )
    second = document(
        "knowledge:b", "alpha", source_type=KnowledgeSourceType.RESEARCH_REPORT,
        temporal_class=KnowledgeTemporalClass.TIME_BOUNDED,
        entity_refs=("600519.SH",), effective_from=effective,
        effective_to=HISTORICAL + timedelta(days=1),
    )
    third = document("knowledge:c", "alpha")
    repo = repository(tmp_path, (third, second, first))
    request = strict_request(
        entity_refs=("000001.SZ", "999999.SH"),
        source_types=(KnowledgeSourceType.COMPANY_PROFILE, KnowledgeSourceType.ANNUAL_REPORT),
        temporal_classes=(KnowledgeTemporalClass.SEMI_STATIC,),
        document_family_ids=("knowledge:a", "knowledge:missing"),
        effective_at=HISTORICAL,
    )
    result = search(repo, request)
    assert tuple(hit.document_family_id for hit in result.hits) == ("knowledge:a",)


def test_filter_value_order_does_not_change_request_or_results(tmp_path):
    first = document("knowledge:a", "alpha", entity_refs=("000001.SZ", "600519.SH"))
    repo = repository(tmp_path, (first,))
    left_request = strict_request(entity_refs=("600519.SH", "000001.SZ"))
    right_request = strict_request(entity_refs=("000001.SZ", "600519.SH"))
    left = search(repo, left_request)
    right = search(repo, right_request)
    assert left_request == right_request
    assert left.retrieval_request_id == right.retrieval_request_id
    assert left.hits == right.hits


def test_effective_time_boundaries_are_inclusive(tmp_path):
    start = HISTORICAL - timedelta(days=1)
    end = HISTORICAL + timedelta(days=1)
    bounded = document(
        "knowledge:bounded", "alpha",
        temporal_class=KnowledgeTemporalClass.TIME_BOUNDED,
        effective_from=start, effective_to=end,
    )
    repo = repository(tmp_path, (bounded,))
    assert search(repo, strict_request(effective_at=start)).hits
    assert search(repo, strict_request(effective_at=end)).hits
    assert not search(repo, strict_request(effective_at=start - timedelta(seconds=1))).hits
    assert not search(repo, strict_request(effective_at=end + timedelta(seconds=1))).hits


def test_metadata_filtering_happens_before_bm25_statistics(tmp_path):
    included = document(
        "knowledge:included", "alpha beta",
        source_type=KnowledgeSourceType.COMPANY_PROFILE,
        temporal_class=KnowledgeTemporalClass.SEMI_STATIC,
        effective_from=HISTORICAL - timedelta(days=1), entity_refs=("600519.SH",),
    )
    excluded = tuple(
        document(f"knowledge:excluded-{index}", "alpha " * 20)
        for index in range(5)
    )
    baseline = repository(tmp_path / "baseline", (included,))
    expanded = repository(tmp_path / "expanded", (included, *excluded))
    request = strict_request(source_types=(KnowledgeSourceType.COMPANY_PROFILE,))
    left = search(baseline, request)
    right = search(expanded, request)
    assert tuple((hit.document_id, hit.score) for hit in left.hits) == tuple(
        (hit.document_id, hit.score) for hit in right.hits
    )


def test_exact_score_ties_have_stable_order_and_limit_after_ranking(tmp_path):
    first = document("knowledge:a", "alpha")
    second = document("knowledge:b", "alpha")
    left_repo = repository(tmp_path / "left", (second, first))
    right_repo = repository(tmp_path / "right", (first, second))
    left = search(left_repo, strict_request(limit=1))
    right = search(right_repo, strict_request(limit=1))
    assert left.hits[0].score == search(left_repo, strict_request(limit=2)).hits[1].score
    assert left.hits[0].document_family_id == right.hits[0].document_family_id == "knowledge:a"


def test_retrieval_hit_traverses_to_full_canonical_provenance(tmp_path):
    original = document("knowledge:a", "alpha")
    repo = repository(tmp_path, (original,))
    hit = search(repo, strict_request()).hits[0]
    recovered = repo.get_document(hit.document_id)
    assert recovered == original
    assert recovered.provenance.source_identifier == hit.provenance_source_identifier
    assert recovered.provenance.origin_reference == hit.origin_reference
    assert recovered.provenance.ingestion_adapter == "fixture"
    assert recovered.provenance.ingestion_adapter_version == "v1"


def test_unknown_document_lookup_is_explicit_not_found(tmp_path):
    with pytest.raises(KnowledgeNotFoundError):
        KnowledgeRepository(tmp_path).get_document("0" * 64)


def test_document_lookup_fails_closed_on_corrupt_repository(tmp_path):
    original = document("knowledge:a", "alpha")
    repo = repository(tmp_path, (original,))
    next(repo.documents_root.rglob("*.json")).write_text("{", encoding="utf-8")
    with pytest.raises(KnowledgeCorruptionError):
        repo.get_document(original.document_id)


def test_query_rejects_stale_index_without_rebuilding(tmp_path):
    repo = repository(tmp_path, (document("knowledge:a", "alpha"),))
    index = build_knowledge_lexical_index(repo, built_at=BUILT_AT)
    repo.store(document("knowledge:b", "alpha"))
    with pytest.raises(KnowledgeRetrievalError) as captured:
        retrieve_knowledge(repo, index, strict_request(), generated_at=BUILT_AT)
    assert captured.value.code is KnowledgeRetrievalErrorCode.STALE_INDEX


def test_query_rejects_index_metadata_tampering_that_could_bypass_pit(tmp_path):
    repo = repository(tmp_path, (
        document(
            "knowledge:future", "alpha",
            available=HISTORICAL + timedelta(days=1),
        ),
    ))
    index = build_knowledge_lexical_index(repo, built_at=BUILT_AT)
    entry = index.entries[0]
    altered_entry = replace(
        entry, chunk=replace(entry.chunk, available_at=HISTORICAL),
    )
    altered_index = replace(index, entries=(altered_entry,))
    assert altered_index.lexical_index_version == index.lexical_index_version

    with pytest.raises(KnowledgeRetrievalError) as captured:
        retrieve_knowledge(
            repo, altered_index, strict_request(), generated_at=BUILT_AT,
        )
    assert captured.value.code is KnowledgeRetrievalErrorCode.CORRUPT_INDEX


def test_query_rejects_tokens_not_derived_from_canonical_chunk(tmp_path):
    repo = repository(tmp_path, (document("knowledge:a", "beta"),))
    index = build_knowledge_lexical_index(repo, built_at=BUILT_AT)
    altered_entries = (
        KnowledgeLexicalIndexEntry(chunk=index.entries[0].chunk, tokens=("alpha",)),
    )
    altered_index = replace(
        index,
        lexical_index_version=knowledge_lexical_index_version(
            corpus_version=index.corpus_version,
            chunk_manifest_version=index.chunk_manifest_version,
            entries=altered_entries,
        ),
        entries=altered_entries,
    )

    with pytest.raises(KnowledgeRetrievalError) as captured:
        retrieve_knowledge(
            repo, altered_index, strict_request(), generated_at=BUILT_AT,
        )
    assert captured.value.code is KnowledgeRetrievalErrorCode.CORRUPT_INDEX


def test_retrieval_is_read_only_and_never_rebuilds_index(tmp_path):
    repo = repository(tmp_path / "documents", (document("knowledge:a", "alpha"),))
    index = build_knowledge_lexical_index(repo, built_at=BUILT_AT)
    before = tuple((path, path.read_bytes(), path.stat().st_mtime_ns)
                   for path in sorted(repo.root.rglob("*.json")))
    retrieve_knowledge(repo, index, strict_request(), generated_at=BUILT_AT)
    after = tuple((path, path.read_bytes(), path.stat().st_mtime_ns)
                  for path in sorted(repo.root.rglob("*.json")))
    assert after == before


def test_request_and_result_identity_exclude_generation_time(tmp_path):
    repo = repository(tmp_path, (document("knowledge:a", "alpha"),))
    index = build_knowledge_lexical_index(repo, built_at=BUILT_AT)
    request = strict_request("  alpha   ")
    first = retrieve_knowledge(repo, index, request, generated_at=BUILT_AT)
    second = retrieve_knowledge(
        repo, index, request, generated_at=BUILT_AT + timedelta(hours=1),
    )
    assert request.query == "alpha"
    assert first.retrieval_request_id == second.retrieval_request_id
    assert first.hits == second.hits


@pytest.mark.parametrize("changes", [
    {"corpus_cutoff": BUILT_AT},
    {"as_of_time": datetime(2026, 9, 1, 15)},
    {"effective_at": datetime(2026, 9, 1, 15)},
    {"limit": 0},
    {"limit": MAX_RETRIEVAL_LIMIT + 1},
    {"tokenizer_version": "v2"},
    {"entity_refs": ("600519",)},
    {"document_family_ids": (" bad ",)},
])
def test_strict_request_rejects_invalid_contract(changes):
    with pytest.raises(ValueError):
        strict_request(**changes)


@pytest.mark.parametrize("changes", [
    {"corpus_cutoff": None},
    {"corpus_cutoff": datetime(2026, 9, 8, 12)},
    {"corpus_cutoff": HISTORICAL - timedelta(seconds=1)},
])
def test_research_request_rejects_invalid_cutoff(changes):
    with pytest.raises(ValueError):
        research_request(**changes)


def test_empty_or_tokenless_query_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        strict_request("  ")
    repo = repository(tmp_path, (document("knowledge:a", "alpha"),))
    with pytest.raises(KnowledgeRetrievalError) as captured:
        search(repo, strict_request("---"))
    assert captured.value.code is KnowledgeRetrievalErrorCode.EMPTY_QUERY_TOKENS
