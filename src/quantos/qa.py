"""Immutable, provider-neutral grounding contract for QuantOS Ask."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any


QA_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["answer", "claims", "source_refs", "certainty"],
    "properties": {
        "answer": {"type": "string"},
        "claims": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "text", "source_refs"],
            "properties": {
                "kind": {"type": "string", "enum": ["fact", "interpretation"]},
                "text": {"type": "string"},
                "source_refs": {"type": "array", "minItems": 1,
                                "items": {"type": "string"}},
            },
        }},
        "source_refs": {"type": "array", "items": {"type": "string"}},
        "certainty": {"type": "string", "enum": ["uncertain", "not_applicable"]},
    },
}

_FUTURE = re.compile(r"明天|明日|下个交易日|未来|预测|一定会|必然")
_CERTAIN = re.compile(r"一定|必然|保证|肯定|确定会|目标价")
_CAUSAL = re.compile(r"(导致|造成|引发|驱动|证明了|就是因为|确定由).{0,12}(上涨|下跌|涨|跌)|(上涨|下跌).{0,20}(因为|由.{0,10}导致)")


class QAValidationError(ValueError):
    """Safe validation code; rejected model text is never included."""


@dataclass(frozen=True, slots=True)
class QAMarketFacts:
    ref: str
    trade_date: str
    close: str
    prev_close: str | None
    change_pct: str | None
    volume: int
    amount: str
    source: str
    source_record_id: str
    available_at: datetime

    def __post_init__(self) -> None:
        if self.ref != "M1" or self.available_at.tzinfo is None:
            raise QAValidationError("INVALID_MARKET_FACTS")

    def payload(self) -> dict[str, Any]:
        return {"ref": self.ref, "trade_date": self.trade_date, "close": self.close,
                "prev_close": self.prev_close, "change_pct": self.change_pct,
                "volume": self.volume, "amount": self.amount, "source": self.source,
                "source_record_id": self.source_record_id,
                "available_at": self.available_at.isoformat()}


@dataclass(frozen=True, slots=True)
class QAResearchEvidence:
    ref: str
    title: str
    snippet: str
    url: str
    published_at: datetime | None
    provider: str
    strict_pit: bool

    def __post_init__(self) -> None:
        if not re.fullmatch(r"E[1-9]\d*", self.ref) or self.strict_pit:
            raise QAValidationError("INVALID_RESEARCH_EVIDENCE")

    def payload(self) -> dict[str, Any]:
        return {"ref": self.ref, "title": self.title, "snippet": self.snippet,
                "url": self.url,
                "published_at": self.published_at.isoformat() if self.published_at else None,
                "provider": self.provider, "strict_pit": False}


@dataclass(frozen=True, slots=True)
class QAContext:
    symbol: str
    as_of_time: datetime
    market: QAMarketFacts
    research: tuple[QAResearchEvidence, ...]

    def __post_init__(self) -> None:
        if self.as_of_time.tzinfo is None or self.market.available_at > self.as_of_time:
            raise QAValidationError("INVALID_QA_CONTEXT")
        if len({item.ref for item in self.research}) != len(self.research):
            raise QAValidationError("DUPLICATE_SOURCE_REF")


@dataclass(frozen=True, slots=True)
class QAClaim:
    kind: str
    text: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QAAnswer:
    answer: str
    claims: tuple[QAClaim, ...]
    source_refs: tuple[str, ...]
    certainty: str


def grounding_payload(context: QAContext, question: str) -> dict[str, Any]:
    return {"symbol": context.symbol, "question": question,
            "as_of_time": context.as_of_time.isoformat(), "strict_pit": False,
            "market": context.market.payload(),
            "sources": [item.payload() for item in context.research]}


def grounding_prompt(*, has_research: bool) -> str:
    research_note = (
        "搜索元数据 strict_pit=false，不是严格历史 PIT 证据。搜索结果只表明可能相关，"
        "不能证明目标时点前可用或证明市场因果。"
        if has_research else "No research evidence is available. 只能使用 M1 市场事实。"
    )
    return (
        "你是 QuantOS 证据问答助手。只使用输入的 M1 市场事实和 E1..En 搜索元数据。"
        "每条 fact 或 interpretation claim 都必须有 source_refs；市场事实引用 M1，"
        "搜索信息引用对应 E 编号。answer 必须与 claims 的 text 逐条一致，多个 claim 用换行连接。"
        "市场数字只能引用输入，不能自行计算或编造。"
        f"{research_note}不得给出确定性未来价格预测或直接交易建议。"
        "缺乏依据时明确说证据不足。source_refs 只能来自输入，不得引用其他来源。"
        "输出符合 JSON schema。"
    )


def _has_terminal_control(value: str) -> bool:
    return any(ord(char) < 32 and char not in "\n\t" or ord(char) == 127
               for char in value)


def sanitize_terminal_text(value: Any) -> str:
    return "".join(char for char in str(value)
                   if char.isprintable() and char not in "\x1b\x7f")


def _has_unsupported_causal_claim(value: str) -> bool:
    for clause in re.split(r"[。！？；;，,\n]", value):
        for match in _CAUSAL.finditer(clause):
            prefix = clause[max(0, match.start() - 24):match.start()]
            if not re.search(r"不能|无法|不足以|并非|不可|不等于", prefix):
                return True
    return False


def _has_future_certainty(value: str) -> bool:
    for clause in re.split(r"[。！？；;，,\n]", value):
        for match in _CERTAIN.finditer(clause):
            prefix = clause[max(0, match.start() - 20):match.start()]
            if not re.search(r"不能|无法|不可|不应|没有|不保证|不确定|并非", prefix):
                return True
    return False


def validate_answer(value: Any, context: QAContext, question: str) -> QAAnswer:
    if not isinstance(value, Mapping) or set(value) != set(QA_SCHEMA["required"]):
        raise QAValidationError("INVALID_QA_RESPONSE")
    answer, claims, refs = value["answer"], value["claims"], value["source_refs"]
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 2000:
        raise QAValidationError("INVALID_QA_RESPONSE")
    if not isinstance(claims, list) or not claims or not isinstance(refs, list):
        raise QAValidationError("INVALID_QA_RESPONSE")
    if not isinstance(value["certainty"], str) or value["certainty"] not in {"uncertain", "not_applicable"}:
        raise QAValidationError("INVALID_QA_RESPONSE")
    allowed = {"M1", *(item.ref for item in context.research)}
    parsed: list[QAClaim] = []
    for raw in claims:
        if not isinstance(raw, Mapping) or set(raw) != {"kind", "text", "source_refs"}:
            raise QAValidationError("INVALID_QA_RESPONSE")
        text, claim_refs = raw["text"], raw["source_refs"]
        if not isinstance(raw["kind"], str) or raw["kind"] not in {"fact", "interpretation"} or not isinstance(text, str) or not text.strip():
            raise QAValidationError("INVALID_QA_RESPONSE")
        if not isinstance(claim_refs, list) or any(not isinstance(ref, str) for ref in claim_refs):
            raise QAValidationError("INVALID_QA_RESPONSE")
        if not claim_refs:
            raise QAValidationError("CLAIM_SOURCE_REQUIRED")
        if any(ref not in allowed for ref in claim_refs):
            raise QAValidationError("UNKNOWN_SOURCE_REF")
        if len(set(claim_refs)) != len(claim_refs):
            raise QAValidationError("DUPLICATE_SOURCE_REF")
        parsed.append(QAClaim(raw["kind"], text.strip(), tuple(claim_refs)))
    if any(not isinstance(ref, str) for ref in refs):
        raise QAValidationError("INVALID_QA_RESPONSE")
    if any(ref not in allowed for ref in refs):
        raise QAValidationError("UNKNOWN_SOURCE_REF")
    if len(set(refs)) != len(refs):
        raise QAValidationError("DUPLICATE_SOURCE_REF")
    if set(refs) != {ref for claim in parsed for ref in claim.source_refs}:
        raise QAValidationError("INVALID_QA_RESPONSE")
    if answer.strip() != "\n".join(claim.text for claim in parsed):
        raise QAValidationError("INVALID_QA_RESPONSE")
    if _has_terminal_control(answer) or any(_has_terminal_control(claim.text) for claim in parsed):
        raise QAValidationError("TERMINAL_CONTROL_CHARACTER")
    if _FUTURE.search(question) and _has_future_certainty(answer):
        raise QAValidationError("FUTURE_CERTAINTY")
    if _has_unsupported_causal_claim(answer):
        raise QAValidationError("UNSUPPORTED_CAUSAL_CLAIM")
    return QAAnswer(answer.strip(), tuple(parsed), tuple(refs), value["certainty"])
