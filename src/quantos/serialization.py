"""Dependency-free canonical JSON serialization shared by QuantOS layers."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
import json
import math
from typing import Any, Mapping
from zoneinfo import ZoneInfo


_IDENTITY_TIMEZONE = ZoneInfo("Asia/Shanghai")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize supported values to the existing deterministic JSON encoding."""
    return _canonical(value).encode("utf-8")


def canonical_identity_json_bytes(value: Any) -> bytes:
    """Serialize logical identity values with instant-equivalent datetimes."""
    return _canonical(_normalize_identity_datetimes(value)).encode("utf-8")


def _normalize_identity_datetimes(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {
            key: _normalize_identity_datetimes(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_normalize_identity_datetimes(item) for item in value)
    if isinstance(value, list):
        return [_normalize_identity_datetimes(item) for item in value]
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("identity datetime must be timezone-aware")
        return value.astimezone(_IDENTITY_TIMEZONE)
    return value


def _canonical(value: Any) -> str:
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    if isinstance(value, Mapping):
        return "{" + ",".join(
            json.dumps(str(key), ensure_ascii=False) + ":" + _canonical(value[key])
            for key in sorted(value)
        ) + "}"
    if isinstance(value, (tuple, list)):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, (date, datetime)):
        return json.dumps(value.isoformat(), ensure_ascii=False)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical JSON numbers must be finite")
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON numbers must be finite")
        return json.dumps(value, allow_nan=False)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
