"""Deterministic, finite intent classification for Phase 6A.1."""

from __future__ import annotations

import re

from .contracts import AskIntent


_RULES = (
    (AskIntent.SYSTEM_STATUS, r"系统状态|运行状态|健康状态|system status"),
    (AskIntent.HISTORICAL_COMPARE, r"历史.*比较|历史.*对比|和.*去年|与.*去年"),
    (AskIntent.SECTOR_PERFORMANCE, r"板块|行业.*表现|sector"),
    (AskIntent.ENTITY_MOVE_EXPLANATION, r"为什么.*[涨跌]|上涨.*原因|下跌.*原因"),
    (AskIntent.ATTRIBUTION_LOOKUP, r"归因|因果|导致"),
    (AskIntent.KNOWLEDGE_LOOKUP, r"知识库|研报|文档|knowledge"),
    (AskIntent.REPORT_LOOKUP, r"已有报告|日报|盘前报告|盘后报告|report"),
    (AskIntent.EVIDENCE_LOOKUP, r"消息|新闻|公告|证据|evidence"),
    (AskIntent.MARKET_OVERVIEW, r"表现|行情|收盘|价格|涨跌|成交|今天|明天|最近|market"),
)


def resolve_intent(question: str) -> AskIntent:
    if re.search(r"\b(sql|python|shell|exec|drop|select)\b|执行任意|交易下单", question, re.I):
        return AskIntent.UNSUPPORTED
    for intent, pattern in _RULES:
        if re.search(pattern, question, re.I):
            return intent
    return AskIntent.UNSUPPORTED
