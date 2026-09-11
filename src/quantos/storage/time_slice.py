"""Append-only time-slice JSON/Markdown pairs; JSON plus refs is source of truth."""

import json
import os
from pathlib import Path
from decimal import Decimal
import tempfile
from uuid import uuid4

from .market import StorageError


class TimeSliceRepository:
    def __init__(self, settings):
        self.root = settings.data_root / "derived" / "time_slice_reports"

    def write(self, report, *, daily_report_loader=None):
        from quantos.reporting import canonical_json_bytes
        from quantos.time_slices import (
            load_daily_reference, render_time_slice, time_slice_from_record, time_slice_to_record,
        )
        daily_report_loader = daily_report_loader or load_daily_reference
        lock = None
        owns_lock = False
        temporary = None
        try:
            directory = self.root / f"target_trade_date={report.target_trade_date}"
            directory.mkdir(parents=True, exist_ok=True)
            stem = f"{report.report_id}-{uuid4().hex}"
            target, markdown = directory / f"{stem}.json", directory / f"{stem}.md"
            lock = directory / f".{stem}.lock"
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            owns_lock = True
            os.close(descriptor)
            if target.exists() or markdown.exists():
                raise FileExistsError()
            serialized = canonical_json_bytes(time_slice_to_record(report)) + b"\n"
            time_slice_from_record(json.loads(serialized, parse_float=Decimal))
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".time-slice-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, target)
            persisted = self.read(target)
            rendered = render_time_slice(persisted, daily_report_loader=daily_report_loader)
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".time-slice-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(rendered.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, markdown)
            return target, markdown
        except Exception:
            # A committed JSON is retained if Markdown fails; never claim pair success.
            raise StorageError("failed to persist complete time-slice report pair") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if owns_lock:
                lock.unlink(missing_ok=True)

    def read(self, path):
        from quantos.time_slices import time_slice_from_record

        try:
            return time_slice_from_record(json.loads(Path(path).read_bytes(), parse_float=Decimal))
        except Exception:
            raise StorageError("failed to read time-slice report") from None
