"""Pure QA contract tests; no provider or network is involved."""

from datetime import datetime

import pytest

from quantos.config import MARKET_TIMEZONE
from quantos.qa import (QAContext, QAMarketFacts, QAResearchEvidence,
                        QAValidationError, validate_answer, grounding_payload,
                        sanitize_terminal_text)


NOW = datetime(2026, 9, 18, 19, tzinfo=MARKET_TIMEZONE)


def context():
    return QAContext(
        symbol="600519.SH", as_of_time=NOW,
        market=QAMarketFacts("M1", "2026-09-18", "1430", "1400", "2.14",
                             1000, "1430000", "tushare", "daily:1", NOW),
        research=(QAResearchEvidence("E1", "公告", "摘要", "https://example.org/a",
                                     None, "tavily", False),))


def answer(kind="fact", refs=None, text="收盘价为 1430。"):
    refs = ["M1"] if refs is None else refs
    return {"answer": text, "claims": [{"kind": kind, "text": text,
                                           "source_refs": refs}],
            "source_refs": refs, "certainty": "uncertain"}


def test_unknown_source_ref_rejected():
    with pytest.raises(QAValidationError, match="UNKNOWN_SOURCE_REF"):
        validate_answer(answer(refs=["E9"]), context(), "价格？")


@pytest.mark.parametrize("kind", ["fact", "interpretation"])
def test_claim_requires_source_ref(kind):
    with pytest.raises(QAValidationError, match="CLAIM_SOURCE_REQUIRED"):
        validate_answer(answer(kind=kind, refs=[]), context(), "解释？")


def test_valid_m1_and_e1_accepted():
    value = answer(refs=["M1", "E1"])
    result = validate_answer(value, context(), "可能相关？")
    assert result.source_refs == ("M1", "E1")
    assert len(result.claims) == 1


def test_strict_pit_false_preserved_in_grounding_payload():
    payload = grounding_payload(context(), "消息？")
    assert payload["strict_pit"] is False
    assert payload["sources"][0]["strict_pit"] is False
    assert payload["market"]["ref"] == "M1"


def test_causal_overclaim_rejected():
    with pytest.raises(QAValidationError, match="UNSUPPORTED_CAUSAL_CLAIM"):
        validate_answer(answer(refs=["E1"], text="这些新闻导致了上涨。"), context(), "能证明因果吗？")


def test_negated_causal_claim_accepted():
    text = "现有搜索结果不能证明上涨由这些新闻导致。"
    result = validate_answer(answer(kind="interpretation", refs=["M1", "E1"], text=text),
                             context(), "能证明因果吗？")
    assert result.answer == text


def test_future_certainty_rejected():
    with pytest.raises(QAValidationError, match="FUTURE_CERTAINTY"):
        validate_answer(answer(text="明天一定上涨。"), context(), "明天一定涨吗？")


def test_negated_future_certainty_accepted():
    text = "根据当前事实，不能确定会在明天上涨。"
    result = validate_answer(answer(kind="interpretation", text=text),
                             context(), "明天一定涨吗？")
    assert result.answer == text


def test_terminal_controls_rejected():
    with pytest.raises(QAValidationError, match="TERMINAL_CONTROL_CHARACTER"):
        validate_answer(answer(text="收盘价\x1b[31m"), context(), "价格？")
    assert "\x1b" not in sanitize_terminal_text("bad\x1b[31m")


def test_malformed_structured_answer_rejected():
    with pytest.raises(QAValidationError, match="INVALID_QA_RESPONSE"):
        validate_answer(["wrong"], context(), "消息？")
    malformed = answer()
    malformed["certainty"] = []
    with pytest.raises(QAValidationError, match="INVALID_QA_RESPONSE"):
        validate_answer(malformed, context(), "消息？")
