"""Append-only persistence for validated evidence synthesis records."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime
import tempfile
from uuid import uuid4

from quantos.config import Settings
from quantos.schemas import EvidenceSynthesis, KnowledgeBackgroundStatement, PossibleExplanation
from quantos.serialization import canonical_json_bytes
from quantos.synthesis import synthesis_record

from .market import StorageError


_OBSERVATIONAL_OUTPUT_FIELDS = {
    "generated_at", "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_tokens", "total_tokens", "duration_ms",
}


class SynthesisRepository:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_directories()

    def write(
        self, output: EvidenceSynthesis, *, cache_key: dict[str, str] | None = None,
    ) -> Path:
        day = output.generated_at.date().isoformat()
        target_dir = self.settings.synthesis_dir / f"generated_date={day}"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{output.input_bundle_id}-{uuid4().hex}.json"
        record = dict(synthesis_record(output))
        if cache_key is not None:
            record["cache_key"] = dict(cache_key)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=target_dir,
                prefix=".synthesis-", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(
                    json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2)
                    + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
            if cache_key is None:
                os.link(temporary, target)
                return target
            with _cache_key_lock(self.settings.synthesis_dir, cache_key):
                existing = _resolve_valid_cache_key(
                    self.settings.synthesis_dir, cache_key,
                )
                if existing is not None:
                    existing_path, existing_output = existing
                    if _logical_cache_payload(existing_output) != (
                        _logical_cache_payload(output)
                    ):
                        raise StorageError("synthesis cache logical-key collision")
                    return existing_path
                os.link(temporary, target)
                return target
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def find_valid(self, cache_key: dict[str, str]) -> EvidenceSynthesis | None:
        """Return the unique exact PASS payload; ambiguity fails closed."""

        resolved = _resolve_valid_cache_key(self.settings.synthesis_dir, cache_key)
        return resolved[1] if resolved is not None else None


def _resolve_valid_cache_key(
    root: Path, cache_key: dict[str, str],
) -> tuple[Path, EvidenceSynthesis] | None:
    matches: list[tuple[Path, EvidenceSynthesis]] = []
    for path in sorted(root.rglob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("validation_result") != "PASS":
                continue
            if record.get("cache_key") != cache_key:
                continue
            output = _output_from_record(record["structured_output"])
            if not _output_matches_cache_key(output, cache_key):
                continue
            matches.append((path, output))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    if not matches:
        return None
    payloads = {_logical_cache_payload(output) for _, output in matches}
    if len(payloads) != 1:
        raise StorageError("synthesis cache logical-key collision")
    return matches[0]


def _logical_cache_payload(output: EvidenceSynthesis) -> bytes:
    payload = dict(synthesis_record(output)["structured_output"])
    for field in _OBSERVATIONAL_OUTPUT_FIELDS:
        payload.pop(field)
    return canonical_json_bytes(payload)


@contextmanager
def _cache_key_lock(root: Path, cache_key: dict[str, str]):
    lock_identity = hashlib.sha256(canonical_json_bytes(cache_key)).hexdigest()
    lock_directory = root / ".locks"
    lock_directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        lock_directory / f"{lock_identity}.lock",
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _output_from_record(value: dict[str, object]) -> EvidenceSynthesis:
    explanations = tuple(
        PossibleExplanation(
            statement=str(item["statement"]),
            supporting_evidence_ids=tuple(item["supporting_evidence_ids"]),
            limitations=tuple(item["limitations"]),
            causal_status=str(item["causal_status"]),
        )
        for item in value["possible_explanations"]
    )
    knowledge_background = _knowledge_statements(value.get("knowledge_background", []))
    retrospective_knowledge = _knowledge_statements(
        value.get("retrospective_knowledge_context", [])
    )
    return EvidenceSynthesis(
        symbol=str(value["symbol"]), mode=str(value["mode"]),
        evidence_summary=str(value["evidence_summary"]),
        possible_explanations=explanations,
        contradicting_signals=tuple(value["contradicting_signals"]),
        post_event_notes=tuple(value["post_event_notes"]),
        insufficient_evidence=bool(value["insufficient_evidence"]),
        limitations=tuple(value["limitations"]),
        used_evidence_ids=tuple(value["used_evidence_ids"]),
        prompt_version=str(value["prompt_version"]),
        schema_version=str(value["schema_version"]),
        model_provider=str(value["model_provider"]),
        model_name=str(value["model_name"]),
        generated_at=datetime.fromisoformat(str(value["generated_at"])),
        input_bundle_id=str(value["input_bundle_id"]),
        input_tokens=_optional_int(value.get("input_tokens")),
        output_tokens=_optional_int(value.get("output_tokens")),
        total_tokens=_optional_int(value.get("total_tokens")),
        cached_input_tokens=_optional_int(value.get("cached_input_tokens")),
        reasoning_tokens=_optional_int(value.get("reasoning_tokens")),
        duration_ms=_optional_int(value.get("duration_ms")),
        knowledge_background=knowledge_background,
        retrospective_knowledge_context=retrospective_knowledge,
        supplied_context_id=(
            str(value["supplied_context_id"])
            if value.get("supplied_context_id") is not None else None
        ),
        supplied_knowledge_refs=tuple(value.get("supplied_knowledge_refs", [])),
    )


def _knowledge_statements(value: object) -> tuple[KnowledgeBackgroundStatement, ...]:
    if not isinstance(value, list):
        raise ValueError("knowledge statements must be a list")
    return tuple(
        KnowledgeBackgroundStatement(
            statement=str(item["statement"]),
            knowledge_refs=tuple(item["knowledge_refs"]),
        )
        for item in value
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("token and duration metadata must be non-negative integers")
    return value


def _output_matches_cache_key(
    output: EvidenceSynthesis, cache_key: dict[str, str],
) -> bool:
    expected_context_id = cache_key.get("knowledge_context_id")
    expected_references = (
        tuple(filter(None, cache_key.get("supplied_knowledge_refs", "").split(",")))
        if expected_context_id is not None else ()
    )
    return (
        output.input_bundle_id == cache_key.get("input_bundle_id")
        and output.prompt_version == cache_key.get("prompt_version")
        and output.schema_version == cache_key.get("schema_version")
        and output.model_provider == cache_key.get("model_provider")
        and output.model_name == cache_key.get("model_name")
        and output.supplied_context_id == expected_context_id
        and output.supplied_knowledge_refs == expected_references
    )
