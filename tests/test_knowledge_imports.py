"""Regression tests for low-level package import boundaries."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import os
from pathlib import Path
import subprocess
import sys

import pytest

from quantos.serialization import canonical_json_bytes as low_level_canonical_json_bytes


@pytest.mark.parametrize("modules", [
    ("quantos.schemas", "quantos.knowledge_retrieval"),
    ("quantos.knowledge_retrieval", "quantos.schemas"),
    ("quantos.storage", "quantos.schemas"),
    ("quantos.reporting", "quantos.schemas"),
    (
        "quantos.knowledge_chunking",
        "quantos.knowledge_retrieval",
        "quantos.storage.knowledge_index",
    ),
    (
        "quantos.schemas",
        "quantos.knowledge_context",
        "quantos.storage.knowledge_context",
    ),
    (
        "quantos.knowledge_context",
        "quantos.synthesis",
        "quantos.schemas",
        "quantos.storage",
    ),
    (
        "quantos.knowledge_integration",
        "quantos.synthesis",
        "quantos.knowledge_context",
        "quantos.schemas",
        "quantos.storage",
    ),
    (
        "quantos.reporting",
        "quantos.time_slices",
        "quantos.knowledge_integration",
        "quantos.schemas",
        "quantos.storage",
    ),
    (
        "quantos.storage.run",
        "quantos.orchestration",
        "quantos.reporting",
        "quantos.schemas",
    ),
])
def test_package_import_orders_are_independent(modules):
    root = Path(__file__).parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(root / "src") + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    code = ";".join(f"import {module}" for module in modules)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root, env=environment,
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_reporting_reexports_unchanged_canonical_serialization_contract():
    from quantos.reporting import canonical_json_bytes as reporting_canonical_json_bytes

    @dataclass(frozen=True)
    class Record:
        label: str
        instant: datetime
        amount: Decimal

    value = {
        "z": [Record("贵州茅台", datetime.fromisoformat("2026-09-08T12:00:00+08:00"),
                     Decimal("1.2300"))],
        "a": 1.5,
    }
    expected = (
        b'{"a":1.5,"z":[{"amount":1.2300,"instant":'
        b'"2026-09-08T12:00:00+08:00","label":"\xe8\xb4\xb5\xe5\xb7\x9e\xe8\x8c\x85\xe5\x8f\xb0"}]}'
    )
    assert low_level_canonical_json_bytes(value) == expected
    assert reporting_canonical_json_bytes(value) == expected
