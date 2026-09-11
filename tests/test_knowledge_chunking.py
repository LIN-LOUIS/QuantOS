from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
from zoneinfo import ZoneInfo

import pytest

from quantos.knowledge_chunking import (
    build_knowledge_chunk_manifest,
    chunk_knowledge_corpus,
    chunk_knowledge_document,
)
from quantos.schemas.knowledge import (
    KnowledgeDocumentInput,
    KnowledgeProvenance,
    KnowledgeSourceType,
    KnowledgeTemporalClass,
    build_knowledge_corpus_manifest,
    canonicalize_knowledge_document,
)
from quantos.schemas.knowledge_retrieval import (
    CHUNKING_ALGORITHM,
    CHUNKING_VERSION,
    CHUNK_SIZE_UNIT,
    MAX_CHUNK_SIZE,
    OVERLAP_SIZE,
    TARGET_CHUNK_SIZE,
    chunk_content_hash,
    knowledge_chunk_id,
)


SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 7, 12, tzinfo=SH)


def document(content="贵州茅台渠道库存下降。", *, family="knowledge:test:family", version=1,
             available=NOW, entity_refs=("600519.SH",)):
    return canonicalize_knowledge_document(KnowledgeDocumentInput(
        document_family_id=family,
        document_version=version,
        source="fixture",
        source_type=KnowledgeSourceType.COMPANY_PROFILE,
        title="Fixture",
        content=content,
        temporal_class=KnowledgeTemporalClass.SEMI_STATIC,
        published_at=NOW - timedelta(days=1),
        available_at=available,
        retrieved_at=available,
        effective_from=NOW - timedelta(days=30),
        effective_to=None,
        entity_refs=entity_refs,
        provenance=KnowledgeProvenance("fixture-id", "fixture", "v1"),
    ))


def manifest(documents, generated=NOW):
    return build_knowledge_corpus_manifest(tuple(documents), generated_at=generated)


def test_chunking_contract_constants_are_frozen():
    assert (CHUNKING_ALGORITHM, CHUNKING_VERSION) == ("paragraph_window", "v1")
    assert CHUNK_SIZE_UNIT == "unicode_code_points"
    assert (TARGET_CHUNK_SIZE, MAX_CHUNK_SIZE, OVERLAP_SIZE) == (800, 1000, 0)


def test_single_short_document_produces_one_chunk():
    value = document()
    chunks = chunk_knowledge_document(value)
    assert len(chunks) == 1
    assert chunks[0].content == value.content


def test_document_and_chunk_identity_are_distinct():
    value = document()
    chunk = chunk_knowledge_document(value)[0]
    assert chunk.document_id == value.document_id
    assert chunk.chunk_id != value.document_id


def test_chunk_id_is_stable_across_repeated_derivation():
    value = document("alpha\n\nbeta")
    assert chunk_knowledge_document(value) == chunk_knowledge_document(value)


def test_chunk_content_hash_is_sha256_of_canonical_utf8():
    chunk = chunk_knowledge_document(document("贵州茅台"))[0]
    assert chunk.content_hash == hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
    assert chunk.content_hash == chunk_content_hash(chunk.content)


def test_offsets_are_unicode_code_points_and_auditable():
    value = document("甲" * 810 + "\n\n" + "乙" * 20)
    for chunk in chunk_knowledge_document(value):
        assert value.content[chunk.start_offset:chunk.end_offset] == chunk.content


def test_paragraph_boundary_has_first_priority():
    value = document("甲" * 700 + "\n\n" + "乙" * 500)
    chunks = chunk_knowledge_document(value)
    assert chunks[0].content == "甲" * 700
    assert chunks[1].content == "乙" * 500


def test_line_boundary_is_second_priority():
    value = document("甲" * 700 + "\n" + "乙" * 500)
    chunks = chunk_knowledge_document(value)
    assert chunks[0].content == "甲" * 700
    assert chunks[1].content == "乙" * 500


def test_long_paragraph_uses_hard_maximum_split():
    chunks = chunk_knowledge_document(document("甲" * 2101))
    assert tuple(len(item.content) for item in chunks) == (1000, 1000, 101)


def test_chunking_never_emits_empty_or_whitespace_chunks():
    chunks = chunk_knowledge_document(document("甲" * 800 + "\n\n   \n\n" + "乙" * 20))
    assert chunks
    assert all(item.content and item.content.strip() for item in chunks)


def test_chunk_indices_are_contiguous_and_ordered():
    chunks = chunk_knowledge_document(document("x" * 2500))
    assert tuple(item.chunk_index for item in chunks) == tuple(range(len(chunks)))
    assert tuple(item.start_offset for item in chunks) == tuple(sorted(item.start_offset for item in chunks))


def test_each_chunk_respects_maximum_code_point_size():
    chunks = chunk_knowledge_document(document("甲" * 4000))
    assert max(len(item.content) for item in chunks) == MAX_CHUNK_SIZE


@pytest.mark.parametrize(
    "attribute",
    ["document_family_id", "document_version", "source", "title", "source_type",
     "entity_refs", "available_at", "published_at", "effective_from", "effective_to",
     "temporal_class", "provenance_source_identifier", "origin_reference"],
)
def test_chunk_preserves_parent_metadata(attribute):
    value = document()
    chunk = chunk_knowledge_document(value)[0]
    expected_name = "provenance" if attribute == "provenance_source_identifier" else attribute
    if attribute == "provenance_source_identifier":
        expected = value.provenance.source_identifier
    elif attribute == "origin_reference":
        expected = value.provenance.origin_reference
    else:
        expected = getattr(value, expected_name)
    assert getattr(chunk, attribute) == expected


def test_parent_document_change_changes_chunk_identity():
    first = chunk_knowledge_document(document("same", version=1))[0]
    second = chunk_knowledge_document(document("same", version=2))[0]
    assert first.content_hash == second.content_hash
    assert first.chunk_id != second.chunk_id


def test_corpus_chunk_order_is_independent_of_input_order():
    first = document("first", family="knowledge:test:b")
    second = document("second", family="knowledge:test:a")
    assert chunk_knowledge_corpus((first, second)) == chunk_knowledge_corpus((second, first))


def test_duplicate_documents_are_rejected():
    value = document()
    with pytest.raises(ValueError, match="duplicate"):
        chunk_knowledge_corpus((value, value))


def test_empty_corpus_has_deterministic_empty_chunk_manifest():
    corpus = manifest(())
    chunks = chunk_knowledge_corpus(())
    value = build_knowledge_chunk_manifest(chunks, corpus_manifest=corpus, generated_at=NOW)
    assert value.chunk_count == value.document_count == 0
    assert value.chunk_ids == value.document_ids == ()


def test_one_document_manifest_counts_are_consistent():
    value = document()
    chunks = chunk_knowledge_corpus((value,))
    result = build_knowledge_chunk_manifest(chunks, corpus_manifest=manifest((value,)), generated_at=NOW)
    assert result.chunk_count == len(chunks)
    assert result.document_count == 1


def test_manifest_is_independent_of_generated_at():
    value = document()
    chunks = chunk_knowledge_corpus((value,))
    corpus = manifest((value,))
    first = build_knowledge_chunk_manifest(chunks, corpus_manifest=corpus, generated_at=NOW)
    second = build_knowledge_chunk_manifest(
        chunks, corpus_manifest=corpus, generated_at=NOW + timedelta(hours=1),
    )
    assert first.chunk_manifest_version == second.chunk_manifest_version


def test_manifest_is_independent_of_document_insertion_order():
    first = document("first", family="knowledge:test:a")
    second = document("second", family="knowledge:test:b")
    left_chunks = chunk_knowledge_corpus((first, second))
    right_chunks = chunk_knowledge_corpus((second, first))
    left = build_knowledge_chunk_manifest(
        left_chunks, corpus_manifest=manifest((first, second)), generated_at=NOW,
    )
    right = build_knowledge_chunk_manifest(
        right_chunks, corpus_manifest=manifest((second, first)), generated_at=NOW,
    )
    assert left.chunk_manifest_version == right.chunk_manifest_version
    assert left.chunk_ids == right.chunk_ids


def test_corpus_change_changes_chunk_manifest_version():
    first = document("first", family="knowledge:test:a")
    second = document("second", family="knowledge:test:b")
    one = build_knowledge_chunk_manifest(
        chunk_knowledge_corpus((first,)), corpus_manifest=manifest((first,)), generated_at=NOW,
    )
    two = build_knowledge_chunk_manifest(
        chunk_knowledge_corpus((first, second)),
        corpus_manifest=manifest((first, second)), generated_at=NOW,
    )
    assert one.chunk_manifest_version != two.chunk_manifest_version
    assert one.corpus_version != two.corpus_version


def test_chunk_manifest_and_corpus_versions_are_distinct():
    value = document()
    corpus = manifest((value,))
    chunks = chunk_knowledge_corpus((value,))
    result = build_knowledge_chunk_manifest(chunks, corpus_manifest=corpus, generated_at=NOW)
    assert result.chunk_manifest_version != result.corpus_version


def test_manifest_rejects_document_set_mismatch():
    value = document()
    with pytest.raises(ValueError, match="do not match"):
        build_knowledge_chunk_manifest(
            (), corpus_manifest=manifest((value,)), generated_at=NOW,
        )


def test_manifest_rejects_naive_generated_time():
    value = document()
    chunks = chunk_knowledge_corpus((value,))
    with pytest.raises(ValueError, match="timezone-aware"):
        build_knowledge_chunk_manifest(
            chunks, corpus_manifest=manifest((value,)),
            generated_at=datetime(2026, 9, 7),
        )


def test_chunk_schema_rejects_identity_tampering():
    value = chunk_knowledge_document(document())[0]
    with pytest.raises(ValueError, match="identity mismatch"):
        replace(value, chunk_id="0" * 64)


def test_multiple_newline_gaps_are_only_omitted_as_whitespace():
    value = document("甲" * 790 + "\n\n \t\n\n" + "乙" * 40)
    chunks = chunk_knowledge_document(value)
    assert tuple(value.content[item.start_offset:item.end_offset] for item in chunks) == tuple(
        item.content for item in chunks
    )
    cursor = 0
    for chunk in chunks:
        assert not value.content[cursor:chunk.start_offset].strip()
        cursor = chunk.end_offset
    assert not value.content[cursor:].strip()


def test_chunk_count_and_boundaries_are_stable_for_unicode_content():
    value = document(("甲A😀" * 400) + "\n" + ("乙B" * 300))
    left = chunk_knowledge_document(value)
    right = chunk_knowledge_document(value)
    assert left == right
    assert tuple((item.start_offset, item.end_offset, item.content) for item in left) == tuple(
        (item.start_offset, item.end_offset, item.content) for item in right
    )


def test_chunk_identity_changes_with_algorithm_index_offsets_and_content():
    chunk = chunk_knowledge_document(document("stable chunk"))[0]
    base = {
        "document_id": chunk.document_id,
        "chunk_index": chunk.chunk_index,
        "start_offset": chunk.start_offset,
        "end_offset": chunk.end_offset,
        "content_hash": chunk.content_hash,
    }
    identities = {
        knowledge_chunk_id(**base),
        knowledge_chunk_id(**base, chunking_version="v2"),
        knowledge_chunk_id(**{**base, "chunk_index": 1}),
        knowledge_chunk_id(**{**base, "start_offset": 1}),
        knowledge_chunk_id(**{**base, "content_hash": "0" * 64}),
    }
    assert len(identities) == 5
