from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from quantos.storage.market import StorageError
from quantos.storage.time_slice import TimeSliceRepository
from quantos.time_slices import render_time_slice
from tests.test_time_slices import product


def test_append_only_pair_identity_and_json_source(tmp_path):
    report, _, _, _, settings = product(tmp_path)
    repository = TimeSliceRepository(settings)
    first = repository.write(report)
    second = repository.write(report)
    assert first[0] != second[0]
    assert first[0].stem == first[1].stem
    assert first[0].read_bytes() == second[0].read_bytes()
    assert first[1].read_text() == render_time_slice(repository.read(first[0]))


def test_collision_does_not_overwrite(tmp_path, monkeypatch):
    report, _, _, _, settings = product(tmp_path)
    monkeypatch.setattr("quantos.storage.time_slice.uuid4", lambda: SimpleNamespace(hex="fixed"))
    repository = TimeSliceRepository(settings)
    paths = repository.write(report)
    before = paths[0].read_bytes()
    with pytest.raises(StorageError):
        repository.write(report)
    assert paths[0].read_bytes() == before


def test_json_atomic_rename_failure_leaves_no_partial(tmp_path, monkeypatch):
    report, _, _, _, settings = product(tmp_path)
    repository = TimeSliceRepository(settings)
    def fail(*args):
        raise OSError("sensitive exception text")
    monkeypatch.setattr("quantos.storage.time_slice.os.rename", fail)
    with pytest.raises(StorageError) as error:
        repository.write(report)
    assert "sensitive" not in str(error.value)
    assert not list(repository.root.rglob("*.json"))
    assert not list(repository.root.rglob(".time-slice-*"))


def test_markdown_failure_retains_json_and_explicit_error(tmp_path, monkeypatch):
    report, _, _, _, settings = product(tmp_path)
    repository = TimeSliceRepository(settings)
    def fail(*args, **kw):
        raise ValueError("Authorization raw response")
    monkeypatch.setattr("quantos.time_slices.render_time_slice", fail)
    with pytest.raises(StorageError) as error:
        repository.write(report)
    assert "Authorization" not in str(error.value)
    assert len(list(repository.root.rglob("*.json"))) == 1
    assert not list(repository.root.rglob("*.md"))


def test_corrupt_identity_and_extra_payload_rejected(tmp_path):
    report, _, _, _, settings = product(tmp_path)
    repository = TimeSliceRepository(settings)
    path, _ = repository.write(report)
    data = json.loads(path.read_text())
    data["report_id"] = "0" * 64
    from quantos.time_slices import time_slice_from_record
    with pytest.raises(ValueError):
        time_slice_from_record(data)
    data["payload"]["current_price"] = 1
    with pytest.raises(TypeError):
        time_slice_from_record(data)


def test_unreadable_artifact_safe_error(tmp_path):
    _, _, _, _, settings = product(tmp_path)
    with pytest.raises(StorageError, match="failed to read time-slice"):
        TimeSliceRepository(settings).read(tmp_path / "missing.json")
