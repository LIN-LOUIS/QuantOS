"""Immutable lexical-index build and persistence tests for Phase 4C."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import quantos.schemas.knowledge_retrieval as retrieval_schema
from quantos.knowledge_retrieval import build_knowledge_lexical_index
from quantos.schemas.knowledge import (
    KnowledgeDocumentInput,
    KnowledgeProvenance,
    KnowledgeSourceType,
    KnowledgeTemporalClass,
    canonicalize_knowledge_document,
)
from quantos.schemas.knowledge_retrieval import knowledge_lexical_index_version
from quantos.storage.knowledge import KnowledgeRepository
from quantos.storage.knowledge_index import (
    KnowledgeIndexCollisionError,
    KnowledgeIndexCorruptionError,
    KnowledgeIndexNotFoundError,
    KnowledgeIndexStaleError,
    KnowledgeLexicalIndexRepository,
    knowledge_lexical_index_from_record,
    knowledge_lexical_index_record,
)


SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 8, 12, tzinfo=SH)


def document(family="knowledge:a", content="alpha beta"):
    return canonicalize_knowledge_document(KnowledgeDocumentInput(
        document_family_id=family,
        document_version=1,
        source="official-source",
        source_type=KnowledgeSourceType.REFERENCE_DOCUMENT,
        title="Reference",
        content=content,
        temporal_class=KnowledgeTemporalClass.TIMELESS,
        published_at=NOW - timedelta(days=2),
        available_at=NOW - timedelta(days=1),
        retrieved_at=NOW - timedelta(days=1),
        effective_from=None,
        effective_to=None,
        entity_refs=(),
        provenance=KnowledgeProvenance(
            f"artifact:{family}", "fixture", "v1", f"https://example.invalid/{family}",
        ),
    ))


def build(tmp_path: Path, documents=(None,), *, built_at=NOW):
    docs = (document(),) if documents == (None,) else tuple(documents)
    source = KnowledgeRepository(tmp_path / "documents")
    for item in docs:
        source.store(item)
    return source, build_knowledge_lexical_index(source, built_at=built_at)


def test_index_build_is_stable_across_insertion_order_and_build_time(tmp_path):
    documents = (document("knowledge:b", "beta"), document("knowledge:a", "alpha"))
    left_source, left = build(tmp_path / "left", documents, built_at=NOW)
    _, right = build(tmp_path / "right", tuple(reversed(documents)), built_at=NOW + timedelta(hours=1))
    assert left.lexical_index_version == right.lexical_index_version
    assert left.corpus_version == right.corpus_version
    assert tuple(entry.chunk.chunk_id for entry in left.entries) == tuple(
        entry.chunk.chunk_id for entry in right.entries
    )
    assert left.built_at != right.built_at
    assert left_source.build_manifest(generated_at=NOW).document_count == 2


def test_index_version_changes_with_corpus_content(tmp_path):
    _, first = build(tmp_path / "first", (document(content="alpha"),))
    _, second = build(tmp_path / "second", (document(content="alpha changed"),))
    assert first.lexical_index_version != second.lexical_index_version
    assert first.corpus_version != second.corpus_version


def test_index_version_covers_tokenizer_retrieval_and_schema_versions(tmp_path, monkeypatch):
    _, index = build(tmp_path)
    values = {
        knowledge_lexical_index_version(
            corpus_version=index.corpus_version,
            chunk_manifest_version=index.chunk_manifest_version,
            entries=index.entries,
        ),
        knowledge_lexical_index_version(
            corpus_version=index.corpus_version,
            chunk_manifest_version=index.chunk_manifest_version,
            entries=index.entries,
            tokenizer_version="v2",
        ),
        knowledge_lexical_index_version(
            corpus_version=index.corpus_version,
            chunk_manifest_version=index.chunk_manifest_version,
            entries=index.entries,
            retrieval_version="v2",
        ),
        knowledge_lexical_index_version(
            corpus_version=index.corpus_version,
            chunk_manifest_version=index.chunk_manifest_version,
            entries=index.entries,
            retrieval_algorithm="BM25-variant",
        ),
    }
    monkeypatch.setattr(
        retrieval_schema, "KNOWLEDGE_LEXICAL_INDEX_SCHEMA_VERSION",
        "quantos-knowledge-lexical-index-v2",
    )
    values.add(knowledge_lexical_index_version(
        corpus_version=index.corpus_version,
        chunk_manifest_version=index.chunk_manifest_version,
        entries=index.entries,
    ))
    assert len(values) == 5


def test_index_round_trip_preserves_canonical_entry_order(tmp_path):
    _, index = build(tmp_path, (
        document("knowledge:b", "beta"), document("knowledge:a", "alpha"),
    ))
    repository = KnowledgeLexicalIndexRepository(tmp_path / "indexes")
    path = repository.store(index)
    loaded = repository.read(path)
    assert loaded == index
    assert tuple(entry.chunk.document_family_id for entry in loaded.entries) == (
        "knowledge:a", "knowledge:b",
    )


def test_same_index_store_is_idempotent(tmp_path):
    _, index = build(tmp_path)
    repository = KnowledgeLexicalIndexRepository(tmp_path / "indexes")
    first = repository.store(index)
    before = first.read_bytes()
    second = repository.store(index)
    assert second == first and first.read_bytes() == before


def test_built_at_difference_is_observational_not_collision(tmp_path):
    _, index = build(tmp_path)
    repository = KnowledgeLexicalIndexRepository(tmp_path / "indexes")
    path = repository.store(index)
    rebuilt = replace(index, built_at=NOW + timedelta(hours=1))
    assert rebuilt.lexical_index_version == index.lexical_index_version
    assert repository.store(rebuilt) == path
    assert repository.read(path).built_at == index.built_at


def test_same_identity_with_corrupt_or_different_payload_is_collision(tmp_path):
    _, index = build(tmp_path)
    repository = KnowledgeLexicalIndexRepository(tmp_path / "indexes")
    path = repository.store(index)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["entries"][0]["tokens"].append("different")
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(KnowledgeIndexCollisionError):
        repository.store(index)


def test_missing_index_is_explicit_not_found(tmp_path):
    repository = KnowledgeLexicalIndexRepository(tmp_path)
    with pytest.raises(KnowledgeIndexNotFoundError):
        repository.load("0" * 64)


@pytest.mark.parametrize("mutation", [
    "invalid-json", "truncated", "unknown-schema", "unknown-field", "invalid-nested",
])
def test_corrupt_index_fails_closed(tmp_path, mutation):
    _, index = build(tmp_path)
    repository = KnowledgeLexicalIndexRepository(tmp_path / "indexes")
    path = repository.store(index)
    if mutation == "invalid-json":
        path.write_text("not-json", encoding="utf-8")
    elif mutation == "truncated":
        path.write_text("{", encoding="utf-8")
    else:
        record = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "unknown-schema":
            record["schema_version"] = "future"
        elif mutation == "unknown-field":
            record["unexpected"] = True
        else:
            record["entries"][0]["chunk"]["entity_refs"] = "not-a-list"
        path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(KnowledgeIndexCorruptionError):
        repository.read(path)


def test_noncanonical_record_is_rejected():
    with pytest.raises(KnowledgeIndexCorruptionError):
        knowledge_lexical_index_from_record({"schema_version": "future"})


def test_wrong_index_filename_is_corruption(tmp_path):
    _, index = build(tmp_path)
    repository = KnowledgeLexicalIndexRepository(tmp_path / "indexes")
    path = repository.store(index)
    wrong = repository.root / f"lexical_index_version={'0' * 64}.json"
    wrong.write_bytes(path.read_bytes())
    with pytest.raises(KnowledgeIndexCorruptionError):
        repository.read(wrong)


def test_stale_corpus_and_chunk_manifest_fail_closed(tmp_path):
    _, index = build(tmp_path)
    repository = KnowledgeLexicalIndexRepository(tmp_path / "indexes")
    repository.store(index)
    with pytest.raises(KnowledgeIndexStaleError):
        repository.load(index.lexical_index_version, expected_corpus_version="0" * 64)
    with pytest.raises(KnowledgeIndexStaleError):
        repository.load(index.lexical_index_version, expected_chunk_manifest_version="1" * 64)


def test_explicit_rebuild_creates_new_version_without_rewriting_old(tmp_path):
    source, first = build(tmp_path)
    indexes = KnowledgeLexicalIndexRepository(tmp_path / "indexes")
    first_path = indexes.store(first)
    before = first_path.read_bytes()
    source.store(document("knowledge:b", "new corpus content"))
    second = build_knowledge_lexical_index(source, built_at=NOW + timedelta(hours=1))
    assert second.lexical_index_version != first.lexical_index_version
    with pytest.raises(KnowledgeIndexStaleError):
        indexes.load(first.lexical_index_version, expected_corpus_version=second.corpus_version)
    assert first_path.read_bytes() == before
    assert indexes.store(second) != first_path


def test_index_build_rejects_naive_built_at(tmp_path):
    source = KnowledgeRepository(tmp_path / "documents")
    source.store(document())
    with pytest.raises(ValueError, match="timezone-aware"):
        build_knowledge_lexical_index(source, built_at=datetime(2026, 9, 8, 12))


def test_concurrent_logically_identical_publication_has_one_target(tmp_path):
    _, index = build(tmp_path)
    root = tmp_path / "indexes"
    variants = tuple(replace(index, built_at=NOW + timedelta(seconds=value)) for value in range(8))

    def publish(value):
        return KnowledgeLexicalIndexRepository(root).store(value)

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = tuple(pool.map(publish, variants))
    assert len(set(paths)) == 1
    assert len(tuple(root.glob("lexical_index_version=*.json"))) == 1
    loaded = KnowledgeLexicalIndexRepository(root).read(paths[0])
    assert knowledge_lexical_index_record(loaded)["lexical_index_version"] == index.lexical_index_version
