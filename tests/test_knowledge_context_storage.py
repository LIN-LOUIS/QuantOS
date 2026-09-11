"""Append-only knowledge context storage tests for Phase 4D."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantos.knowledge_context import assemble_knowledge_context
from quantos.knowledge_retrieval import build_knowledge_lexical_index, retrieve_knowledge
from quantos.schemas.knowledge import (
    KnowledgeDocumentInput,
    KnowledgeProvenance,
    KnowledgeQueryMode,
    KnowledgeSourceType,
    KnowledgeTemporalClass,
    canonicalize_knowledge_document,
)
from quantos.schemas.knowledge_context import KnowledgeContextPolicy
from quantos.schemas.knowledge_retrieval import KnowledgeRetrievalRequest
from quantos.storage.knowledge import KnowledgeRepository
from quantos.storage.knowledge_context import (
    KnowledgeContextCollisionError,
    KnowledgeContextCorruptionError,
    KnowledgeContextNotFoundError,
    KnowledgeContextRepository,
    KnowledgeContextStaleError,
    knowledge_context_from_record,
    knowledge_context_record,
)


SH = ZoneInfo("Asia/Shanghai")
HISTORICAL = datetime(2026, 9, 1, 15, tzinfo=SH)
GENERATED = datetime(2026, 9, 8, 12, tzinfo=SH)


def bundle(tmp_path: Path, *, generated_at=GENERATED):
    source = KnowledgeRepository(tmp_path / "documents")
    source.store(canonicalize_knowledge_document(KnowledgeDocumentInput(
        document_family_id="knowledge:a",
        document_version=1,
        source="official-source",
        source_type=KnowledgeSourceType.REFERENCE_DOCUMENT,
        title="Reference",
        content="alpha 贵州",
        temporal_class=KnowledgeTemporalClass.TIMELESS,
        published_at=HISTORICAL - timedelta(days=1),
        available_at=HISTORICAL,
        retrieved_at=HISTORICAL,
        effective_from=None,
        effective_to=None,
        entity_refs=("600519.SH",),
        provenance=KnowledgeProvenance(
            "artifact:knowledge:a", "fixture", "v1",
            "https://example.invalid/knowledge:a",
        ),
    )))
    index = build_knowledge_lexical_index(source, built_at=GENERATED)
    request = KnowledgeRetrievalRequest(
        query="alpha",
        mode=KnowledgeQueryMode.STRICT_LIVE,
        as_of_time=HISTORICAL,
    )
    result = retrieve_knowledge(source, index, request, generated_at=GENERATED)
    return assemble_knowledge_context(
        source, index, request, result,
        KnowledgeContextPolicy(
            max_items=10,
            max_context_chars=1000,
            allow_retrospective_research_context=False,
        ),
        generated_at=generated_at,
    )


def test_context_storage_round_trip(tmp_path):
    value = bundle(tmp_path)
    repository = KnowledgeContextRepository(tmp_path / "contexts")
    path = repository.store(value)
    assert repository.read(path) == value
    assert repository.load(value.context_id) == value
    assert path.name == f"context_id={value.context_id}.json"


def test_same_context_is_idempotent_and_does_not_rewrite(tmp_path):
    value = bundle(tmp_path)
    repository = KnowledgeContextRepository(tmp_path / "contexts")
    path = repository.store(value)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert repository.store(value) == path
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_generated_at_difference_is_observational_and_idempotent(tmp_path):
    first = bundle(tmp_path)
    second = replace(first, generated_at=GENERATED + timedelta(hours=1))
    assert first.context_id == second.context_id
    repository = KnowledgeContextRepository(tmp_path / "contexts")
    path = repository.store(first)
    assert repository.store(second) == path
    assert repository.read(path).generated_at == first.generated_at


def test_equivalent_timezone_representation_is_logically_idempotent(tmp_path):
    first = bundle(tmp_path)
    second = replace(
        first,
        as_of_time=first.as_of_time.astimezone(ZoneInfo("UTC")),
        generated_at=first.generated_at.astimezone(ZoneInfo("UTC")),
    )
    assert first.context_id == second.context_id
    repository = KnowledgeContextRepository(tmp_path / "contexts")
    path = repository.store(first)
    assert repository.store(second) == path


def test_same_identity_with_corrupt_payload_is_collision(tmp_path):
    value = bundle(tmp_path)
    repository = KnowledgeContextRepository(tmp_path / "contexts")
    path = repository.store(value)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["historical_items"][0]["content"] = "tampered"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(KnowledgeContextCollisionError):
        repository.store(value)


def test_missing_context_is_explicit_not_found(tmp_path):
    with pytest.raises(KnowledgeContextNotFoundError):
        KnowledgeContextRepository(tmp_path).load("0" * 64)


@pytest.mark.parametrize("mutation", [
    "invalid-json", "truncated", "unknown-schema", "unknown-field", "wrong-context-id",
    "wrong-chunk-id", "invalid-lane", "duplicate-item", "invalid-char-count",
    "invalid-rank", "invalid-flags", "invalid-timestamp",
    "nonboolean-flags", "invalid-count-type", "invalid-effective-interval",
])
def test_corrupt_context_artifact_fails_closed(tmp_path, mutation):
    value = bundle(tmp_path)
    repository = KnowledgeContextRepository(tmp_path / "contexts")
    path = repository.store(value)
    if mutation == "invalid-json":
        path.write_text("not-json", encoding="utf-8")
    elif mutation == "truncated":
        path.write_text("{", encoding="utf-8")
    else:
        record = json.loads(path.read_text(encoding="utf-8"))
        item = record["historical_items"][0]
        if mutation == "unknown-schema":
            record["schema_version"] = "future"
        elif mutation == "unknown-field":
            record["unexpected"] = True
        elif mutation == "wrong-context-id":
            record["context_id"] = "0" * 64
        elif mutation == "wrong-chunk-id":
            item["chunk_id"] = "0" * 64
        elif mutation == "invalid-lane":
            item["lane"] = "evidence"
        elif mutation == "duplicate-item":
            record["historical_items"].append(item.copy())
        elif mutation == "invalid-char-count":
            item["content_char_count"] += 1
        elif mutation == "invalid-rank":
            item["retrieval_rank"] = 2
        elif mutation == "invalid-flags":
            item["known_by_historical_as_of"] = False
            item["knowledge_after_event"] = True
        elif mutation == "nonboolean-flags":
            item["known_by_historical_as_of"] = 1
            item["knowledge_after_event"] = 0
        elif mutation == "invalid-count-type":
            record["selected_item_count"] = True
        elif mutation == "invalid-effective-interval":
            item["temporal_class"] = "time_bounded"
            item["effective_from"] = "2026-09-02T15:00:00+08:00"
            item["effective_to"] = "2026-09-01T15:00:00+08:00"
        else:
            item["available_at"] = "2026-09-01T15:00:00"
        path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(KnowledgeContextCorruptionError):
        repository.read(path)


def test_noncanonical_or_unknown_record_is_rejected():
    with pytest.raises(KnowledgeContextCorruptionError):
        knowledge_context_from_record({"schema_version": "future"})


def test_wrong_context_filename_is_corruption(tmp_path):
    value = bundle(tmp_path)
    repository = KnowledgeContextRepository(tmp_path / "contexts")
    path = repository.store(value)
    wrong = repository.root / f"context_id={'0' * 64}.json"
    wrong.write_bytes(path.read_bytes())
    with pytest.raises(KnowledgeContextCorruptionError):
        repository.read(wrong)


@pytest.mark.parametrize("expected", [
    {"expected_retrieval_request_id": "0" * 64},
    {"expected_lexical_index_version": "1" * 64},
    {"expected_corpus_version": "2" * 64},
])
def test_stale_context_references_fail_closed(tmp_path, expected):
    value = bundle(tmp_path)
    repository = KnowledgeContextRepository(tmp_path / "contexts")
    repository.store(value)
    with pytest.raises(KnowledgeContextStaleError):
        repository.load(value.context_id, **expected)


def test_read_and_load_never_rewrite_or_reassemble(tmp_path):
    value = bundle(tmp_path)
    repository = KnowledgeContextRepository(tmp_path / "contexts")
    path = repository.store(value)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    repository.read(path)
    repository.load(
        value.context_id,
        expected_retrieval_request_id=value.retrieval_request_id,
        expected_lexical_index_version=value.lexical_index_version,
        expected_corpus_version=value.corpus_version,
    )
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_context_record_round_trip_is_closed_and_deterministic(tmp_path):
    value = bundle(tmp_path)
    record = knowledge_context_record(value)
    assert knowledge_context_from_record(record) == value
    assert set(record) == {
        "schema_version", "context_id", "policy", "retrieval_request_id", "mode",
        "as_of_time", "corpus_cutoff", "corpus_version", "chunk_manifest_version",
        "lexical_index_version", "retrieved_hit_count", "selected_item_count",
        "selected_char_count", "historical_item_count", "retrospective_item_count",
        "omitted_hit_count", "selection_stop_reason", "historical_items",
        "retrospective_items", "generated_at",
    }


def test_concurrent_publication_has_one_complete_artifact(tmp_path):
    value = bundle(tmp_path)
    root = tmp_path / "contexts"
    variants = tuple(
        replace(value, generated_at=GENERATED + timedelta(seconds=index))
        for index in range(8)
    )

    def publish(item):
        return KnowledgeContextRepository(root).store(item)

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = tuple(pool.map(publish, variants))
    assert len(set(paths)) == 1
    assert len(tuple(root.glob("context_id=*.json"))) == 1
    assert not tuple(root.glob(".knowledge-context-*.tmp"))
    assert set(root.iterdir()) == {paths[0]}
    loaded = KnowledgeContextRepository(root).read(paths[0])
    assert loaded.context_id == value.context_id
