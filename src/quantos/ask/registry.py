"""Explicit Ask capability allowlist; handlers are registered by application code."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Any, Callable

from .contracts import AskIntent


class ToolRejected(ValueError):
    """Safe registry denial without leaking arguments or handler exceptions."""


class CapabilityAvailability(str, Enum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    input_schema: tuple[tuple[str, type], ...]
    allowed_intents: tuple[AskIntent, ...]
    handler: Callable[[dict[str, Any]], Any]
    output_contract: type | tuple[type, ...]
    version: str = "v1"
    availability: CapabilityAvailability = CapabilityAvailability.READY

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z]+(?:\.[a-z_]+)+", self.name) or not self.input_schema or not self.allowed_intents:
            raise ValueError("INVALID_CAPABILITY")
        if len({name for name, _ in self.input_schema}) != len(self.input_schema):
            raise ValueError("INVALID_CAPABILITY")
        if not callable(self.handler) or any(not isinstance(item, AskIntent)
                                             for item in self.allowed_intents):
            raise ValueError("INVALID_CAPABILITY")
        if not isinstance(self.availability, CapabilityAvailability):
            raise ValueError("INVALID_CAPABILITY")


class ToolRegistry:
    def __init__(self, capabilities: tuple[Capability, ...] = ()) -> None:
        names = [item.name for item in capabilities]
        if len(names) != len(set(names)):
            raise ValueError("DUPLICATE_CAPABILITY")
        self._items = {item.name: item for item in capabilities}

    def has(self, name: str) -> bool:
        return name in self._items

    def availability(self, name: str) -> CapabilityAvailability:
        capability = self._items.get(name)
        return (capability.availability if capability is not None
                else CapabilityAvailability.UNAVAILABLE)

    def execute(self, name: str, intent: AskIntent, args: dict[str, Any]) -> Any:
        capability = self._items.get(name)
        if capability is None:
            raise ToolRejected("TOOL_UNAVAILABLE")
        if capability.availability in {
            CapabilityAvailability.UNAVAILABLE, CapabilityAvailability.DISABLED,
        }:
            raise ToolRejected("TOOL_UNAVAILABLE")
        if intent not in capability.allowed_intents:
            raise ToolRejected("POLICY_DENIED")
        if not isinstance(args, dict) or set(args) != {field for field, _ in capability.input_schema}:
            raise ToolRejected("INVALID_TOOL_INPUT")
        if any(not isinstance(args[field], kind) for field, kind in capability.input_schema):
            raise ToolRejected("INVALID_TOOL_INPUT")
        if any(callable(value) or isinstance(value, (dict, list, set)) for value in args.values()):
            raise ToolRejected("INVALID_TOOL_INPUT")
        if any(isinstance(value, datetime) and value.utcoffset() is None
               for value in args.values()):
            raise ToolRejected("INVALID_TOOL_INPUT")
        if "symbol" in args and not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", args["symbol"]):
            raise ToolRejected("INVALID_TOOL_INPUT")
        result = capability.handler(args)
        if not isinstance(result, capability.output_contract):
            raise ToolRejected("INVALID_TOOL_OUTPUT")
        return result
