"""Append-only JSON persistence for safe, inspectable Ask traces."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile

from quantos.ask.contracts import AskTrace
from quantos.config import DEFAULT_SETTINGS, Settings
from quantos.serialization import canonical_json_bytes


class AskTraceStorageError(ValueError):
    """Safe storage error that never includes a trace payload."""


_SECRET = re.compile(
    r"(?i)(authorization\s*[:=]|password\s*[:=]|api[_-]?key\s*[:=]|"
    r"token\s*[:=]|bearer\s+[A-Za-z0-9._-]+|\b(?:sk|tvly)-[A-Za-z0-9_-]{8,})"
)
_TRACE_ID = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{64})$")
_REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")


class AskTraceRepository:
    def __init__(self, root: Path | None = None, *, settings: Settings = DEFAULT_SETTINGS) -> None:
        self.root = Path(root) if root is not None else settings.data_root / "derived" / "ask_traces"

    def save(self, trace: AskTrace) -> Path:
        if not isinstance(trace, AskTrace):
            raise TypeError("AskTrace required")
        return self.save_record(trace.to_dict())

    def save_record(self, record: dict[str, object]) -> Path:
        try:
            serialized = canonical_json_bytes(record)
            if _SECRET.search(serialized.decode("utf-8")):
                raise AskTraceStorageError("unsafe trace payload rejected")
            trace = AskTrace.from_dict(json.loads(serialized))
            if not _TRACE_ID.fullmatch(trace.trace_id) or not _REQUEST_ID.fullmatch(
                trace.request_id
            ):
                raise AskTraceStorageError("invalid Ask trace identity")
            directory = self.root / f"created_date={trace.created_at.date().isoformat()}"
            target = directory / f"trace_id={trace.trace_id}.json"
            directory.mkdir(parents=True, exist_ok=True)
            if target.exists():
                existing = self.read(target)
                if existing != trace:
                    raise AskTraceStorageError("trace identity collision")
                return target
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=directory, prefix=".ask-trace-", suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(serialized + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    existing = self.read(target)
                    if existing != trace:
                        raise AskTraceStorageError("trace identity collision")
                return target
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        except AskTraceStorageError:
            raise
        except Exception:
            raise AskTraceStorageError("failed to persist Ask trace") from None

    def read(self, path: Path) -> AskTrace:
        try:
            record = json.loads(Path(path).read_text(encoding="utf-8"))
            if _SECRET.search(json.dumps(record, ensure_ascii=False)):
                raise ValueError("unsafe trace")
            return AskTrace.from_dict(record)
        except Exception:
            raise AskTraceStorageError("failed to read Ask trace") from None

    def load(self, trace_id: str) -> AskTrace:
        if not isinstance(trace_id, str) or not _TRACE_ID.fullmatch(trace_id):
            raise AskTraceStorageError("invalid Ask trace id")
        matches = tuple(self.root.glob(f"created_date=*/trace_id={trace_id}.json"))
        if len(matches) != 1:
            raise AskTraceStorageError("Ask trace was not found")
        return self.read(matches[0])

    def find_by_request_id(self, request_id: str) -> tuple[AskTrace, ...]:
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise AskTraceStorageError("invalid Ask request id")
        values = tuple(
            trace for trace in self._all() if trace.request_id == request_id
        )
        return tuple(sorted(values, key=lambda item: (item.created_at, item.trace_id)))

    def recent(self, *, limit: int = 20) -> tuple[AskTrace, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("trace limit must be between 1 and 100")
        values = sorted(self._all(), key=lambda item: (item.created_at, item.trace_id), reverse=True)
        return tuple(values[:limit])

    def _all(self) -> tuple[AskTrace, ...]:
        if not self.root.exists():
            return ()
        return tuple(self.read(path) for path in sorted(self.root.glob("created_date=*/*.json")))
