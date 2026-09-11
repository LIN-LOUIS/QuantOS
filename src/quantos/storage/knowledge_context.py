"""Append-only persistence for deterministic knowledge context bundles."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping

from quantos.config import DEFAULT_SETTINGS, Settings
from quantos.serialization import canonical_json_bytes
from quantos.schemas._validation import require_aware
from quantos.schemas.knowledge import KnowledgeQueryMode, KnowledgeSourceType, KnowledgeTemporalClass
from quantos.schemas.knowledge_context import (
    KNOWLEDGE_CONTEXT_BUNDLE_SCHEMA_VERSION,
    KNOWLEDGE_CONTEXT_ITEM_SCHEMA_VERSION,
    KNOWLEDGE_CONTEXT_POLICY_SCHEMA_VERSION,
    KnowledgeContextBundle,
    KnowledgeContextItem,
    KnowledgeContextLane,
    KnowledgeContextPolicy,
    KnowledgeContextStopReason,
    validate_knowledge_context_bundle,
)
from .market import StorageError


_BUNDLE_FIELDS = {
    "schema_version", "context_id", "policy", "retrieval_request_id", "mode",
    "as_of_time", "corpus_cutoff", "corpus_version", "chunk_manifest_version",
    "lexical_index_version", "retrieved_hit_count", "selected_item_count",
    "selected_char_count", "historical_item_count", "retrospective_item_count",
    "omitted_hit_count", "selection_stop_reason", "historical_items",
    "retrospective_items", "generated_at",
}
_POLICY_FIELDS = {
    "schema_version", "selection_algorithm", "selection_version", "max_items",
    "max_context_chars", "allow_retrospective_research_context",
}
_ITEM_FIELDS = {
    "schema_version", "lane", "chunk_id", "document_id", "document_family_id",
    "document_version", "retrieval_rank", "retrieval_score", "content",
    "content_hash", "content_char_count", "source", "source_type", "title",
    "published_at", "available_at", "effective_from", "effective_to",
    "temporal_class", "entity_refs", "provenance_source_identifier",
    "origin_reference", "known_by_historical_as_of", "knowledge_after_event",
}


class KnowledgeContextStorageError(StorageError):
    pass


class KnowledgeContextCorruptionError(KnowledgeContextStorageError):
    pass


class KnowledgeContextCollisionError(KnowledgeContextStorageError):
    pass


class KnowledgeContextStaleError(KnowledgeContextStorageError):
    pass


class KnowledgeContextNotFoundError(KnowledgeContextStorageError):
    pass


def knowledge_context_record(bundle: KnowledgeContextBundle) -> dict[str, object]:
    if not isinstance(bundle, KnowledgeContextBundle):
        raise TypeError("KnowledgeContextBundle required")
    validate_knowledge_context_bundle(bundle)
    return {
        "schema_version": bundle.schema_version,
        "context_id": bundle.context_id,
        "policy": _policy_record(bundle.policy),
        "retrieval_request_id": bundle.retrieval_request_id,
        "mode": bundle.mode.value,
        "as_of_time": _iso(bundle.as_of_time),
        "corpus_cutoff": _iso(bundle.corpus_cutoff),
        "corpus_version": bundle.corpus_version,
        "chunk_manifest_version": bundle.chunk_manifest_version,
        "lexical_index_version": bundle.lexical_index_version,
        "retrieved_hit_count": bundle.retrieved_hit_count,
        "selected_item_count": bundle.selected_item_count,
        "selected_char_count": bundle.selected_char_count,
        "historical_item_count": bundle.historical_item_count,
        "retrospective_item_count": bundle.retrospective_item_count,
        "omitted_hit_count": bundle.omitted_hit_count,
        "selection_stop_reason": bundle.selection_stop_reason.value,
        "historical_items": [_item_record(item) for item in bundle.historical_items],
        "retrospective_items": [_item_record(item) for item in bundle.retrospective_items],
        "generated_at": _iso(bundle.generated_at),
    }


def knowledge_context_from_record(record: Mapping[str, object]) -> KnowledgeContextBundle:
    try:
        if not isinstance(record, Mapping) or set(record) != _BUNDLE_FIELDS:
            raise ValueError("invalid knowledge context fields")
        if record["schema_version"] != KNOWLEDGE_CONTEXT_BUNDLE_SCHEMA_VERSION:
            raise ValueError("unsupported knowledge context schema")
        bundle = KnowledgeContextBundle(
            schema_version=record["schema_version"],
            context_id=record["context_id"],
            policy=_policy_from_record(record["policy"]),
            retrieval_request_id=record["retrieval_request_id"],
            mode=KnowledgeQueryMode(record["mode"]),
            as_of_time=_datetime(record["as_of_time"]),
            corpus_cutoff=_optional_datetime(record["corpus_cutoff"]),
            corpus_version=record["corpus_version"],
            chunk_manifest_version=record["chunk_manifest_version"],
            lexical_index_version=record["lexical_index_version"],
            retrieved_hit_count=record["retrieved_hit_count"],
            selected_item_count=record["selected_item_count"],
            selected_char_count=record["selected_char_count"],
            historical_item_count=record["historical_item_count"],
            retrospective_item_count=record["retrospective_item_count"],
            omitted_hit_count=record["omitted_hit_count"],
            selection_stop_reason=KnowledgeContextStopReason(record["selection_stop_reason"]),
            historical_items=_items(record["historical_items"]),
            retrospective_items=_items(record["retrospective_items"]),
            generated_at=_datetime(record["generated_at"]),
        )
        if canonical_json_bytes(knowledge_context_record(bundle)) != canonical_json_bytes(record):
            raise ValueError("noncanonical knowledge context record")
        return bundle
    except (TypeError, ValueError, KeyError):
        raise KnowledgeContextCorruptionError("knowledge context artifact is corrupt") from None


class KnowledgeContextRepository:
    def __init__(self, root: Path | None = None, *, settings: Settings = DEFAULT_SETTINGS):
        self.root = (
            Path(root) if root is not None
            else settings.data_root / "knowledge" / "contexts"
        )

    def store(self, bundle: KnowledgeContextBundle) -> Path:
        if not isinstance(bundle, KnowledgeContextBundle):
            raise TypeError("KnowledgeContextBundle required")
        record = knowledge_context_record(bundle)
        knowledge_context_from_record(record)
        target = self._path_for(bundle.context_id)
        temporary = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with _exclusive_lock(self.root):
                if target.exists():
                    return self._verify_existing(target, bundle)
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=self.root, prefix=".knowledge-context-", suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(canonical_json_bytes(record) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    return self._verify_existing(target, bundle)
                return target
        except KnowledgeContextStorageError:
            raise
        except Exception:
            raise KnowledgeContextStorageError("failed to persist knowledge context") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read(self, path: Path) -> KnowledgeContextBundle:
        if not Path(path).is_file():
            raise KnowledgeContextNotFoundError("knowledge context was not found")
        try:
            bundle = knowledge_context_from_record(json.loads(
                Path(path).read_text(encoding="utf-8"),
            ))
            if Path(path).resolve() != self._path_for(bundle.context_id).resolve():
                raise ValueError("knowledge context path does not match identity")
            return bundle
        except KnowledgeContextCorruptionError:
            raise
        except Exception:
            raise KnowledgeContextCorruptionError("knowledge context artifact is corrupt") from None

    def load(
        self, context_id: str, *, expected_retrieval_request_id: str | None = None,
        expected_lexical_index_version: str | None = None,
        expected_corpus_version: str | None = None,
    ) -> KnowledgeContextBundle:
        bundle = self.read(self._path_for(context_id))
        if (expected_retrieval_request_id is not None
                and bundle.retrieval_request_id != expected_retrieval_request_id):
            raise KnowledgeContextStaleError("knowledge context retrieval reference is stale")
        if (expected_lexical_index_version is not None
                and bundle.lexical_index_version != expected_lexical_index_version):
            raise KnowledgeContextStaleError("knowledge context index reference is stale")
        if (expected_corpus_version is not None
                and bundle.corpus_version != expected_corpus_version):
            raise KnowledgeContextStaleError("knowledge context corpus reference is stale")
        return bundle

    def _path_for(self, context_id: str) -> Path:
        if type(context_id) is not str or len(context_id) != 64 or any(
            character not in "0123456789abcdef" for character in context_id
        ):
            raise KnowledgeContextCorruptionError("invalid knowledge context ID")
        return self.root / f"context_id={context_id}.json"

    def _verify_existing(self, target: Path, bundle: KnowledgeContextBundle) -> Path:
        try:
            existing = self.read(target)
        except KnowledgeContextCorruptionError:
            raise KnowledgeContextCollisionError("knowledge context collision or corruption") from None
        existing_record = knowledge_context_record(existing)
        requested_record = knowledge_context_record(bundle)
        existing_record.pop("generated_at")
        requested_record.pop("generated_at")
        if existing_record != requested_record:
            raise KnowledgeContextCollisionError("knowledge context identity collision")
        return target


def _policy_record(policy: KnowledgeContextPolicy) -> dict[str, object]:
    return {
        "schema_version": policy.schema_version,
        "selection_algorithm": policy.selection_algorithm,
        "selection_version": policy.selection_version,
        "max_items": policy.max_items,
        "max_context_chars": policy.max_context_chars,
        "allow_retrospective_research_context": (
            policy.allow_retrospective_research_context
        ),
    }


def _policy_from_record(record) -> KnowledgeContextPolicy:
    if not isinstance(record, Mapping) or set(record) != _POLICY_FIELDS:
        raise ValueError("invalid knowledge context policy")
    if record["schema_version"] != KNOWLEDGE_CONTEXT_POLICY_SCHEMA_VERSION:
        raise ValueError("unsupported knowledge context policy schema")
    return KnowledgeContextPolicy(
        schema_version=record["schema_version"],
        selection_algorithm=record["selection_algorithm"],
        selection_version=record["selection_version"],
        max_items=record["max_items"],
        max_context_chars=record["max_context_chars"],
        allow_retrospective_research_context=record[
            "allow_retrospective_research_context"
        ],
    )


def _item_record(item: KnowledgeContextItem) -> dict[str, object]:
    return {
        "schema_version": item.schema_version,
        "lane": item.lane.value,
        "chunk_id": item.chunk_id,
        "document_id": item.document_id,
        "document_family_id": item.document_family_id,
        "document_version": item.document_version,
        "retrieval_rank": item.retrieval_rank,
        "retrieval_score": item.retrieval_score,
        "content": item.content,
        "content_hash": item.content_hash,
        "content_char_count": item.content_char_count,
        "source": item.source,
        "source_type": item.source_type.value,
        "title": item.title,
        "published_at": _iso(item.published_at),
        "available_at": _iso(item.available_at),
        "effective_from": _iso(item.effective_from),
        "effective_to": _iso(item.effective_to),
        "temporal_class": item.temporal_class.value,
        "entity_refs": list(item.entity_refs),
        "provenance_source_identifier": item.provenance_source_identifier,
        "origin_reference": item.origin_reference,
        "known_by_historical_as_of": item.known_by_historical_as_of,
        "knowledge_after_event": item.knowledge_after_event,
    }


def _items(record) -> tuple[KnowledgeContextItem, ...]:
    if type(record) is not list:
        raise ValueError("knowledge context items must be a list")
    return tuple(_item_from_record(item) for item in record)


def _item_from_record(record) -> KnowledgeContextItem:
    if not isinstance(record, Mapping) or set(record) != _ITEM_FIELDS:
        raise ValueError("invalid knowledge context item")
    if record["schema_version"] != KNOWLEDGE_CONTEXT_ITEM_SCHEMA_VERSION:
        raise ValueError("unsupported knowledge context item schema")
    if type(record["entity_refs"]) is not list:
        raise ValueError("knowledge context entity refs must be a list")
    return KnowledgeContextItem(
        schema_version=record["schema_version"],
        lane=KnowledgeContextLane(record["lane"]),
        chunk_id=record["chunk_id"],
        document_id=record["document_id"],
        document_family_id=record["document_family_id"],
        document_version=record["document_version"],
        retrieval_rank=record["retrieval_rank"],
        retrieval_score=record["retrieval_score"],
        content=record["content"],
        content_hash=record["content_hash"],
        content_char_count=record["content_char_count"],
        source=record["source"],
        source_type=KnowledgeSourceType(record["source_type"]),
        title=record["title"],
        published_at=_optional_datetime(record["published_at"]),
        available_at=_datetime(record["available_at"]),
        effective_from=_optional_datetime(record["effective_from"]),
        effective_to=_optional_datetime(record["effective_to"]),
        temporal_class=KnowledgeTemporalClass(record["temporal_class"]),
        entity_refs=tuple(record["entity_refs"]),
        provenance_source_identifier=record["provenance_source_identifier"],
        origin_reference=record["origin_reference"],
        known_by_historical_as_of=record["known_by_historical_as_of"],
        knowledge_after_event=record["knowledge_after_event"],
    )


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _datetime(value) -> datetime:
    if type(value) is not str:
        raise ValueError("knowledge context timestamp must be text")
    result = datetime.fromisoformat(value)
    require_aware(result, "knowledge context timestamp")
    return result


def _optional_datetime(value) -> datetime | None:
    return _datetime(value) if value is not None else None


@contextmanager
def _exclusive_lock(root: Path):
    descriptor = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
