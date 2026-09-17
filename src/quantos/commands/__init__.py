"""Shared execution and rendering contracts for user-facing commands."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping


@dataclass(frozen=True, slots=True)
class CommandFailure(Exception):
    """A safe user-facing failure with no provider or secret detail."""

    error_code: str
    remediation: str
    exit_code: int = 1

    def record(self) -> dict[str, object]:
        return {
            "status": "FAIL",
            "error_code": self.error_code,
            "remediation": self.remediation,
        }


def render_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
