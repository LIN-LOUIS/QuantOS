"""Immutable, serializable contracts for the bounded Ask entry point."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from quantos.serialization import canonical_identity_json_bytes


class AskIntent(str, Enum):
    MARKET_OVERVIEW = "MARKET_OVERVIEW"
    ENTITY_MOVE_EXPLANATION = "ENTITY_MOVE_EXPLANATION"
    SECTOR_PERFORMANCE = "SECTOR_PERFORMANCE"
    EVIDENCE_LOOKUP = "EVIDENCE_LOOKUP"
    ATTRIBUTION_LOOKUP = "ATTRIBUTION_LOOKUP"
    KNOWLEDGE_LOOKUP = "KNOWLEDGE_LOOKUP"
    REPORT_LOOKUP = "REPORT_LOOKUP"
    HISTORICAL_COMPARE = "HISTORICAL_COMPARE"
    SYSTEM_STATUS = "SYSTEM_STATUS"
    UNSUPPORTED = "UNSUPPORTED"


ASK_REASON_CODES = frozenset({
    "OK", "UNSUPPORTED_INTENT", "ENTITY_NOT_FOUND", "AMBIGUOUS_ENTITY",
    "DATA_UNAVAILABLE", "HISTORICAL_DATA_UNAVAILABLE", "ATTRIBUTION_INSUFFICIENT",
    "KNOWLEDGE_UNAVAILABLE", "REPORT_UNAVAILABLE", "NO_EVIDENCE",
    "TOOL_UNAVAILABLE", "TOOL_FAILED", "POLICY_DENIED", "SYNTHESIS_FAILED",
    "IDENTITY_DATA_UNAVAILABLE",
})


def _aware(value: datetime | None) -> None:
    if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
        raise ValueError("TIMEZONE_REQUIRED")


def _identity(value: object) -> str:
    return hashlib.sha256(canonical_identity_json_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class AskRequest:
    question: str
    as_of_time: datetime | None = None
    market: str = "A_SHARE"
    entity_hints: tuple[str, ...] = ()
    locale: str = "zh-CN"
    no_research: bool = False
    raw_question: str = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.question, str):
            raise ValueError("INVALID_QUESTION")
        object.__setattr__(self, "raw_question", self.question)
        normalized = " ".join(self.question.split())
        if (not normalized or len(normalized) > 1000
                or any(ord(c) < 32 for c in normalized)
                or re.search(r"(?i)\b(?:authorization|password|api[_-]?key|token)\s*[:=]|\b(?:sk|tvly)-[A-Za-z0-9_-]{12,}|\b[A-Za-z0-9_-]{40,}\b", normalized)):
            raise ValueError("INVALID_QUESTION")
        object.__setattr__(self, "question", normalized)
        _aware(self.as_of_time)
        if self.market != "A_SHARE" or self.locale != "zh-CN":
            raise ValueError("UNSUPPORTED_MARKET_OR_LOCALE")
        if not isinstance(self.entity_hints, tuple) or any(
            not isinstance(hint, str) or not hint.strip() for hint in self.entity_hints
        ):
            raise ValueError("INVALID_ENTITY_HINT")
        if type(self.no_research) is not bool:
            raise ValueError("INVALID_RESEARCH_POLICY")

    def to_dict(self) -> dict[str, Any]:
        return {"question": self.question,
                "as_of_time": self.as_of_time.isoformat() if self.as_of_time else None,
                "market": self.market, "entity_hints": list(self.entity_hints),
                "locale": self.locale, "no_research": self.no_research}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AskRequest:
        if set(value) != {"question", "as_of_time", "market", "entity_hints", "locale", "no_research"}:
            raise ValueError("INVALID_ASK_REQUEST")
        stamp = value["as_of_time"]
        return cls(value["question"], datetime.fromisoformat(stamp) if stamp else None,
                   value["market"], tuple(value["entity_hints"]), value["locale"],
                   value["no_research"])


@dataclass(frozen=True, slots=True)
class ResolvedEntity:
    raw: str
    symbol: str
    source: str
    status: str = "RESOLVED"


@dataclass(frozen=True, slots=True)
class QueryStep:
    capability: str
    args: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z]+(?:\.[a-z_]+)+", self.capability):
            raise ValueError("INVALID_CAPABILITY")
        if tuple(sorted(self.args)) != self.args or len({key for key, _ in self.args}) != len(self.args):
            raise ValueError("INVALID_STEP_ARGS")

    def to_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "args": dict(self.args)}


@dataclass(frozen=True, slots=True)
class QueryPlan:
    request_id: str
    intent: AskIntent
    normalized_question: str
    entities: tuple[ResolvedEntity, ...]
    as_of_time: datetime
    time_scope: str
    steps: tuple[QueryStep, ...]
    policy_result: str
    unsupported_reason: str | None = None
    capability_availability: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _aware(self.as_of_time)
        if not isinstance(self.intent, AskIntent):
            raise ValueError("INVALID_INTENT")
        if self.policy_result not in {"ALLOW", "DENY"}:
            raise ValueError("INVALID_POLICY")
        if tuple(sorted(self.capability_availability)) != self.capability_availability:
            raise ValueError("INVALID_CAPABILITY_AVAILABILITY")

    @property
    def plan_hash(self) -> str:
        return _identity(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "intent": self.intent.value,
                "normalized_question": self.normalized_question,
                "entities": [asdict(entity) for entity in self.entities],
                "as_of_time": self.as_of_time.isoformat(), "time_scope": self.time_scope,
                "required_capabilities": [step.capability for step in self.steps],
                "steps": [step.to_dict() for step in self.steps],
                "policy_result": self.policy_result,
                "unsupported_reason": self.unsupported_reason,
                "capability_availability": dict(self.capability_availability)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> QueryPlan:
        steps = tuple(QueryStep(item["capability"], tuple(sorted(item["args"].items())))
                      for item in value["steps"])
        if value["required_capabilities"] != [step.capability for step in steps]:
            raise ValueError("INVALID_PLAN_CAPABILITIES")
        return cls(value["request_id"], AskIntent(value["intent"]),
                   value["normalized_question"],
                   tuple(ResolvedEntity(**item) for item in value["entities"]),
                   datetime.fromisoformat(value["as_of_time"]), value["time_scope"],
                   steps, value["policy_result"],
                   value["unsupported_reason"],
                   tuple(sorted(value.get("capability_availability", {}).items())))


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    capability: str
    status: str
    result_count: int
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class AskTrace:
    trace_id: str
    request_id: str
    created_at: datetime
    completed_at: datetime
    intent: AskIntent
    raw_question: str
    normalized_question: str
    entities: tuple[ResolvedEntity, ...]
    as_of_time: datetime
    plan: QueryPlan
    invocations: tuple[ToolInvocation, ...]
    evidence_refs: tuple[str, ...]
    knowledge_refs: tuple[str, ...]
    report_refs: tuple[str, ...]
    reason_codes: tuple[str, ...]
    limitations: tuple[str, ...]
    errors: tuple[str, ...]
    duration_ms: float = 0.0
    run_id: str | None = None

    def __post_init__(self) -> None:
        _aware(self.created_at)
        _aware(self.completed_at)
        _aware(self.as_of_time)
        if self.completed_at < self.created_at or self.duration_ms < 0:
            raise ValueError("INVALID_TRACE_TIMING")
        if self.request_id != self.plan.request_id or self.as_of_time != self.plan.as_of_time:
            raise ValueError("INVALID_TRACE_PLAN")
        if any(code not in ASK_REASON_CODES for code in self.reason_codes):
            raise ValueError("INVALID_REASON_CODE")
        if self.run_id is not None and (not isinstance(self.run_id, str) or not self.run_id):
            raise ValueError("INVALID_RUN_ID")

    def to_dict(self) -> dict[str, Any]:
        return {"trace_id": self.trace_id, "request_id": self.request_id,
                "created_at": self.created_at.isoformat(),
                "completed_at": self.completed_at.isoformat(),
                "intent": self.intent.value, "raw_question": self.raw_question,
                "normalized_question": self.normalized_question,
                "entities": [asdict(item) for item in self.entities],
                "as_of_time": self.as_of_time.isoformat(), "query_plan": self.plan.to_dict(),
                "tool_invocations": [asdict(item) for item in self.invocations],
                "evidence_refs": list(self.evidence_refs),
                "knowledge_refs": list(self.knowledge_refs), "report_refs": list(self.report_refs),
                "reason_codes": list(self.reason_codes), "limitations": list(self.limitations),
                "errors": list(self.errors), "duration_ms": self.duration_ms,
                "run_id": self.run_id}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AskTrace:
        return cls(value["trace_id"], value["request_id"],
                   datetime.fromisoformat(value["created_at"]),
                   datetime.fromisoformat(value["completed_at"]),
                   AskIntent(value["intent"]), value["raw_question"],
                   value["normalized_question"],
                   tuple(ResolvedEntity(**item) for item in value["entities"]),
                   datetime.fromisoformat(value["as_of_time"]),
                   QueryPlan.from_dict(value["query_plan"]),
                   tuple(ToolInvocation(**item) for item in value["tool_invocations"]),
                   tuple(value["evidence_refs"]), tuple(value["knowledge_refs"]),
                   tuple(value["report_refs"]), tuple(value["reason_codes"]),
                   tuple(value["limitations"]), tuple(value["errors"]),
                   value["duration_ms"], value.get("run_id"))


@dataclass(frozen=True, slots=True)
class AskResponse:
    request_id: str
    trace_id: str
    status: str
    answer: str
    intent: AskIntent
    entities: tuple[ResolvedEntity, ...]
    as_of_time: datetime
    facts: tuple[dict[str, Any], ...]
    evidence_refs: tuple[str, ...]
    knowledge_refs: tuple[str, ...]
    report_refs: tuple[str, ...]
    limitations: tuple[str, ...]
    reason_codes: tuple[str, ...]
    trace: AskTrace
    sources: tuple[dict[str, Any], ...] = ()
    source_refs: tuple[str, ...] = ()
    mode: str = "grounded_qa"
    research_status: str = "disabled"
    market_requests: int = 0
    research_requests: int = 0
    llm_requests: int = 0

    def __post_init__(self) -> None:
        if self.status not in {"PASS", "PARTIAL", "FAIL", "UNSUPPORTED"}:
            raise ValueError("INVALID_ASK_STATUS")
        if any(code not in ASK_REASON_CODES for code in self.reason_codes):
            raise ValueError("INVALID_REASON_CODE")
        if self.trace_id != self.trace.trace_id or self.request_id != self.trace.request_id:
            raise ValueError("INVALID_RESPONSE_TRACE")

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def to_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "trace_id": self.trace_id,
                "status": self.status, "answer": self.answer,
                "intent": self.intent.value,
                "entities": [asdict(item) for item in self.entities],
                "as_of_time": self.as_of_time.isoformat(), "facts": list(self.facts),
                "evidence_refs": list(self.evidence_refs),
                "knowledge_refs": list(self.knowledge_refs), "report_refs": list(self.report_refs),
                "limitations": list(self.limitations), "reason_codes": list(self.reason_codes),
                "trace": self.trace.to_dict(),
                "symbol": self.entities[0].symbol if self.entities else None,
                "question": self.trace.raw_question,
                "market": self.facts[0] if self.facts else None,
                "sources": list(self.sources), "source_refs": list(self.source_refs),
                "mode": self.mode, "research_status": self.research_status,
                "market_requests": self.market_requests,
                "research_requests": self.research_requests,
                "llm_requests": self.llm_requests,
                "strict_pit": False,
                "pit_warning": "Tavily 搜索元数据不满足严格 PIT；不能证明历史时点已可见。"}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AskResponse:
        return cls(value["request_id"], value["trace_id"], value["status"],
                   value["answer"], AskIntent(value["intent"]),
                   tuple(ResolvedEntity(**item) for item in value["entities"]),
                   datetime.fromisoformat(value["as_of_time"]),
                   tuple(value["facts"]), tuple(value["evidence_refs"]),
                   tuple(value["knowledge_refs"]), tuple(value["report_refs"]),
                   tuple(value["limitations"]), tuple(value["reason_codes"]),
                   AskTrace.from_dict(value["trace"]), tuple(value["sources"]),
                   tuple(value["source_refs"]), value["mode"],
                   value["research_status"], value["market_requests"],
                   value["research_requests"], value["llm_requests"])
